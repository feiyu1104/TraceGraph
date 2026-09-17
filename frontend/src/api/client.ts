import type {
  AdaptersResponse,
  Answer,
  EntityRelations,
  EntitySearchResult,
  IngestionResult,
  ModelsResponse,
  RelationEvidence,
  SystemInfo,
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

export function fetchWorkspaces(): Promise<WorkspacesResponse> {
  return request<WorkspacesResponse>('/workspaces')
}

export function fetchAdapters(): Promise<AdaptersResponse> {
  return request<AdaptersResponse>('/adapters')
}

/** 建库只提交名称与内置适配器 ID；适配器清单由服务端决定。 */
export function createWorkspace(
  name: string,
  adapterId: string,
): Promise<WorkspaceInfo> {
  return request<WorkspaceInfo>(
    '/workspaces',
    postJson({ name, adapter_id: adapterId }),
  )
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

export function searchEntities(query: string, limit = 10): Promise<EntitySearchResult> {
  const params = new URLSearchParams({ query, limit: String(limit) })
  return request<EntitySearchResult>(`/graph/entities?${params}`)
}

export function fetchEntityRelations(
  entityId: string,
  limit = 30,
): Promise<EntityRelations> {
  const params = new URLSearchParams({ limit: String(limit) })
  return request<EntityRelations>(
    `/graph/entities/${encodeURIComponent(entityId)}/relations?${params}`,
  )
}

export function fetchRelationEvidence(relationId: string): Promise<RelationEvidence> {
  return request<RelationEvidence>(
    `/graph/relations/${encodeURIComponent(relationId)}/evidence`,
  )
}
