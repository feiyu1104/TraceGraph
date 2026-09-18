import type {
  AdaptersResponse,
  Answer,
  BatchReviewResponse,
  CandidateEntity,
  CandidateRelation,
  CandidateStatus,
  EntityRelations,
  EntitySearchResult,
  ExtractionRun,
  ExtractionRunsResponse,
  IngestionResult,
  ModelConnection,
  ModelConnectionInput,
  ModelConnectionsResponse,
  ModelDiscoveryResponse,
  ModelsResponse,
  PublicationResult,
  RelationEvidence,
  RunCandidatesResponse,
  SystemInfo,
  WorkspaceCandidatesResponse,
  WorkspaceCustomTypes,
  WorkspaceDocumentsResponse,
  WorkspaceInfo,
  WorkspacesResponse,
} from './types'

export const SUPPORTED_UPLOAD_SUFFIXES = [
  '.txt',
  '.md',
  '.json',
  '.jsonl',
  '.csv',
  '.pdf',
]

// 后端错误一律带 error_code（见 tracegraph/api.py 的 ApiError）。
// 这里把它原样带出来，界面按码给不同提示，而不是只能显示一句话。
export class ApiRequestError extends Error {
  readonly status: number
  readonly errorCode: string

  constructor(status: number, errorCode: string, detail: string) {
    super(detail)
    this.name = 'ApiRequestError'
    this.status = status
    this.errorCode = errorCode
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch {
    throw new ApiRequestError(
      0,
      'network_unreachable',
      '无法连接后端服务，请确认 FastAPI 已在 127.0.0.1:8000 运行。',
    )
  }

  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const body = payload as { detail?: unknown; error_code?: unknown } | null
    const detail =
      typeof body?.detail === 'string' && body.detail
        ? body.detail
        : `请求失败（HTTP ${response.status}）`
    const code = typeof body?.error_code === 'string' ? body.error_code : 'unknown_error'
    throw new ApiRequestError(response.status, code, detail)
  }
  return payload as T
}

function postJson(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

function patchJson(body: unknown): RequestInit {
  return {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

function putJson(body: unknown): RequestInit {
  return {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export function fetchSystem(): Promise<SystemInfo> {
  return request<SystemInfo>('/system')
}

export function fetchHealth(): Promise<{ status: string; version: string }> {
  return request<{ status: string; version: string }>('/healthz')
}

/**
 * workspaceId 是必填参数：服务端有默认值，但页面必须每次都把当前选中的
 * 知识库显式带上，否则「切换到别的知识库后仍查出默认库的内容」这类问题
 * 只会在运行时才暴露出来。
 */
export function askQuestion(
  question: string,
  workspaceId: string,
  maxHops: number,
  generatorId?: string,
  limit = 8,
): Promise<Answer> {
  return request<Answer>(
    '/query',
    postJson({
      question,
      workspace_id: workspaceId,
      max_hops: maxHops,
      limit,
      // 不传就是服务端默认模型；页面永远不参与决定「用哪个网关」。
      generator_id: generatorId || null,
    }),
  )
}

export function fetchModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>('/models')
}

export function fetchModelConnections(): Promise<ModelConnectionsResponse> {
  return request<ModelConnectionsResponse>('/model-connections')
}

export function discoverModels(
  baseUrl: string,
  apiKey: string,
  timeout: number,
): Promise<ModelDiscoveryResponse> {
  return request<ModelDiscoveryResponse>(
    '/model-connections/discover',
    postJson({ base_url: baseUrl, api_key: apiKey, timeout }),
  )
}

export function discoverConnectionModels(connectionId: string): Promise<ModelDiscoveryResponse> {
  return request<ModelDiscoveryResponse>(
    `/model-connections/${encodeURIComponent(connectionId)}/discover`,
    postJson({}),
  )
}

export function saveModelConnection(
  connectionId: string,
  input: ModelConnectionInput,
): Promise<ModelConnection> {
  return request<ModelConnection>(
    `/model-connections/${encodeURIComponent(connectionId)}`,
    putJson(input),
  )
}

export function deleteModelConnection(
  connectionId: string,
): Promise<{ id: string; default: string }> {
  return request<{ id: string; default: string }>(
    `/model-connections/${encodeURIComponent(connectionId)}`,
    { method: 'DELETE' },
  )
}

export function setDefaultModel(modelId: string): Promise<ModelsResponse> {
  return request<ModelsResponse>('/models/default', putJson({ model_id: modelId }))
}

export function fetchWorkspaces(): Promise<WorkspacesResponse> {
  return request<WorkspacesResponse>('/workspaces')
}

export function fetchAdapters(): Promise<AdaptersResponse> {
  return request<AdaptersResponse>('/adapters')
}

/**
 * 建库提交名称、内置适配器 ID，以及可选的自定义抽取类型。
 *
 * customTypes 里为 null 的项表示沿用适配器内置清单；整个不传同理。类型清单
 * 由服务端决定，前端只能覆盖这一个知识库要用的那一份。
 */
export function createWorkspace(
  name: string,
  adapterId: string,
  customTypes?: WorkspaceCustomTypes,
): Promise<WorkspaceInfo> {
  const body: Record<string, unknown> = { name, adapter_id: adapterId }
  if (customTypes) body.custom_types = customTypes
  return request<WorkspaceInfo>('/workspaces', postJson(body))
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('浏览器无法读取该文件'))
    reader.onload = () => {
      const result = reader.result
      if (typeof result !== 'string') {
        reject(new Error('浏览器无法读取该文件'))
        return
      }
      // readAsDataURL 的结果形如 "data:<mime>;base64,<内容>"。
      const separator = result.indexOf(',')
      resolve(separator >= 0 ? result.slice(separator + 1) : '')
    }
    reader.readAsDataURL(file)
  })
}

export async function uploadDocument(
  file: File,
  workspaceId: string,
): Promise<IngestionResult> {
  const contentBase64 = await readAsBase64(file)
  return request<IngestionResult>(
    '/ingestions/file',
    postJson({
      filename: file.name,
      content_base64: contentBase64,
      workspace_id: workspaceId,
    }),
  )
}

export function searchEntities(
  query: string,
  workspaceId: string,
  limit = 10,
): Promise<EntitySearchResult> {
  const params = new URLSearchParams({
    query,
    workspace_id: workspaceId,
    limit: String(limit),
  })
  return request<EntitySearchResult>(`/graph/entities?${params}`)
}

export function fetchEntityRelations(
  entityId: string,
  workspaceId: string,
  limit = 30,
): Promise<EntityRelations> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    limit: String(limit),
  })
  return request<EntityRelations>(
    `/graph/entities/${encodeURIComponent(entityId)}/relations?${params}`,
  )
}

