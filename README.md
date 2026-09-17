# TraceGraph

面向专业知识库的证据优先 GraphRAG 应用框架。

TraceGraph 旨在把领域文档转化为可验证回答：用户不仅能看到自然语言答案，还能定位到对应的文档版本、原文位置、文本片段和图检索路径。第一个参考领域将是 DUTMed 医疗知识库。

## 为什么建设这个项目

常见 RAG 原型已经能够检索文本并生成流畅答案，TraceGraph 更关注应用层中尚未自然解决的问题：

- 回答中的每一项主要结论由什么证据支持？
- 没有证据或证据互相冲突时，系统应该如何响应？
- 如何将领域 Schema、安全规则和评测案例作为一个整体替换？
- 如何把用户反馈转化为可重复执行的回归案例？

TraceGraph 会复用成熟的模型、图数据库、向量数据库和文档解析工具。框架自身主要负责证据生命周期和领域应用契约。

## 当前能力

TraceGraph 本地参考实现已经具备：

- 不可变的 `Evidence`、`Claim`、`Answer` 和 `EvaluationCase` 契约；
- `Document`、`DocumentVersion`、`Chunk` 和 `IngestionJob` 文档生命周期契约；
- TXT、Markdown、JSON、JSONL、CSV 和文本型 PDF 解析，标题感知切片、内容哈希和幂等入库；
- 可替换的文档存储接口，以及内存和 SQLite 实现；
- 文档删除和关系证据级联清理；
- 关键词、医疗图关系和加权混合检索，并统一输出 `Evidence`；
- **默认 2 跳、上限 3 跳的多跳图检索**，带方向的完整路径、fanout 预算和显式截断提示；
- 检索 HTTP API、固定案例集和 Evidence Recall@k 评测器；
- 医疗 `DomainAdapter`、急症分流和 DUTMed JSONL 迁移；
- 离线摘录生成器，以及**不绑定厂商**的 OpenAI-compatible 模型接入，带超时、错误分类和严格的引用 ID 校验；
- **回答正文只由通过校验的 Claim 拼装**：模型只提交结构化主张，任何自由文本都没有进入 `answer.text` 的通道；
- **可配置文件化的多模型清单**，每次请求各自选择模型，浏览器只能看到模型 ID、显示名称、模型名称、可用性与默认标记；
- 生成器的**显式降级策略**：模型失败默认报错，只有显式配置才降级，降级事实写进 `/system` 与 `metrics`；
- 带 Claim–Evidence 绑定的结构化回答、拒答和冲突状态；
- **区分「原文事实」与「图路径推导的关联」**：推导关联不进入 `Claim`，没有原文事实时直接拒答；
- 网页端**文档导入**：上传即入库并可立即参与关键词检索，服务端与前端双重大小校验；
- 独立的 React + TypeScript + Vite 前端（`frontend/`），构建后由 FastAPI 托管在 `/app`；
- 反馈保存、反馈转评测案例和聚合指标；
- Neo4j 实体搜索、邻接关系和证据位置浏览；
- SQLite 文档存储、Neo4j 图存储，以及无需外部服务的 SQLite 图后端；
- `graph-stats`、`check-consistency`、`resync-graph` 三个运维命令。

### 多跳检索能回答什么

```text
百日咳 →资料列出的推荐药物是→ 环酯红霉素片 ←资料列出的推荐药物是← 口腔毛滴虫病
百日咳 →可能有症状→ 抽搐 ←可能有症状← 小儿心房扑动
```

左端是原文事实（可直接引用），右端是推导关联（明确标注「由其他实体的记录经图路径推导，不是本实体的原文结论」）。路径上的每条关系都保存支持它的 DUTMed Chunk ID，点击即可查看原文位置。

`max_hops` 通过查询参数控制（默认 2，上限 3），网页上对应「一跳 / 二跳 / 三跳」选择器，越界返回 HTTP 400。

当前非目标包括医学影像、多智能体、低代码工作流、多租户权限和公网部署。DUTMed 数据仅用于项目学习与检索验证，不能替代医疗诊断。

## 快速开始

需要 Python 3.11 或更高版本。

```bash
python -m venv .venv
```

