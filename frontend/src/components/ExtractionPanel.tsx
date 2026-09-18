import { useState } from 'react'

import type { ApiRequestError } from '../api/client'
import type { ExtractionRun, ExtractionStatus, ModelInfo, WorkspaceDocument } from '../api/types'
import EmptyState from './EmptyState'
import ErrorState from './ErrorState'

const RUN_STATUS_TEXT: Record<ExtractionStatus, string> = {
  pending: '排队中',
  running: '抽取中',
  succeeded: '已完成',
  failed: '失败',
}

const RUN_STATUS_CLASS: Record<ExtractionStatus, string> = {
  pending: 'badge--pending',
  running: 'badge--pending',
  succeeded: 'badge--approved',
  failed: 'badge--rejected',
}

export function runStatusText(status: ExtractionStatus): string {
  return RUN_STATUS_TEXT[status] ?? status
}

export function isTerminal(status: ExtractionStatus): boolean {
  return status === 'succeeded' || status === 'failed'
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

interface ExtractionPanelProps {
  document: WorkspaceDocument
  /** 当前知识库的适配器标签；抽取按它的类型清单产出候选。 */
  adapterLabel: string
  adapterId: string
  models: ModelInfo[]
  modelId: string
  onModelChange: (modelId: string) => void
  onStart: () => void
  /** 启动中或轮询中：此时不允许重复提交。 */
  busy: boolean
  /** 本次启动的任务；已有结果后仍然显示，直到下一次启动。 */
  activeRun: ExtractionRun | null
  startError: ApiRequestError | null
  /** 0 候选一类需要解释的情况；不是错误。 */
  notice: string | null
  runs: ExtractionRun[] | null
  runsLoading: boolean
  runsError: ApiRequestError | null
  onOpenRun: (runId: string) => void
  onReloadRuns: () => void
}

export default function ExtractionPanel({
  document,
  adapterLabel,
  adapterId,
  models,
  modelId,
  onModelChange,
  onStart,
  busy,
  activeRun,
  startError,
  notice,
  runs,
  runsLoading,
  runsError,
  onOpenRun,
  onReloadRuns,
}: ExtractionPanelProps) {
  const [showFailedRuns, setShowFailedRuns] = useState(false)
  const current = models.find((model) => model.id === modelId)
  const polling =
    activeRun !== null && (activeRun.status === 'pending' || activeRun.status === 'running')
  const failedRunCount = runs?.filter((run) => run.status === 'failed').length ?? 0
  const visibleRuns = showFailedRuns
    ? runs
    : runs?.filter((run) => run.status !== 'failed')

  return (
    <div className="extraction">
      <h3 className="block__title">提取知识</h3>
      <p className="meta">
        对「{document.source_name}」的最新版本抽取候选实体与关系。抽取只产出候选，
        不写图谱；批准与发布都是之后另外的操作。
      </p>

      <dl className="metrics extraction__scope">
        <div>
          <dt>适配器</dt>
          <dd title={adapterId}>{adapterLabel}</dd>
        </div>
        <div>
          <dt>文档版本</dt>
          <dd title={document.latest_version_id ?? ''}>
            {document.latest_version_id ? shortId(document.latest_version_id) : '暂无'}
          </dd>
        </div>
        <div>
          <dt>切片</dt>
          <dd>{document.chunk_count}</dd>
        </div>
      </dl>

      {document.chunk_count === 0 ? (
        <p className="extraction__notice">
          这份文档还没有可抽取的切片，先确认上传时已成功入库。
        </p>
      ) : (
        <>
          <label className="field extraction__model">
            <span className="field__label">抽取模型</span>
            <select
              className="field__input"
              value={modelId}
              onChange={(event) => onModelChange(event.target.value)}
              disabled={busy}
            >
              {models.map((model) => (
                <option key={model.id} value={model.id} disabled={!model.available}>
                  {model.model ? `${model.label}（${model.model}）` : model.label}
                  {model.available ? '' : `（不可用：${model.reason}）`}
                </option>
              ))}
            </select>
          </label>

          <p className="meta">
            {current?.kind === 'extractive'
              ? '离线摘录：按适配器声明的章节规则确定性抽取，不调用任何外部模型。'
              : '在线模型：把原文切片交给该模型抽取候选，引用到的切片由后端逐条校验。'}
          </p>

          <div className="extraction__actions">
            <button
              type="button"
              className="button button--primary"
              onClick={onStart}
              disabled={busy || !modelId}
            >
              {busy ? '抽取中…' : '开始抽取'}
            </button>
            <button
              type="button"
              className="button"
              onClick={onReloadRuns}
              disabled={busy || runsLoading}
            >
              刷新任务
            </button>
          </div>
        </>
      )}

      {startError && <ErrorState error={startError} />}

      {notice && (
        <p className="extraction__notice extraction__notice--warn" role="status">
          {notice}
        </p>
      )}

      {activeRun && (
        <div className="extraction__run" role="status">
          <p className="extraction__run-head">
            <span className={`badge ${RUN_STATUS_CLASS[activeRun.status]}`}>
              {runStatusText(activeRun.status)}
            </span>
            <span className="meta" title={activeRun.id}>
              任务 {shortId(activeRun.id)}
            </span>
            <span className="meta">模型 {activeRun.model_id}</span>
          </p>
          <p className="meta">
            候选实体 {activeRun.entity_count} · 候选关系 {activeRun.relation_count}
            {polling && ' · 正在等待任务结束…'}
          </p>
          {activeRun.error && (
            <p className="extraction__notice extraction__notice--error">{activeRun.error}</p>
          )}
        </div>
      )}

      <div className="extraction__history">
        <div className="extraction__history-head">
          <h4 className="extraction__history-title">抽取任务历史</h4>
          {failedRunCount > 0 && (
            <button
              type="button"
              className="button button--ghost"
              onClick={() => setShowFailedRuns((shown) => !shown)}
            >
              {showFailedRuns ? '收起失败记录' : `显示失败记录（${failedRunCount}）`}
            </button>
          )}
        </div>
        {runsError && <ErrorState error={runsError} onRetry={onReloadRuns} />}
        {runsLoading && runs === null && <p className="meta">正在读取抽取任务…</p>}
        {runs !== null && runs.length === 0 && !runsError && (
          <EmptyState
            title="这份文档还没有抽取任务"
            hint="选好模型后点击「开始抽取」，任务与候选都会保留在这里。"
          />
        )}
        {visibleRuns !== null && visibleRuns !== undefined && visibleRuns.length === 0 && failedRunCount > 0 && (
          <p className="meta">目前只有失败记录；它们仅用于排查问题，不会产生候选。</p>
        )}
        {visibleRuns !== null && visibleRuns !== undefined && visibleRuns.length > 0 && (
          <ul className="runs">
            {visibleRuns.map((run) => (
              <li key={run.id} className="runs__item">
                <span className={`badge ${RUN_STATUS_CLASS[run.status]}`}>
                  {runStatusText(run.status)}
                </span>
                <span className="runs__id" title={run.id}>
                  {shortId(run.id)}
                </span>
                <span className="meta">
                  {run.model_id} · 实体 {run.entity_count} · 关系 {run.relation_count}
                </span>
                <span className="meta">{run.created_at}</span>
                {run.status === 'succeeded' && (
                  <button
                    type="button"
                    className="button button--ghost"
                    onClick={() => onOpenRun(run.id)}
                  >
                    查看候选
                  </button>
                )}
                {run.error && <span className="runs__error">{run.error}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
