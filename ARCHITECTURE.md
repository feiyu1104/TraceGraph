# TraceGraph 架构

## 主线

TraceGraph 以证据生命周期为主线，而不是以模型调用为中心：

```text
文档或 DUTMed JSON
  → Document / Version / Chunk
  → Entity / Relation / relation.evidence_chunk_ids
  → KeywordRetriever + GraphRetriever
       GraphRetriever → traverse_paths（逐层 BFS）→ GraphRepository.expand_frontier
  → HybridRetriever
  → Evidence[]（graph_path.hop_count == 1 为原文事实，> 1 为推导关联）
  → DomainAdapter 安全分流
  → AnswerGenerator（按本次请求选定的那一个；只接收原文事实）
  → GeneratedAnswer（只有 claims，没有正文）
  → compose_text(claims) 确定性拼装 answer.text
  → Answer / Claim / derived_associations / Feedback / EvaluationCase
  → FastAPI /query、/models、/graph/*、/ingestions/*、/system
  → frontend/dist（React + TypeScript，托管在 /app）
```

文档入库有两条入口，能力并不相同。**DUTMed 专用导入**（`tracegraph import-medical`）读的是 schema 已定义的 JSONL，会同时写入文档侧和实体关系，因此生成可多跳遍历的图。**普通文件上传**（`POST /ingestions/file` 与网页的文档导入）只写入文档侧的 Document / Version / Chunk，参与关键词检索，**不生成任何实体关系**——从自由文本里猜实体只会污染图谱，并让本来可追溯的关系失去可信度。

任何图关系和回答主张都必须能回到实际 Chunk。删除文档时，系统先移除图关系中的 Chunk 引用，再删除文档版本和任务；失去全部证据的关系会一并清理。

多跳遍历的语义（防环、fanout 预算、排序、逐层限额）只实现在 `retrieval/traversal.py` 一处。图仓储只提供 `expand_frontier` 这**一个**新原语，因此内存、SQLite、Neo4j 三个后端天然共享同一套遍历行为，而不是靠人工对齐三份 BFS。

证据在进入生成之前按跳数分流：多跳推导出的关联不会进入 `Claim`，只作为独立区块呈现；没有任何一跳证据时直接返回 `insufficient_evidence`。这条边界由数据流保证，不依赖提示词约束。

## 分层

- `core`：不可变数据契约和端口，不依赖数据库、Web 或模型。
- `ingestion`：格式解析、切片、版本管理和删除生命周期。
- `storage`：内存、SQLite 和可选 Neo4j 实现。
- `retrieval`：关键词、图关系和混合检索。
- `generation`：离线摘录生成器、可选 OpenAI-compatible 生成器、模型清单（`models.py`）和单模型配置回退（`config.py`）。
- `domains/medical`：医疗 Schema、急症分流和 DUTMed JSON 迁移。
- `feedback`：反馈保存及评测案例提升。
- `evaluation`：检索与回答行为的离线指标。
- `api`：HTTP 契约、错误码、前端静态托管、组件状态和聚合运行指标。
- `frontend`：独立的 React + TypeScript + Vite 工程。Python 侧不含任何构建逻辑，只负责托管 `frontend/dist`；开发模式下由 Vite 代理 API，后端不开放 CORS。

## 部署组合

默认应用入口是 `tracegraph.bootstrap:app`。代码在没有环境变量时回退到 SQLite：文档、图谱和反馈共用 `data/local/tracegraph.db`，但由各自仓库管理独立数据表。该组合不需要外部服务。

当前本机部署已将 `TRACEGRAPH_GRAPH_BACKEND` 设为 `neo4j`。实体关系写入 `D:\Neo4j` 中的 Neo4j，文档元数据和原文仍留在 SQLite。图关系只保存 Evidence Chunk ID，避免在两个数据库中复制整段原文。导入器按疾病批量提交节点和关系，保证同一疾病的旧出向关系被原子替换。

图后端由 `graph_backend.create_graph_repository` 统一选择，`start.ps1 -Mode neo4j|sqlite` 是它在启动脚本上的入口。**默认不降级**：Neo4j 连不上就直接失败，因为换后端可能换掉查询结果。只有显式设置 `TRACEGRAPH_GRAPH_FALLBACK=sqlite` 才降级，且降级事实会写入启动日志与 `/system`（`graph_requested` / `graph_degraded` / `graph_detail`），不会静默发生。

### 模型清单与按请求选择

可选模型由 `generation/models.py` 的 `load_model_registry()` 装配，`bootstrap` 把它交给 `AnswerService` 与 `/models`。存在 `config/models.local.json` 时以它为准，否则退回 `generation/config.py` 的 `load_generation_config`（环境变量单模型），两条路径都产出同一份 `ModelRegistry`。