激活虚拟环境：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux 或 macOS
source .venv/bin/activate
```

安装项目、Neo4j 驱动和开发依赖：

```bash
python -m pip install -e ".[dev,graph]"
```

确认 DUTMed 原始数据位于相邻目录：

```bash
../DUTMed/data/症状.json
```

先把全部 8808 条 DUTMed 疾病数据导入本地数据库：

```powershell
tracegraph import-medical `
  --source ..\DUTMed\data\症状.json `
  --database data\local\tracegraph.db
```

首次完整导入需要一些时间；再次执行相同命令会按内容哈希复用已有文档版本。

## 大模型接入与模型切换

### 只用离线摘录

默认生成器是 `extractive`：只把召回的证据整理成条目，不调用任何外部服务，也不需要任何配置。停掉所有模型服务，TraceGraph 依然可以完整跑通问答、多跳和图谱浏览。网页上的模型选择器里，它固定显示为「离线摘录（不调用任何模型）」，永远可选。

### 接入单个模型

要接入模型，把 `TRACEGRAPH_GENERATOR` 设为 `openai-compatible` 并补齐三项：

```powershell
$env:TRACEGRAPH_GENERATOR  = "openai-compatible"
$env:TRACEGRAPH_LLM_BASE_URL = "https://你的网关地址/v1"
$env:TRACEGRAPH_LLM_API_KEY  = "你的密钥"
$env:TRACEGRAPH_LLM_MODEL    = "你的模型名称"
```

`BASE_URL` 填服务根路径即可，客户端自己拼 `/chat/completions`。OpenAI 官方 API、通义千问兼容接口、DeepSeek 兼容接口、本地 Ollama 和自建网关走的是同一套变量——源码里不出现任何厂商域名或模型名。

三项缺任意一项，进程会在**启动时**直接失败并列出缺少的变量名，而不是等到第一次提问才报错；报错信息里只有变量名，没有变量取值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TRACEGRAPH_LLM_TIMEOUT` | `45` | 连接与响应超时，单位秒 |
| `TRACEGRAPH_LLM_FALLBACK` | `none` | `none`：模型失败就让请求失败；`extractive`：显式同意降级为离线摘录 |

这种模式下模型清单里只有两项：内置的 `extractive`，以及 ID 为 `openai-compatible` 的那一个模型，默认值是后者。

### 配置多个模型并切换

要提供多个模型给用户选择，复制一份示例清单：

```bash
cp config/models.example.json config/models.local.json
```

```json
{
  "default": "extractive",
  "models": [
    {
      "id": "main-model",
      "label": "主模型",
      "base_url": "https://example.com/v1",
      "model": "model-name",
      "api_key_env": "TRACEGRAPH_MAIN_MODEL_API_KEY",
      "timeout": 45
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 请求里 `generator_id` 的取值，必须唯一；`extractive` 是保留 ID |
| `label` | 是 | 网页选择器里显示的名称 |
| `base_url` | 是 | 服务根路径，客户端自己拼 `/chat/completions`，必须以 `http://` 或 `https://` 开头 |
| `model` | 是 | 传给上游的模型名称 |
| `api_key_env` | 是 | **环境变量名**，不是密钥本身；密钥只写在 `.env` 或系统环境变量里 |
| `timeout` | 否 | 该模型的超时秒数，缺省用 `TRACEGRAPH_LLM_TIMEOUT` |

`default` 指向 `models` 里某个 `id`，或直接写 `extractive`。清单文件路径可以用 `TRACEGRAPH_MODELS_CONFIG` 改写，默认是 `config/models.local.json`。

几条硬规则：

- **`config/models.local.json` 已被 `.gitignore` 忽略**。它会写满你个人的网关地址，属于本机配置，不要提交。仓库里只保留不含真实地址的 `config/models.example.json`。
- **清单里只写环境变量名**。密钥本身只存在于 `.env` 或系统环境变量，清单文件里永远不该出现。
- **缺少模型名称、地址或密钥的条目会被标记为不可用**，网页上显示原因并禁止选择——但**不会阻止进程启动**。只有一个例外：**默认模型自己不可用时会明确报错并拒绝启动**，因为那意味着每个请求都会失败。
- **不会回退到另一个在线模型**。选了不可用的模型，请求直接失败并返回 `generator_unavailable`；只有显式设置 `TRACEGRAPH_LLM_FALLBACK=extractive` 时才降级到离线摘录。

