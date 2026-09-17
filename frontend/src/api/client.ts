import type {
  Answer,
  EntityRelations,
  EntitySearchResult,
  IngestionResult,
  ModelsResponse,
  RelationEvidence,
  SystemInfo,
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

export function askQuestion(
  question: string,
  maxHops: number,
  generatorId?: string,
  limit = 8,
): Promise<Answer> {
  return request<Answer>(
    '/query',
    postJson({
      question,
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

export async function uploadDocument(file: File): Promise<IngestionResult> {
  const contentBase64 = await readAsBase64(file)
  return request<IngestionResult>(
    '/ingestions/file',
    postJson({ filename: file.name, content_base64: contentBase64 }),
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