**选择发生在每次请求内部，不是进程级状态。** `AnswerService.answer(..., generator_id=...)` 只在本次调用里解析生成器，不改任何共享变量：并发用户各选各的模型互不影响，也不会把上一个人的选择留给下一次请求。`generator_id` 缺省时用清单里的默认项；未知 ID 抛 `UnknownGeneratorError`（400 `invalid_generator`），已标记不可用的 ID 抛 `UnavailableGeneratorError`（503 `generator_unavailable`）——**两者都不回退到另一个在线模型**。

配置错误的处理分两档，这是刻意的：**非默认条目**缺 `model` / `base_url` / 环境变量时只把自己标记为不可用并带上原因，进程照常启动，离线摘录永远可用；**默认条目**不可用则抛 `GenerationConfigurationError` 拒绝启动，因为那意味着每个请求都会失败。

`ModelEntry.describe()` 是模型对外的唯一描述，只有 `id` / `label` / `model` / `available` / `kind`（加不可用时的 `reason`）。`base_url` 不在其中：自建网关常把凭证写在地址上，因此连地址都不透出。`/system` 由 `ModelRegistry.system_status()` 提供 `llm_configured` / `llm_model` / `llm_fallback`，同样不含地址与密钥；同一响应里的 `generator` 由 `ModelRegistry.id_of()` 反查默认生成器的**注册表 ID**（不是生成器类名），与 `/models`、`metrics.generator` 保持同一个取值域。

**降级默认不发生**：只有显式设置 `TRACEGRAPH_LLM_FALLBACK=extractive` 才会在模型失败后改用离线摘录，且降级事实写进 `metrics.generator` / `metrics.model` / `metrics.generation_degraded` 与 `warnings[0]`，同时反映在 `/system` 上。`metrics.requested_generator` 记录请求指定的 ID，与真正生效的 `generator` 对照即可看出这次是否换了模型。

### 模型输出如何被约束

生成器交出的是 `GeneratedAnswer`，而它**只有 `claims` 一个字段，没有正文**。回答正文由 `generation/service.py` 的 `compose_text(claims)` 从通过校验的主张确定性拼装，因此模型返回的任何自由文本都没有进入 `answer.text` 的通道——它根本不会被读取。每条主张至少要引用一个本次提供的 Evidence ID，出现未知 ID 就整条响应作废。

失败经过分类而不是一律 500：配置错误在启动时抛 `GenerationConfigurationError`；连接、超时、上游非 2xx 归为 `generation_network_error`；响应不可解析、`claims` 为空或引用了未提供的 Evidence ID 归为 `generation_response_error`。任何一处校验不通过都作废整条回答，不返回部分通过的主张。

运维命令：`tracegraph graph-stats` 打印规模与类型分布，`tracegraph check-consistency` 检查图侧证据引用是否都落在文档侧 `chunks` 上（`relation_evidence.chunk_id` 刻意没有外键），并在配置了 Neo4j 时对比两库计数，`tracegraph resync-graph` 重跑导入并对比前后统计。

## 关键取舍

- SQLite 图存储是无需外部服务的基线；当前本机使用 SQLite 文档库与 Neo4j 图库的混合组合。
- 多跳遍历逐层扩展（每层一次批量查询），不用 `[*1..3]` 变长路径：只有逐层才能对每个中间节点单独施加 fanout 预算，并拿到真实边数用于截断提示。
- 多跳结果只作推导关联，不进入 `Claim`；评测口径固定为单跳。
- 生成器一律不做自由发挥：离线摘录直接摘原文，大模型只被允许提交结构化主张，正文由后端拼装。
- LLM 只消费 `Evidence[]`，无法自行查询数据库；返回的引用 ID 必须属于输入证据，图路径也不经过它。
- 前端拆成独立工程而不是内联 HTML 字符串：界面已经不是「一个页面」的规模，把它留在 Python 里意味着改一次样式就要动后端源码，且没有任何类型或构建期检查。代价是多一条 `npm` 构建链，因此 `/app` 在未构建时明确返回 503，而不是回落到旧页面。
- 前端用 Vite 代理而不是后端 CORS：开发期只有 Vite 一个入口，后端继续保持不对外开放跨域。
- 模型接入仍然不抽象成插件系统。清单（`ModelRegistry`）之所以存在，只是因为有两件具体的事需要它：浏览器要一份**不含地址与密钥**的模型列表，以及每次请求要按 ID 解析出一个生成器。它不做发现、不做注册装饰器、不加载第三方入口点。
- **模型选择是请求级参数，不是可修改的全局配置**：`/query` 不能改进程状态，因此并发用户不会互相影响，也不会有「谁最后点了谁说了算」的问题。代价是每次请求多一次字典查找——可以忽略。
- 普通上传不生成图关系：宁可让上传的文档只参与关键词检索，也不从自由文本里猜实体。图谱的价值来自每条关系都能回到原文，靠猜出来的关系会毁掉这一点。
- 上传大小限制前后端各查一次，且**后端不信任前端**：前端那次只是为了省掉一次注定失败的往返。文件名只作来源名称，不参与服务器路径拼接，上传内容也不落任意磁盘位置。
- 当前入库同步执行，尚未加入消息队列和多租户权限。