每次 `/query` 请求都可以带一个 `generator_id`：

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question":"百日咳用什么药","generator_id":"main-model"}'
```

不传 `generator_id` 就用服务端默认模型，传 `extractive` 就用离线摘录，传未知 ID 返回 HTTP 400 与 `invalid_generator`。**选择只作用于这一次请求**：模型不是进程级全局变量，同一个进程里并发用户的请求互不影响，也不会把某个人的选择留给下一次请求。

### `GET /models` 返回什么

```bash
curl http://127.0.0.1:8000/models
```

每一项只有 `id`、`label`、`model`、`available`、`kind` 五个字段（不可用时多一个 `reason`）。**刻意不返回 `base_url`**：自建网关常把凭证写在地址里，所以连地址都不透出。API Key、Authorization 头和任何带凭证的 URL 参数都不在这个接口里，也不在任何其他接口里。

### 模型失败时会发生什么

- **连不上、超时、上游非 2xx**：`/query` 返回 `status = system_error`，`error_code = generation_network_error`；
- **响应不是合法 JSON、`claims` 为空、引用了没有提供的 Evidence ID**：`error_code = generation_response_error`，整条回答作废——不会返回「通过了一半」的主张；
- **`TRACEGRAPH_LLM_FALLBACK=extractive`**：改用离线摘录重新生成，回答正常返回，但 `metrics.generation_degraded` 为 `true`、`metrics.generator` 变成 `extractive`，`warnings[0]` 说明降级原因。

降级永远不会自动发生。不设 `TRACEGRAPH_LLM_FALLBACK` 时，模型失败就是失败。

### 怎么确认当前是否降级

```bash
curl http://127.0.0.1:8000/system
```

`generator` 是**实际生效**的生成器的模型 ID（由运行时对象决定，取值与 `/models` 里的 `id` 同一套），`llm_configured` / `llm_model` / `llm_fallback` 来自配置，四者不一致就说明发生了降级。

回答内的 `metrics` 有四个相关字段：`requested_generator` 是这次请求指定的、`generator` 是这次真正生效的、`model` 是生效生成器使用的模型名称、`generation_degraded` 是这次是否发生了降级。网页回答区显示的是 `generator`（真正生效的那个），不是选择器里的当前值——降级时两者不同，这个差别必须看得见。

这个接口不返回 `BASE_URL`，也不返回 API Key——自建网关常把凭证写在地址里，所以连地址都不透出。

### 模型能看到什么

模型只收到当前问题和已经召回的**原文事实**证据（一跳）。多跳推导关联由后端确定性生成，不经过模型，因此模型无从把推导结果写成直接结论。DUTMed 原文在提示词里被明确标注为不可信数据：其中的任何指令都必须忽略。

模型返回的是**结构化主张**，不是一段回答正文：

```json
{"claims": [{"text": "……", "evidence_ids": ["ev-…"]}]}
```

每一条主张至少要引用一个本次提供的 Evidence ID，出现未知 ID 就整条响应作废。**`answer.text` 由后端从通过校验的主张确定性拼装**，模型返回的任何自由文本都没有进入正文的通道——它甚至不会被读取。图路径同样由后端生成，模型既看不到也造不出。

### 密钥

API Key 只从环境变量读取，不写入源码、文档、日志，也不出现在任何接口响应里。`.env` 已被 `.gitignore` 忽略，`.env.example` 里只有空值——**不要把自己的密钥填进会被提交的文件**。

**为什么不能由前端填写 API Key。** 密钥一旦进入浏览器，就必须经过网络下发到页面、落在浏览器内存和开发者工具里、随前端构建产物一起分发，任何能打开页面的人都能拿走它——而它代表的是服务端进程持有的凭证。所以网页上**没有、也不会有** API Key 输入框，同样不允许浏览器提交自定义的 `base_url` 或 `api_key`：那会把服务端变成一个任意地址的转发器（SSRF），也能让前端把密钥写到服务端的请求里。浏览器只能做一件事：从 `GET /models` 里挑一个服务端已经配置好的模型 ID。

需要换模型时改 `config/models.local.json` 和对应的环境变量，重启服务——这个动作只有能登服务器的人做得了。

## 本机 Neo4j

当前 Windows 环境已经使用官方 ZIP 安装：

```text
D:\Neo4j\neo4j-community-2026.08.1
D:\Neo4j\jdk-21.0.12.1+1
```

Neo4j 保存实体、关系和关系对应的 `evidence_chunk_ids`；SQLite 继续保存文档、版本、原文片段、任务和反馈。当前完整 DUTMed 图包含 27510 个实体和 328347 条关系。

安装时已经写入用户级 `JAVA_HOME`、`NEO4J_HOME`、`TRACEGRAPH_GRAPH_BACKEND=neo4j` 和连接变量。新开 PowerShell 后，前台启动 Neo4j：

```powershell
& "$env:NEO4J_HOME\bin\neo4j.bat" console
```

保持该窗口运行；按 `Ctrl+C` 停止。Neo4j Browser 位于 <http://localhost:7474>，Bolt 地址是 `bolt://localhost:7687`。

