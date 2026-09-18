import type { ApiRequestError } from '../api/client'
import type { WorkspaceDocument } from '../api/types'
import EmptyState from './EmptyState'
import ErrorState from './ErrorState'

/** 长 ID 只显示前 12 位；完整值留在 title 里，不编造更好看的名字。 */
function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

interface DocumentListProps {
  documents: WorkspaceDocument[] | null
  selectedId: string | null
  onSelect: (documentId: string) => void
  /** 首次加载中；此时列表还什么都没有。 */
  loading: boolean
  /** 已有数据时的重新加载，按钮显示忙碌状态即可。 */
  refreshing: boolean
  error: ApiRequestError | null
  onRefresh: () => void
}

export default function DocumentList({
  documents,
  selectedId,
  onSelect,
  loading,
  refreshing,
  error,
  onRefresh,
}: DocumentListProps) {
  return (
    <div className="doclist">
      <div className="doclist__head">
        <h3 className="block__title">当前知识库的文档</h3>
        <button
          type="button"
          className="button button--ghost"
          onClick={onRefresh}
          disabled={refreshing || loading}
        >
          {refreshing ? '刷新中…' : '刷新'}
        </button>
      </div>

      {/* 出错时保留已经加载到的清单：一次刷新失败不该把页面清空。 */}
      {error && <ErrorState error={error} onRetry={onRefresh} />}

      {loading && documents === null && <p className="meta">正在读取文档清单…</p>}

      {documents !== null && documents.length === 0 && !error && (
        <EmptyState
          title="这个知识库还没有文档"
          hint="用上面的「文档导入」上传一份 TXT、Markdown、JSON、JSONL、CSV 或文本型 PDF；入库后就能在这里启动知识抽取。"
        />
      )}

      {documents !== null && documents.length > 0 && (
        <>
          <p className="meta">
            共 {documents.length} 份文档。选中一份即可启动抽取并审核它的候选。
          </p>
          <ul className="doclist__items">
            {documents.map((document) => {
              const tally = document.candidates
              const selected = document.id === selectedId
              return (
                <li key={document.id}>
                  <button
                    type="button"
                    className={`doclist__item${selected ? ' doclist__item--active' : ''}`}
                    aria-pressed={selected}
                    onClick={() => onSelect(document.id)}
                  >
                    <span className="doclist__name">{document.source_name}</span>
                    <span className="doclist__meta">
                      <span className="badge">{document.media_type}</span>
                      <span className="meta">
                        最新版本{' '}
                        <code title={document.latest_version_id ?? ''}>
                          {document.latest_version_id ? shortId(document.latest_version_id) : '—'}
                        </code>
                      </span>
                      <span className="meta">切片 {document.chunk_count}</span>
                      <span className="meta">
                        {document.has_extraction_runs ? '已抽取过' : '尚未抽取'}
                      </span>
                    </span>
                    <span className="doclist__tally">
                      <span className="badge badge--pending">待审核 {tally.pending}</span>
                      <span className="badge badge--approved">已批准 {tally.approved}</span>
                      <span className="badge badge--rejected">已拒绝 {tally.rejected}</span>
                      <span className="badge badge--published">已发布 {tally.published}</span>
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </div>
  )
}
