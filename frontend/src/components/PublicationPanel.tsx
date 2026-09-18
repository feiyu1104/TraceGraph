import type { ApiRequestError } from '../api/client'
import type { PublicationResult } from '../api/types'
import ErrorState from './ErrorState'

export interface PublishPreview {
  /** run 表示按抽取任务发布，candidates 表示按显式列出的候选发布。 */
  mode: 'run' | 'candidates'
  entityCount: number
  relationCount: number
  /** 确认面板里如实说明这次发布覆盖的范围。 */
  scope: string
}

interface PublicationPanelProps {
  workspaceName: string
  preview: PublishPreview
  /** 图存储是否可用；不可用时整块只说明原因，不显示发布按钮。 */
  graphAvailable: boolean
  confirming: boolean
  busy: boolean
  error: ApiRequestError | null
  result: PublicationResult | null
  onRequest: () => void
  onCancel: () => void
  onConfirm: () => void
}

export default function PublicationPanel({
  workspaceName,
  preview,
  graphAvailable,
  confirming,
  busy,
  error,
  result,
  onRequest,
  onCancel,
  onConfirm,
}: PublicationPanelProps) {
  const publishable = preview.entityCount + preview.relationCount

  return (
    <div className="publication">
      <h3 className="block__title">发布到图谱</h3>
      <p className="meta">
        只有已批准且尚未发布的候选会进入发布。发布是显式动作：审核通过不会自动发布，
        抽取完成也不会自动发布。
      </p>

      {!graphAvailable && (
        <p className="publication__notice publication__notice--warn">
          当前服务未启用图存储，暂时无法发布。可以在启动服务时选择 SQLite 或 Neo4j 图后端。
        </p>
      )}

      <p className="publication__counts">
        本次可发布：实体 <strong>{preview.entityCount}</strong> 条 · 关系{' '}
        <strong>{preview.relationCount}</strong> 条
      </p>
      <p className="meta">范围：{preview.scope}</p>

      {graphAvailable && !confirming && (
        <div className="publication__actions">
          <button
            type="button"
            className="button button--primary"
            onClick={onRequest}
            disabled={busy || publishable === 0}
          >
            发布到图谱
          </button>
          {publishable === 0 && (
            <span className="meta">
              还没有可发布的候选：请先批准候选实体与关系（关系两端也必须已批准）。
            </span>
          )}
        </div>
      )}

      {confirming && (
        <div className="confirm" role="group" aria-label="发布确认">
          <p className="confirm__title">确认发布到「{workspaceName}」的图谱？</p>
          <dl className="metrics">
            <div>
              <dt>知识库</dt>
              <dd>{workspaceName}</dd>
            </div>
            <div>
              <dt>实体</dt>
              <dd>{preview.entityCount}</dd>
            </div>
            <div>
              <dt>关系</dt>
              <dd>{preview.relationCount}</dd>
            </div>
            <div>
              <dt>抽取任务</dt>
              <dd>{preview.scope}</dd>
            </div>
          </dl>
          <p className="meta">
            同名的实体与关系会被复用而不是重复新建；已经发布过的部分会被跳过。
            撤销发布尚未实现，请确认无误后再继续。
          </p>
          <div className="confirm__actions">
            <button
              type="button"
              className="button button--primary"
              onClick={onConfirm}
              disabled={busy}
            >
              {busy ? '发布中…' : '确认发布'}
            </button>
            <button type="button" className="button" onClick={onCancel} disabled={busy}>
              取消
            </button>
          </div>
        </div>
      )}

      {error && <ErrorState error={error} />}

      {result && (
        <div className="publication__result" role="status">
          <p className="publication__result-title">
            已发布到「{workspaceName}」的图谱（后端 {result.backend}）。
          </p>
          <dl className="metrics">
            <div>
              <dt>新建实体</dt>
              <dd>{result.counts.entities_created}</dd>
            </div>
            <div>
              <dt>复用实体</dt>
              <dd>{result.counts.entities_reused}</dd>
            </div>
            <div>
              <dt>跳过实体</dt>
              <dd>{result.counts.entities_skipped}</dd>
            </div>
            <div>
              <dt>新建关系</dt>
              <dd>{result.counts.relations_created}</dd>
            </div>
            <div>
              <dt>复用关系</dt>
              <dd>{result.counts.relations_reused}</dd>
            </div>
            <div>
              <dt>跳过关系</dt>
              <dd>{result.counts.relations_skipped}</dd>
            </div>
          </dl>
          <p className="meta">
            候选列表、文档统计与图谱浏览都已重新加载；可以在「图谱浏览」里查这些实体与关系。
          </p>
        </div>
      )}
    </div>
  )
}