也可以在项目根目录一键启动 Neo4j 和 TraceGraph：

```powershell
.\start.ps1 -Mode neo4j
```

不带 `-Mode` 时读 `.env` 里的 `TRACEGRAPH_GRAPH_BACKEND`，都没有则用 SQLite。**只想用 SQLite 时可以不装 Java 和 Neo4j**：

```powershell
.\start.ps1 -Mode sqlite
```

`sqlite` 模式会跳过 `JAVA_HOME` / `NEO4J_HOME` / `NEO4J_PASSWORD` 校验与 Neo4j 启动，直接起 uvicorn。

按 `Ctrl+C` 停止当前运行；如果 Neo4j 仍在后台运行，可执行：

```powershell
.\stop-neo4j.ps1
```

如需重新导入图数据，在 Neo4j 已启动的情况下执行：

```powershell
tracegraph import-medical `
  --source ..\DUTMed\data\症状.json `
  --database data\local\tracegraph.db
```

导入器会批量替换每个疾病的出向关系，相同数据可安全重复执行。不要把本机 Neo4j 密码写入源码或提交到 Git。

然后启动完整本地应用：

```bash
python -m uvicorn tracegraph.bootstrap:app --reload --host 127.0.0.1 --port 8000
```

启动后可以访问：

- API 信息：<http://127.0.0.1:8000/>
- Web 界面：<http://127.0.0.1:8000/app>
- 健康检查：<http://127.0.0.1:8000/healthz>
- 当前组件：<http://127.0.0.1:8000/system>
- 交互式 API 文档：<http://127.0.0.1:8000/docs>

Web 界面顶部是状态栏（当前图后端、实际生效的生成器、模型名称、是否发生过降级、系统健康状态），左侧是知识问答与回答区，右侧是图谱浏览与文档导入。图谱浏览可以搜索疾病、症状、检查或药物，查看直接关系和每条关系对应的 DUTMed 原文位置。

提问区里有**模型选择器**和**一跳 / 二跳 / 三跳**选择器。模型清单来自 `GET /models`，默认选中服务端指定的默认模型；不可用的模型会显示原因并且选不中；清单读取失败时页面仍能提问——离线摘录是内置的，永远可选。查询进行中两个选择器都会禁用，避免发出与预期不符的请求。页面自始至终只显示模型 ID、显示名称和模型名称，不显示网关地址，也没有任何密钥输入框。

查询期间只有一个状态提示：「正在检索证据并组织回答」。这是后端真实在做的事，页面不再用定时器模拟「正在检索证据 / 正在分析图路径 / 正在组织回答」这类并不存在的阶段。回答回来后，回答区顶部显示**本次真正生效**的生成模型；发生降级时会在旁边给出明确但不夸张的警告。

多跳路径按 `A → 关系 → B ← 关系 ← C` 渲染，方向、中文关系名、总跳数和截断提示都会显示，每个关系都是可点击的，点击后展开该关系的 DUTMed 原文位置。回答区把「原文事实」和「图路径推导的关联」分成两块：推导关联用琥珀色标出，并写明「推导关联，不是当前实体的直接原文结论」。**推导关联只在它自己的区块里出现一次**，回答正文里不会重复一遍。

网页上的模型输出一律按纯文本渲染，不使用 `dangerouslySetInnerHTML`。

