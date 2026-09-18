import { ApiRequestError } from '../api/client'

// 后端已经用 error_code 区分了失败原因，这里把码翻成用户能行动的提示，
// 而不是把 HTTP 状态码直接摆给用户看。
const HINTS: Record<string, string> = {
  network_unreachable: '后端服务没有响应，请确认它已经启动。',
  graph_unavailable: '图存储当前不可用，换个后端或先启动 Neo4j 再试。',
  frontend_unavailable: '前端尚未构建，请先执行 npm --prefix frontend run build。',
  invalid_request: '请求参数不合法，请调整后重试。',
  internal_error: '服务端出现未预期的错误，详情见服务端日志。',
  invalid_generator: '这个模型 ID 不在服务端清单里，请重新选择回答模型。',
  generator_unavailable:
    '所选模型当前不可用（通常是缺少对应的环境变量），请换一个模型或改用离线摘录。',
  file_too_large: '文件超过服务端允许的大小，请拆分后重试。',
  invalid_adapter: '这个适配器 ID 不在服务端注册表里，请从清单里重新选择。',
  workspace_adapter_unavailable:
    '该知识库记录的适配器当前不可用（不在服务端注册表里），请换一个知识库；适配器不能由前端改写。',
  workspace_not_found: '这个知识库不存在，请重新选择。',
  unsupported_document: '当前不支持这种文档格式，请改用 TXT、Markdown、JSON、JSONL、CSV 或文本型 PDF。',
  extraction_failed:
    '这次知识抽取没有跑完，失败原因见上方信息。可以检查文档内容后重新抽取，或换一个模型再试。',
  extraction_model_unsupported:
    '所选模型不能用于知识抽取，请改选离线摘录或另一个支持抽取的模型。',
  candidate_workspace_mismatch:
    '这条候选不属于当前知识库，列表可能已经过期，请刷新候选列表后重试。',
  candidate_content_conflict:
    '改动与现有候选冲突：类型必须在当前适配器的类型清单里，两端不能相同，端点必须来自同一次抽取。',
  illegal_candidate_transition:
    '这个审核操作在当前状态下不允许；已发布或状态不对的候选不能这样改，请刷新后重试。',
  candidate_already_published:
    '候选已经发布到图谱，撤销发布尚未实现，因此不能再修改或退回。',
  candidate_not_approved: '只有已批准的候选可以发布，请先批准这几条候选。',
  candidate_evidence_missing:
    '候选的原文证据已经找不到（文档或切片可能被删除），发布被拒绝。请重新抽取。',
  publication_conflict:
    '关系的端点还没有进入图谱。请把端点实体连同这条关系一起批准，并在同一次发布里提交。',
}

const GENERATION_HINTS: Record<string, string> = {
  generation_network_error:
    '模型服务连接失败。请检查 TRACEGRAPH_LLM_BASE_URL、网络与超时设置；若希望离线可用，把 TRACEGRAPH_LLM_FALLBACK 设为 extractive。',
  generation_response_error:
    '模型返回的内容没有通过证据校验，本次结果已被丢弃。可重试或改用离线摘录生成器。',
  generation_configuration_error: '模型配置不完整，请检查 .env 中的 LLM 变量。',
}

interface ErrorStateProps {
  error: ApiRequestError
  onRetry?: () => void
}

export default function ErrorState({ error, onRetry }: ErrorStateProps) {
  const hint = GENERATION_HINTS[error.errorCode] ?? HINTS[error.errorCode]

  return (
    <div className="error" role="alert">
      <p className="error__title">{error.message}</p>
      {hint && <p className="error__hint">{hint}</p>}
      <p className="error__code">错误码：{error.errorCode}</p>
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          重试
        </button>
      )}
    </div>
  )
}
