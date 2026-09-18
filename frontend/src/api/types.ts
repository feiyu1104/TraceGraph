// 与 FastAPI 响应一一对应。后端只增字段不改含义，这里也按同样的约定维护。

export type AnswerStatus =
  | 'answered'
  | 'insufficient_evidence'
  | 'conflicting_evidence'
  | 'out_of_scope'
  | 'emergency_escalation'
  | 'system_error'

export type StepDirection = 'outgoing' | 'incoming'

export interface Entity {
  id: string
  name: string
  type: string
}

export interface PathStep {
  relation_id: string
  /** 关系类型常量，例如 RECOMMENDS_DRUG */
  type: string
  /** 中文关系名，由后端统一定义，前端直接渲染 */
  label: string
  direction: StepDirection
  target: Entity
  evidence_chunk_ids: string[]
}

export interface PathTruncation {
  entity_id: string
  total_edges: number
  shown_edges: number
}

export interface GraphPath {
  nodes: Entity[]
  steps: PathStep[]
  truncations: PathTruncation[]
}

export interface Evidence {
  id: string
  content: string
  document_id: string
  document_version: string
  source_name: string
  locator: string
  chunk_id: string
  retrieval_method: string
  retrieval_score: number
  graph_path: GraphPath | null
}

export interface Claim {
  text: string
  evidence_ids: string[]
}

export interface AnswerMetrics {
  evidence_count?: number
  derived_association_count?: number
  max_hops?: number
  /** 本次回答所用的知识库；服务端从请求里带过来，不是前端猜的 */
  workspace_id?: string
  /** 该知识库记录的领域适配器 ID，由服务端按 Workspace 解析 */
  adapter_id?: string
  /** 实际使用的检索链路：hybrid（关键词 + 图）或 keyword */
  retriever?: string
  /** 本次请求指定的模型 ID；未指定时是服务端默认模型的 ID */
  requested_generator?: string
  /** 实际产出答案的生成器 ID（降级时与 requested_generator 不同） */
  generator?: string
  /** 生成器背后的模型名；离线摘录为空串 */
  model?: string
  generation_degraded?: boolean
}

export interface Answer {
  status: AnswerStatus
  text: string | null
  error_code: string | null
  claims: Claim[]
  derived_associations: Claim[]
  evidences: Evidence[]
  warnings: string[]
  metrics: AnswerMetrics
}

export interface SystemInfo {
  version: string
  document_backend: string
  graph_backend: string
  retriever: string
  domain: string
  generator: string
  graph_requested?: string
  graph_degraded?: string
  graph_detail?: string
  llm_configured?: string
  llm_model?: string
  llm_fallback?: string
  /** 服务端生效的上传大小上限（字节）。前端只用它做上传前预检，后端仍会复检。 */
  max_upload_bytes?: number
}

export interface RelationEvidenceChunk {
  chunk_id: string
  content: string
  document_id: string
  document_version_id: string
  source_name: string
  locator: string
}

export interface RelationPublicationSource {
  candidate_relation_id: string
  extraction_run_id: string
  workspace_id: string
  document_id: string
  document_version_id: string
  evidence_chunk_ids: string[]
  published_at: string
}

export interface RelationEvidence {
  relation: {
    id: string
    type: string
    source: Entity | null
    target: Entity | null
  }
  evidence: RelationEvidenceChunk[]
  sources: RelationPublicationSource[]
}

export interface RelationEvidenceRef {
  chunk_id: string
  source_name: string
  locator: string
}

export interface EntityRelation {
  id: string
  type: string
  label: string
  direction: StepDirection
  source: Entity | null
  target: Entity | null
  evidence: RelationEvidenceRef[]
}

export interface EntityRelations {
  entity: Entity
  relations: EntityRelation[]
}

export interface EntitySearchResult {
  query: string
  entities: Entity[]
}

/** 后端刻意不返回 base_url 与任何凭证，这里也就不存在这些字段。 */
export interface ModelInfo {
  id: string
  label: string
  model: string
  available: boolean
  kind: string
  reason: string
}

export interface ModelsResponse {
  default: string
  models: ModelInfo[]
}

/** 一个知识库。adapter_id 只能由服务端在建库时写入，前端没有修改入口。 */
export interface WorkspaceInfo {
  id: string
  name: string
  adapter_id: string
  created_at: string
}