打开 Web 界面后可直接查询，例如“苯中毒有哪些症状”“百日咳需要做什么检查”。也可以通过 API 查询：

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question":"百日咳用什么药","limit":5,"max_hops":2}'
```

`max_hops` 默认 2，上限 3，越界返回 HTTP 400。响应的 `evidences[].graph_path` 是结构化对象（`nodes` / `steps` / `truncations`），`steps` 里每一步都带 `relation_id`、方向、目标实体和支持它的 Chunk ID；`derived_associations` 是与 `claims` 分开的推导关联区块。

按需拉取某条关系对应的 DUTMed 原文：

```bash
curl http://127.0.0.1:8000/graph/relations/{relation_id}/evidence
```

## 文档上传

网页右侧的「文档导入」面板可以直接把本地文档送进知识库：选择文件 → 上传 → 显示结果。上传后立刻就能提问，新入库的内容会参与关键词检索。

支持 **TXT、Markdown（.md）、JSON、JSONL、CSV**，以及**可以提取文本的 PDF**。浏览器支持什么格式无关紧要——判定在后端按扩展名做，不在这份清单里的扩展名返回 HTTP 400 与 `unsupported_document`。

### 普通上传的文档能做什么、不能做什么

> 普通上传文档会进入 SQLite 文本知识库，可参与关键词检索；当前不会自动生成 Neo4j 实体关系，因此不会自动参与图关系和多跳推导。

这是设计上的取舍，不是遗漏：实体和关系必须由领域适配器从结构化数据里抽取，凭空猜测一份自由文本里的疾病和药物，会污染图谱并让本来可追溯的关系变得不可信。**只有 DUTMed 专用导入（`tracegraph import-medical`）才会生成 Neo4j 图关系**，因为它读的是已经定义好 schema 的 JSONL。所以：上传的文档能被关键词检索召回，但不会出现在图谱浏览里，也不会成为多跳路径的一环。

### 大小与安全限制

- 单次上传上限默认 **10 MiB**，可用 `TRACEGRAPH_MAX_UPLOAD_MB=10` 调整；超限返回 HTTP 413 与 `file_too_large`。
- 前端会在选文件时先拦一次，**后端在解码后按真实字节数再拦一次**——前端检查只是省掉一次注定失败的往返，从不被信任。
- 文件名只作为来源名称保存，**不用于拼接服务器文件路径**，上传内容也不会被写到任意磁盘位置；切片和原文进的是 SQLite。
- **不支持扫描版 PDF**。PDF 解析只提取内嵌文本层，不做 OCR；扫描件没有文本层，会得到明确的提示，而不是一个空文档。

### 从接口上传

```bash
curl -X POST http://127.0.0.1:8000/ingestions/file \
  -H "Content-Type: application/json" \
  -d '{"filename":"科室须知.txt","content_base64":"5Zyo6L+Z6YeM5pS+5paH5qGj5q2j5paH"}'