export function fetchRelationEvidence(
  relationId: string,
  workspaceId: string,
): Promise<RelationEvidence> {
  const params = new URLSearchParams({ workspace_id: workspaceId })
  return request<RelationEvidence>(
    `/graph/relations/${encodeURIComponent(relationId)}/evidence?${params}`,
  )
}

// ---------------------------------------------------------------------------
// 文档与知识工作台。每个请求都显式带上 workspace_id：服务端虽然有默认值，
// 但页面从不依赖它 —— 少带一次就会静默地读到另一个知识库的数据。
// ---------------------------------------------------------------------------

export function fetchWorkspaceDocuments(
  workspaceId: string,
): Promise<WorkspaceDocumentsResponse> {
  return request<WorkspaceDocumentsResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/documents`,
  )
}

export function fetchExtractionRuns(
  workspaceId: string,
  documentId?: string,
): Promise<ExtractionRunsResponse> {
  const params = new URLSearchParams()
  if (documentId) params.set('document_id', documentId)
  const query = params.toString()
  return request<ExtractionRunsResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/extractions${query ? `?${query}` : ''}`,
  )
}

export function startExtraction(
  workspaceId: string,
  documentId: string,
  modelId: string,
): Promise<ExtractionRun> {
  return request<ExtractionRun>(
    '/extractions',
    postJson({
      workspace_id: workspaceId,
      document_id: documentId,
      // 空串表示交给服务端默认模型。
      model_id: modelId || null,
    }),
  )
}

export function fetchExtractionRun(runId: string): Promise<ExtractionRun> {
  return request<ExtractionRun>(`/extractions/${encodeURIComponent(runId)}`)
}

export function fetchRunCandidates(runId: string): Promise<RunCandidatesResponse> {
  return request<RunCandidatesResponse>(
    `/extractions/${encodeURIComponent(runId)}/candidates`,
  )
}

export function fetchWorkspaceCandidates(
  workspaceId: string,
  documentId: string,
): Promise<WorkspaceCandidatesResponse> {
  const params = new URLSearchParams({ document_id: documentId })
  return request<WorkspaceCandidatesResponse>(
    `/workspaces/${encodeURIComponent(workspaceId)}/candidates?${params}`,
  )
}

/** 候选实体的单条审核与内容修正；只提交真正要改的字段。 */
export function reviewCandidateEntity(
  candidateId: string,
  workspaceId: string,
  changes: { status?: CandidateStatus; name?: string; type?: string },
): Promise<CandidateEntity> {
  return request<CandidateEntity>(
    `/candidate-entities/${encodeURIComponent(candidateId)}`,
    patchJson({ workspace_id: workspaceId, ...changes }),
  )
}

/** 候选关系的单条审核与内容修正；只提交真正要改的字段。 */
export function reviewCandidateRelation(
  candidateId: string,
  workspaceId: string,
  changes: {
    status?: CandidateStatus
    source_entity_id?: string
    target_entity_id?: string
    type?: string
  },
): Promise<CandidateRelation> {
  return request<CandidateRelation>(
    `/candidate-relations/${encodeURIComponent(candidateId)}`,
    patchJson({ workspace_id: workspaceId, ...changes }),
  )
}

export function batchReviewCandidates(
  workspaceId: string,
  status: CandidateStatus,
  entityIds: string[],
  relationIds: string[],
): Promise<BatchReviewResponse> {
  return request<BatchReviewResponse>(
    '/candidates/batch-review',
    postJson({
      workspace_id: workspaceId,
      status,
      entity_ids: entityIds,
      relation_ids: relationIds,
    }),
  )
}

/**
 * 发布到当前 Workspace 的图谱。两种指法二选一：给 extraction_run_id 发布
 * 该次抽取里全部已批准的候选，否则发布显式列出的候选 ID。
 */
export type PublicationTarget =
  | { extraction_run_id: string }
  | { candidate_entity_ids: string[]; candidate_relation_ids: string[] }

export function publishGraph(
  workspaceId: string,
  target: PublicationTarget,
): Promise<PublicationResult> {
  return request<PublicationResult>(
    `/workspaces/${encodeURIComponent(workspaceId)}/graph-publications`,
    postJson(target),
  )
}