export interface WorkspacesResponse {
  workspaces: WorkspaceInfo[]
}

/** 服务端内置的领域适配器。清单里不含提示词、路径或任何密钥。 */
export interface AdapterInfo {
  id: string
  label: string
  description: string
  version: string
  entity_types: string[]
  relation_types: string[]
  builtin: boolean
}

export interface AdaptersResponse {
  adapters: AdapterInfo[]
}

export type IngestionStatus = 'pending' | 'succeeded' | 'skipped' | 'failed'

export interface IngestionJob {
  id: string
  document_id: string
  status: IngestionStatus
  processed_chunks: number
  error: string | null
}

export interface IngestionResult {
  job: IngestionJob
  document: { id: string; source_name: string; media_type: string }
  version: { id: string; number: number; content_sha256: string }
  chunks: { id: string; index: number; locator: string }[]
}

/** 文档列表接口给出的候选计数；`total` 是三种审核状态之和，不含 published。 */
export interface DocumentCandidateTally {
  pending: number
  approved: number
  rejected: number
  published: number
  total: number
}

export interface WorkspaceDocument {
  id: string
  source_name: string
  media_type: string
  created_at: string | null
  updated_at: string | null
  latest_version_id: string | null
  latest_version_created_at: string | null
  chunk_count: number
  /** 有过抽取任务即为 true，哪怕任务失败或一条候选都没产出。 */
  has_extraction_runs: boolean
  candidates: DocumentCandidateTally
}

export interface WorkspaceDocumentsResponse {
  workspace_id: string
  documents: WorkspaceDocument[]
}

export type ExtractionStatus = 'pending' | 'running' | 'succeeded' | 'failed'

export interface ExtractionRun {
  id: string
  workspace_id: string
  document_id: string
  document_version_id: string
  adapter_id: string
  model_id: string
  status: ExtractionStatus
  entity_count: number
  relation_count: number
  error: string | null
  created_at: string
  updated_at: string
}

export interface ExtractionRunsResponse {
  workspace_id: string
  runs: ExtractionRun[]
}

/** 候选的一条证据：真实 Chunk 的原文与它在文档里的位置。 */
export interface CandidateEvidenceChunk {
  chunk_id: string
  content: string
  source_name: string
  locator: string
}

export type CandidateStatus = 'pending' | 'approved' | 'rejected'

export interface CandidateEntity {
  id: string
  extraction_run_id: string
  workspace_id: string
  document_id: string
  document_version_id: string
  adapter_id: string
  name: string
  type: string
  status: CandidateStatus
  is_published: boolean
  published_at: string | null
  graph_id: string | null
  evidence: CandidateEvidenceChunk[]
  created_at: string
  updated_at: string
}

export interface CandidateRelation {
  id: string
  extraction_run_id: string
  workspace_id: string
  document_id: string
  document_version_id: string
  adapter_id: string
  source_entity_id: string
  target_entity_id: string
  type: string
  status: CandidateStatus
  is_published: boolean
  published_at: string | null
  graph_id: string | null
  evidence: CandidateEvidenceChunk[]
  created_at: string
  updated_at: string
}

/** 文档级候选清单：一次给出该文档全部抽取任务的候选。 */
export interface WorkspaceCandidatesResponse {
  workspace_id: string
  entities: CandidateEntity[]
  relations: CandidateRelation[]
}

/** 单次抽取任务的候选清单，附带任务本身。 */
export interface RunCandidatesResponse {
  run: ExtractionRun
  entities: CandidateEntity[]
  relations: CandidateRelation[]
}

export interface BatchReviewResponse {
  workspace_id: string
  status: CandidateStatus
  entities: CandidateEntity[]
  relations: CandidateRelation[]
}

/** 发布结果：新建、复用、幂等跳过各有多少。 */
export interface PublicationCounts {
  entities_created: number
  entities_reused: number
  entities_skipped: number
  relations_created: number
  relations_reused: number
  relations_skipped: number
}

export interface PublicationResult {
  workspace_id: string
  backend: string
  counts: PublicationCounts
  created_entity_ids: string[]
  reused_entity_ids: string[]
  skipped_entity_ids: string[]
  created_relation_ids: string[]
  reused_relation_ids: string[]
  skipped_relation_ids: string[]
}