```

返回 `job`（`status` 为 `succeeded` / `skipped` / `failed`）和 `document`（文档 ID、版本号、切片数）。内容哈希与已有版本一致时状态是 `skipped`，网页上提示「内容未变化，已跳过」，不会新建版本。异步任务用 `GET /ingestion-jobs/{job_id}` 轮询。

`tracegraph.bootstrap:app` 使用 `data/local/tracegraph.db` 保存文档，按 `TRACEGRAPH_GRAPH_BACKEND` 选择 Neo4j 或 SQLite 图仓库，并按 `TRACEGRAPH_MODELS_CONFIG` / `TRACEGRAPH_GENERATOR` 装配模型清单；直接使用 `tracegraph.api:app` 才是内存开发实例。

如果只想快速观察完整链路，可直接读取 DUTMed 的前三条真实记录并在内存中查询：

```bash
python examples/complete_demo.py
```

该示例没有自造医疗内容；`--limit` 只限制读取多少条真实 DUTMed 记录。可用 `--source` 指定其他 DUTMed 文件，用 `--query` 更换问题。

文档生命周期和入库流程说明见 [文档入库设计](docs/ingestion.md)。
文本与图检索的范围、评分方式和多跳遍历语义见 [文本检索设计](docs/retrieval.md)。
多跳的关键取舍与实测缺陷见 [ADR-0002：多跳图检索](docs/adr/0002-multi-hop-retrieval.md)。
离线评测方法见 [检索评测指南](docs/evaluation.md)。
整体数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)，领域扩展见 [DOMAIN_ADAPTER.md](DOMAIN_ADAPTER.md)，安全边界见 [SECURITY.md](SECURITY.md)。

## 前端开发与构建

前端是独立的 React + TypeScript + Vite 工程，位于 `frontend/`，不依赖任何 UI 组件库或状态管理库。一次性安装依赖：

```bash
npm --prefix frontend install
```

开发时前后端分开跑。Vite 把 API 请求代理给 FastAPI，因此**后端不需要开放 CORS**：

```powershell
# 终端一：后端
.\start.ps1 -Mode sqlite
```

```bash
# 终端二：前端开发服务器（默认 http://127.0.0.1:5173）
npm --prefix frontend run dev
```

交付时构建一次，之后 `/app` 直接打开构建产物：

```bash
npm --prefix frontend run build
```

产物在 `frontend/dist/`。刷新任意前端路由都不会 404——`/app/*` 一律回落到 `index.html`；`/docs`、`/openapi.json` 和全部既有 API 保持不变。**未构建时访问 `/app` 会返回 503 和 `frontend_unavailable`**，提示先执行构建，而不是显示过期页面。构建目录可用 `TRACEGRAPH_FRONTEND_DIST` 覆盖。

## 图后端与运维

配置了 Neo4j 但连不上时，应用会**直接失败**，而不是悄悄改用 SQLite —— 换后端可能换掉查询结果。只有显式设置才会降级：

```powershell
$env:TRACEGRAPH_GRAPH_FALLBACK = "sqlite"
```

降级发生时会在启动日志显著提示，并写入 `/system` 的 `graph_requested` / `graph_degraded` / `graph_detail` 三个字段。

```powershell
# 规模与类型分布、孤立实体数
tracegraph graph-stats

# 图侧证据引用是否都落在文档侧 chunks 上；配置了 Neo4j 时同时对比两库计数
tracegraph check-consistency

# 按当前图后端重跑导入，并对比前后统计
tracegraph resync-graph --source ..\DUTMed\data\症状.json
```

`check-consistency` 在计数不一致或发现悬空引用时以非零状态退出，可直接用于 CI 或定时任务。

## 核心回答状态

系统不保证每个问题都生成答案。核心契约明确区分：

- `answered`：证据支持正常回答；
- `insufficient_evidence`：证据不足；
- `conflicting_evidence`：证据冲突；
- `out_of_scope`：超出知识库范围；
- `emergency_escalation`：需要紧急升级处理；
- `system_error`：系统故障。

把拒答和升级处理建模成正常结果，上层应用就不必解析不可控的自由文本错误。

失败响应统一带一个机器可读的 `error_code`：`invalid_request`、`not_found`、`graph_unavailable`、`frontend_unavailable`、`generation_network_error`、`generation_response_error`、`internal_error`，以及模型与上传相关的 `invalid_generator`、`generator_unavailable`、`file_too_large`、`unsupported_document`。`detail` 始终是字符串，错误码作为同级字段追加，因此既有调用方不受影响。HTTP 500 只回一句固定文案，真实异常与堆栈留在服务端日志里。

网页把错误码翻成可以行动的提示，而不是把 HTTP 状态码摆给用户看。错误信息里始终只有变量名，没有变量取值——配置报错不会顺带泄露密钥。

## 开发里程碑

1. M0：仓库基线与核心契约；
2. M1：文档与证据生命周期；
3. M2：图检索与文本检索；
4. M3：基于证据的生成与拒答；
5. M4：DUTMed 医疗领域适配器；
6. M5：用户反馈与回归评测；
7. M6：可观测性、故障测试与演示完善。

## 项目边界

TraceGraph 不是低代码工作流平台、通用多智能体框架或医学诊断系统。初始架构决策见 [ADR-0001](docs/adr/0001-project-boundary.md)，多跳检索的取舍见 [ADR-0002](docs/adr/0002-multi-hop-retrieval.md)。

多跳结果是**关联线索**，不是本实体的原文结论：它们不进入 `Claim`，在界面上单独分区，并且只有存在一跳原文事实时才会给出回答。

## 开源许可证

项目尚未选择开源许可证。在添加许可证文件前，仓库源代码默认不授予他人复用权利。
