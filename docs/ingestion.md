# 文档入库设计

## 目标

入库阶段的目标不是尽快把一段文本塞入数据库，而是建立一条可以重复执行和追踪来源的数据链：

```text
Document
  └─ DocumentVersion
       └─ Chunk
```

当前实现支持 TXT、Markdown、JSON、JSONL、CSV 和带文本层的 PDF。它提供内存存储用于快速演示，也提供 SQLite 存储用于本地持久化，并可通过 HTTP API 提交文本、Base64 文件和查询任务。

## 四个核心对象

### Document

代表逻辑上的一份文档，例如《高血压指南》。它保存稳定 ID、来源名称和媒体类型，不直接保存某一次具体内容。

### DocumentVersion

代表文档某一版内容。系统对 UTF-8 内容计算 SHA-256：

- 同一来源、同一哈希：认为是重复导入，不创建新版本；
- 同一来源、不同哈希：版本号递增；
- 不同来源：属于不同 Document。

当前以 `source_name` 识别逻辑文档。接入真实上传接口时，需要由知识库 ID 和业务文档 ID 共同确定身份，不能只依赖文件名。

### Chunk

代表可以被检索和引用的最小原文片段。每个 Chunk 保存：

- 所属文档 ID；
- 所属版本 ID；
- 版本内顺序；
- 原文内容；
- 标题路径形式的定位信息。

例如二级标题“检查”位于一级标题“高血压”下，其定位为：

```text
高血压 > 检查
```

以后加入 PDF 解析器时，定位信息可以改为页码和标题组合，但核心 Chunk 契约不需要改变。

### IngestionJob

记录一次入库尝试的结果。当前已经定义以下状态：

- `pending`：等待处理；
- `processing`：处理中；
- `succeeded`：成功创建新版本；
- `failed`：处理失败；
- `skipped`：内容与已有版本相同，跳过重复处理。

目前同步入库只会产生 `succeeded` 或 `skipped`，两种任务都会保存并可按 ID 查询。其他状态将在异步任务阶段接入。

## 一次入库的执行过程

```text
校验扩展名并解析正文切片
  → 计算内容 SHA-256
  → 查找或创建 Document
  → 查找相同哈希的已有版本
      ├─ 已存在：返回 skipped 和已有 Chunk
      └─ 不存在：创建 DocumentVersion
                   → 创建稳定 Chunk ID
                   → 一次性写入版本、Chunk 和任务记录
                   → 返回 succeeded
```

稳定 ID 来自相关业务字段的 SHA-256 摘要，而入库任务 ID 使用随机 UUID。这意味着文档、版本和 Chunk 可以被重复计算，任务仍能区分每一次尝试。

## 存储实现

`TextIngestionService` 依赖的是 `DocumentRepository` 接口，而不是具体数据库。目前有两种实现：

- `InMemoryDocumentRepository`：不需要外部服务，适合单元测试和一次性示例；
- `SQLiteDocumentRepository`：将文档、版本、Chunk 和入库任务保存到本地数据库，适合开发环境。

SQLite 实现将版本、全部 Chunk 和成功任务放在同一个事务中写入。如果任一记录违反约束，整次写入都会回滚；重复内容产生的 `skipped` 任务也会单独保存。它同时通过唯一约束保护同一文档的版本号和内容哈希。Neo4j 仍留给实体关系和图检索阶段，不承担这部分关系型元数据的首个持久化基线。

## HTTP 接口

`POST /ingestions` 接收 `source_name` 和 `content` 两个 JSON 字段，返回文档、版本、Chunk 摘要和任务信息。`GET /ingestion-jobs/{job_id}` 返回一次入库任务的状态。当前处理是同步的，接口主要用于固定请求和响应契约；异步队列尚未接入。

`POST /ingestions/file` 接收文件名和 Base64 内容。JSON 与 JSONL 会转换为标题化文本，CSV 按记录转换，PDF 按页建立定位。扫描 PDF 没有文本层，当前需要先在外部完成 OCR。

扩展名白名单在服务端判定：不支持的后缀返回 HTTP 400 与 `unsupported_document`。单次上传默认上限 10 MiB（`TRACEGRAPH_MAX_UPLOAD_MB`），超出返回 HTTP 413 与 `file_too_large`。文件名只作来源名称保存，不参与服务器路径拼接。

`DELETE /documents/{document_id}` 会删除全部版本、Chunk 和任务，并从图关系中移除对应 Chunk ID；没有剩余证据的关系会被删除。

## 当前限制

- 长段落按字符数硬切分，尚未使用语义切片；
- 文档身份暂时依赖来源名称；
- 已保存创建时间和 Workspace 归属，但尚未引入用户身份，因此没有操作者审计字段；
- PDF 仅提取文本层，不执行 OCR 或图片理解；
- 普通自由文本不会自动写入图谱；用户可显式启动模型抽取，审核带原文证据的候选后再发布；DUTMed 结构化 JSON 仍可使用确定性映射；
- API 尚未支持 multipart 文件上传和异步任务。

这些限制是有意保留的：先让版本和证据链正确，再逐步增加解析与索引能力。
