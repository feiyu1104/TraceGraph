import { useState } from 'react'

import type { ApiRequestError } from '../api/client'
import type {
  CandidateEntity,
  CandidateEvidenceChunk,
  CandidateRelation,
  CandidateStatus,
  ExtractionRun,
} from '../api/types'
import ErrorState from './ErrorState'

const STATUS_TEXT: Record<CandidateStatus, string> = {
  pending: '待审核',
  approved: '已批准',
  rejected: '已拒绝',
}

const STATUS_CLASS: Record<CandidateStatus, string> = {
  pending: 'badge--pending',
  approved: 'badge--approved',
  rejected: 'badge--rejected',
}

/** 修正候选时提交的字段；只包含真正改动的项。 */
export interface CandidateEdit {
  name?: string
  type?: string
  source_entity_id?: string
  target_entity_id?: string
}

interface CommonProps {
  /** 同一次抽取里的候选实体：关系的端点下拉只能从这里面选。 */
  runEntities: CandidateEntity[]
  entityById: Map<string, CandidateEntity>
  run: ExtractionRun | null
  entityTypes: string[]
  relationTypes: string[]
  selected: boolean
  onToggleSelect: (candidateId: string) => void
  onReviewStatus: (
    kind: 'entity' | 'relation',
    candidateId: string,
    status: CandidateStatus,
  ) => void
  onSave: (kind: 'entity' | 'relation', candidateId: string, edit: CandidateEdit) => void
  /** 这条候选上的请求是否在飞。 */
  busy: boolean
  /** 这条候选专属的服务端错误；冲突信息直接贴在它旁边。 */
  error: ApiRequestError | null
}

type CandidateRowProps = CommonProps &
  (
    | { kind: 'entity'; entity: CandidateEntity; relation?: never }
    | { kind: 'relation'; entity?: never; relation: CandidateRelation }
  )

export default function CandidateRow(props: CandidateRowProps) {
  const { selected, onToggleSelect, busy, error } = props
  const candidate = props.kind === 'entity' ? props.entity : props.relation

  const [editing, setEditing] = useState(false)
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const [draftName, setDraftName] = useState('')
  const [draftType, setDraftType] = useState('')
  const [draftSource, setDraftSource] = useState('')
  const [draftTarget, setDraftTarget] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)

  // 只有待审核且未发布的候选能改：已批准的候选动内容会让审核结果与内容对不上，
  // 已发布的候选在图里已经有一份，撤销发布是本批之外的功能。
  const editable = candidate.status === 'pending' && !candidate.is_published
  // 已发布的候选不能再走任何状态迁移，因此连勾选都不给。
  const selectable = !candidate.is_published

  function startEdit() {
    setLocalError(null)
    if (props.kind === 'entity') {
      setDraftName(props.entity.name)
      setDraftType(props.entity.type)
    } else {
      setDraftSource(props.relation.source_entity_id)
      setDraftTarget(props.relation.target_entity_id)
      setDraftType(props.relation.type)
    }
    setEditing(true)
  }

  function submitEdit() {
    // 只做基础非空检查，合法性以后端为准（类型白名单、自环、跨文档端点）。
    if (props.kind === 'entity') {
      if (!draftName.trim() || !draftType) {
        setLocalError('名称与类型都不能为空。')
        return
      }
      props.onSave('entity', props.entity.id, {
        name: draftName.trim(),
        type: draftType,
      })
      return
    }
    if (!draftSource || !draftTarget || !draftType) {
      setLocalError('关系类型与两端实体都不能为空。')
      return
    }
    if (draftSource === draftTarget) {
      setLocalError('关系两端不能是同一条候选实体。')
      return
    }
    props.onSave('relation', props.relation.id, {
      source_entity_id: draftSource,
      target_entity_id: draftTarget,
      type: draftType,
    })
  }

  const runLabel = props.run
    ? `任务 ${props.run.id.slice(0, 12)}… · ${props.run.model_id}`
    : `任务 ${candidate.extraction_run_id.slice(0, 12)}…`

  return (
    <li className={`candidate${selected ? ' candidate--selected' : ''}`}>
      <div className="candidate__head">
        <label className="candidate__select" title={selectable ? undefined : '已发布的候选不能再参与审核'}>
          <input
            type="checkbox"
            checked={selected}
            disabled={!selectable || busy}
            onChange={() => onToggleSelect(candidate.id)}
          />
          <span className="sr-only">选择这条候选</span>
        </label>

        <div className="candidate__main">
          {props.kind === 'entity' ? (
            <p className="candidate__title">
              <span className="candidate__name">{props.entity.name}</span>
              <span className="badge">{props.entity.type}</span>
            </p>
          ) : (
            <p className="candidate__title">
              <span className="candidate__name">
                {endpointLabel(props.entityById, props.relation.source_entity_id)}
              </span>
              <span className="candidate__edge">{props.relation.type}</span>
              <span className="candidate__name">
                {endpointLabel(props.entityById, props.relation.target_entity_id)}
              </span>
            </p>
          )}

          <p className="candidate__meta">
            <span className={`badge ${STATUS_CLASS[candidate.status]}`}>
              {STATUS_TEXT[candidate.status]}
            </span>
            {candidate.is_published ? (
              <span className="badge badge--published">已发布</span>
            ) : (
              <span className="badge">未发布</span>
            )}
            <span className="meta">{runLabel}</span>
            <span className="meta" title={candidate.document_version_id}>
              版本 {candidate.document_version_id.slice(0, 12)}…
            </span>
          </p>
        </div>

        <div className="candidate__actions">
          {!candidate.is_published && candidate.status === 'pending' && (
            <>
              <button
                type="button"
                className="button button--primary"
                disabled={busy}
                onClick={() => props.onReviewStatus(props.kind, candidate.id, 'approved')}
              >
                批准
              </button>
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => props.onReviewStatus(props.kind, candidate.id, 'rejected')}
              >
                拒绝
              </button>
            </>
          )}
          {!candidate.is_published && candidate.status !== 'pending' && (
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() => props.onReviewStatus(props.kind, candidate.id, 'pending')}
            >
              退回待审核
            </button>
          )}
          {editable && !editing && (
            <button type="button" className="button" disabled={busy} onClick={startEdit}>
              编辑
            </button>
          )}
        </div>
      </div>

      {candidate.is_published && (
        <p className="candidate__note">
          这条候选已经发布到图谱（图对象 {candidate.graph_id ?? '—'}）。
          撤销发布尚未实现，因此不能再修改或退回。
        </p>
      )}

      <button
        type="button"
        className="candidate__evidence-toggle"
        aria-expanded={evidenceOpen}
        onClick={() => setEvidenceOpen((open) => !open)}
      >
        <span>原文证据（{candidate.evidence.length}）</span>
        <span className="candidate__chevron">{evidenceOpen ? '收起' : '展开'}</span>
      </button>

      {evidenceOpen && <EvidenceBlock evidence={candidate.evidence} />}

      {editing && (
        <form
          className="candidate__edit"
          onSubmit={(event) => {
            event.preventDefault()
            submitEdit()
          }}
        >
          {props.kind === 'entity' ? (
            <>
              <label className="field">
                <span className="field__label">名称</span>
                <input
                  className="field__input"
                  value={draftName}
                  disabled={busy}
                  onChange={(event) => setDraftName(event.target.value)}
                />
              </label>
              <label className="field">
                <span className="field__label">类型</span>
                <select
                  className="field__input"
                  value={draftType}
                  disabled={busy}
                  onChange={(event) => setDraftType(event.target.value)}
                >
                  {options(props.entityTypes, draftType).map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </label>
            </>
          ) : (
            <>
              <label className="field">
                <span className="field__label">源实体</span>
                <select
                  className="field__input"
                  value={draftSource}
                  disabled={busy}
                  onChange={(event) => setDraftSource(event.target.value)}
                >
                  {props.runEntities.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}（{item.type}）
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field__label">关系类型</span>
                <select
                  className="field__input"
                  value={draftType}
                  disabled={busy}
                  onChange={(event) => setDraftType(event.target.value)}
                >
                  {options(props.relationTypes, draftType).map((type) => (
                    <option key={type} value={type}>
                      {type}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field__label">目标实体</span>
                <select
                  className="field__input"
                  value={draftTarget}
                  disabled={busy}
                  onChange={(event) => setDraftTarget(event.target.value)}
                >
                  {props.runEntities.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}（{item.type}）
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}

          <p className="candidate__note">
            只能改名称与类型；知识库、文档、版本、抽取任务与证据都由抽取决定，不可修改。
          </p>

          {localError && (
            <p className="candidate__note candidate__note--error" role="alert">
              {localError}
            </p>
          )}

          <div className="candidate__edit-actions">
            <button type="submit" className="button button--primary" disabled={busy}>
              {busy ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() => {
                setEditing(false)
                setLocalError(null)
              }}
            >
              取消
            </button>
          </div>
        </form>
      )}

      {error && <ErrorState error={error} />}
    </li>
  )
}

function EvidenceBlock({ evidence }: { evidence: CandidateEvidenceChunk[] }) {
  if (evidence.length === 0) {
    // 契约要求候选至少有一条证据，走到这里说明证据已经断链（切片被删）。
    // 这不能渲染成一条正常的候选。
    return (
      <p className="evidence__missing" role="alert">
        这条候选没有可显示的原文证据：它关联的切片已经不存在。请不要批准或发布它，
        重新抽取一次即可恢复证据。
      </p>
    )
  }
  return (
    <ul className="evidence">
      {evidence.map((item) => (
        <li key={item.chunk_id} className="evidence__item">
          <p className="meta">
            来源：{item.source_name} · 位置：{item.locator} · 切片：
            {item.chunk_id}
          </p>
          <p className="evidence__content">{item.content}</p>
        </li>
      ))}
    </ul>
  )
}

function endpointLabel(entityById: Map<string, CandidateEntity>, entityId: string): string {
  const found = entityById.get(entityId)
  // 关系两端优先显示实体名称；确实找不到时如实说明，不显示成一串 ID 了事。
  return found ? found.name : `未知实体（${entityId}）`
}

/** 适配器清单里没有当前值时，把它补进选项，免得下拉框显示成空白。 */
function options(available: string[], current: string): string[] {
  if (!current || available.includes(current)) return available
  return [current, ...available]
}
