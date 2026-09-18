import { useMemo, useState } from 'react'

import type { ApiRequestError, PublicationTarget } from '../api/client'
import type {
  CandidateEntity,
  CandidateRelation,
  CandidateStatus,
  ExtractionRun,
  PublicationResult,
} from '../api/types'
import CandidateRow, { type CandidateEdit } from './CandidateRow'
import EmptyState from './EmptyState'
import ErrorState from './ErrorState'
import PublicationPanel, { type PublishPreview } from './PublicationPanel'

/** 抽取任务筛选里的「全部」；驱动候选的加载方式，因此由页面持有。 */
export const ALL_RUNS = 'all'

export type StatusFilter =
  | 'all'
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'published'
  | 'unpublished'

const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'pending', label: '待审核' },
  { value: 'approved', label: '已批准' },
  { value: 'rejected', label: '已拒绝' },
  { value: 'published', label: '已发布' },
  { value: 'unpublished', label: '未发布' },
]

interface CandidateBoardProps {
  entities: CandidateEntity[]
  relations: CandidateRelation[]
  /** 还没有任何一份成功加载的候选时为 true。 */
  loading: boolean
  error: ApiRequestError | null
  onReload: () => void
  runs: ExtractionRun[]
  runFilter: string
  onRunFilterChange: (value: string) => void
  /** 当前 Workspace 适配器声明的类型；修正候选时的类型下拉来自它。 */
  entityTypes: string[]
  relationTypes: string[]
  busyIds: ReadonlySet<string>
  rowErrors: Record<string, ApiRequestError>
  batchBusy: boolean
  batchError: ApiRequestError | null
  onReviewStatus: (
    kind: 'entity' | 'relation',
    candidateId: string,
    status: CandidateStatus,
  ) => void
  onSave: (kind: 'entity' | 'relation', candidateId: string, edit: CandidateEdit) => void
  onBatchReview: (
    status: CandidateStatus,
    entityIds: string[],
    relationIds: string[],
  ) => void
  publish: {
    workspaceName: string
    graphAvailable: boolean
    busy: boolean
    error: ApiRequestError | null
    result: PublicationResult | null
    onPublish: (target: PublicationTarget) => void
  }
}

export default function CandidateBoard(props: CandidateBoardProps) {
  const { entities, relations, runs, runFilter, onRunFilterChange, publish } = props

  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [entityTypeFilter, setEntityTypeFilter] = useState('')
  const [relationTypeFilter, setRelationTypeFilter] = useState('')
  const [selectedEntityIds, setSelectedEntityIds] = useState<ReadonlySet<string>>(new Set())
  const [selectedRelationIds, setSelectedRelationIds] = useState<ReadonlySet<string>>(new Set())
  const [confirming, setConfirming] = useState(false)

  const entityById = useMemo(
    () => new Map(entities.map((entity) => [entity.id, entity])),
    [entities],
  )

  // 关系的端点下拉只能用同一次抽取里的候选实体。
  const entitiesByRun = useMemo(() => {
    const grouped = new Map<string, CandidateEntity[]>()
    for (const entity of entities) {
      const bucket = grouped.get(entity.extraction_run_id)
      if (bucket) bucket.push(entity)
      else grouped.set(entity.extraction_run_id, [entity])
    }
    return grouped
  }, [entities])

  const entityTypeOptions = useMemo(
    () => union(props.entityTypes, entities.map((entity) => entity.type)),
    [props.entityTypes, entities],
  )
  const relationTypeOptions = useMemo(
    () => union(props.relationTypes, relations.map((relation) => relation.type)),
    [props.relationTypes, relations],
  )

  const visibleEntities = useMemo(
    () =>
      entities.filter(
        (entity) =>
          matchesStatus(entity.status, entity.is_published, statusFilter) &&
          (!entityTypeFilter || entity.type === entityTypeFilter),
      ),
    [entities, statusFilter, entityTypeFilter],
  )
  const visibleRelations = useMemo(
    () =>
      relations.filter(
        (relation) =>
          matchesStatus(relation.status, relation.is_published, statusFilter) &&
          (!relationTypeFilter || relation.type === relationTypeFilter),
      ),
    [relations, statusFilter, relationTypeFilter],
  )

  const totals = useMemo(() => count(entities, relations), [entities, relations])

  const selected = useMemo(
    () => ({
      entities: entities.filter((entity) => selectedEntityIds.has(entity.id)),
      // 已发布的候选不参与任何审核操作，即使它还在选中集合里。
      relations: relations.filter(
        (relation) => selectedRelationIds.has(relation.id) && !relation.is_published,
      ),
    }),
    [entities, relations, selectedEntityIds, selectedRelationIds],
  )

  const batchCounts = useMemo(
    () => ({
      approve: batchSize(selected, 'approved'),
      reject: batchSize(selected, 'rejected'),
      reset: batchSize(selected, 'pending'),
    }),
    [selected],
  )

  const publication = useMemo(
    () => publicationPlan(entities, relations, selectedEntityIds, selectedRelationIds, runFilter),
    [entities, relations, selectedEntityIds, selectedRelationIds, runFilter],
  )

  const selectedTotal =
    selected.entities.length + selected.relations.length
  const selectableEntities = visibleEntities.filter((entity) => !entity.is_published)
  const selectableRelations = visibleRelations.filter((relation) => !relation.is_published)

  function toggle(
    setter: (update: (current: ReadonlySet<string>) => ReadonlySet<string>) => void,
    candidateId: string,
  ) {
    setter((current) => {
      const next = new Set(current)
      if (next.has(candidateId)) next.delete(candidateId)
      else next.add(candidateId)
      return next
    })
  }

  function selectAllVisible() {
    setSelectedEntityIds(new Set(selectableEntities.map((entity) => entity.id)))
    setSelectedRelationIds(new Set(selectableRelations.map((relation) => relation.id)))
  }

  function clearSelection() {
    setSelectedEntityIds(new Set())
    setSelectedRelationIds(new Set())
    setConfirming(false)
  }

  function submitBatch(status: CandidateStatus) {
    const picked = batchPayload(selected, status)
    if (picked.entityIds.length === 0 && picked.relationIds.length === 0) return
    // 实体与关系一次提交：批量批准关系时，端点实体如果在同一批里就一起提交，
    // 否则后端会因为「端点尚未批准」整批拒绝。
    props.onBatchReview(status, picked.entityIds, picked.relationIds)
  }

  // 有任何一条候选的请求在飞时，整块一起禁用：避免两个审核操作同时提交，
  // 后到的重新加载会覆盖前一次的结果。
  const busy = props.batchBusy || props.busyIds.size > 0

  return (
    <div className="board">
      <div className="board__head">
        <h3 className="block__title">候选审核</h3>
        <button
          type="button"
          className="button button--ghost"
          onClick={props.onReload}
          disabled={props.loading}
        >
          {props.loading ? '刷新中…' : '刷新候选'}
        </button>
      </div>

      <div className="board__filters">
        <label className="field">
          <span className="field__label">审核状态</span>
          <select
            className="field__input"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
          >
            {STATUS_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">实体类型</span>
          <select
            className="field__input"
            value={entityTypeFilter}
            onChange={(event) => setEntityTypeFilter(event.target.value)}
          >
            <option value="">全部类型</option>
            {entityTypeOptions.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">关系类型</span>
          <select
            className="field__input"
            value={relationTypeFilter}
            onChange={(event) => setRelationTypeFilter(event.target.value)}
          >
            <option value="">全部类型</option>
            {relationTypeOptions.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">抽取任务</span>
          <select
            className="field__input"
            value={runFilter}
            onChange={(event) => {
              // 换任务等于换一份候选：勾选与发布确认都必须重来。
              clearSelection()
              onRunFilterChange(event.target.value)
            }}
          >
            <option value={ALL_RUNS}>全部任务</option>
            {runs.map((run) => (
              <option key={run.id} value={run.id}>
                {run.created_at} · {run.model_id} · 实体 {run.entity_count} / 关系{' '}
                {run.relation_count}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p className="board__summary">
        实体总数 {totals.entities} · 关系总数 {totals.relations} · 待审核{' '}
        {totals.pending} · 已批准 {totals.approved} · 已拒绝 {totals.rejected} · 已发布{' '}
        {totals.published}
      </p>
      <p className="meta">
        当前筛选显示：实体 {visibleEntities.length} / {totals.entities} 条，关系{' '}
        {visibleRelations.length} / {totals.relations} 条。筛选只影响显示与勾选范围，
        不会改变服务端的数据。
      </p>

      <div className="board__batch">
        <div className="board__batch-select">
          <button type="button" className="button" onClick={selectAllVisible} disabled={busy}>
            全选当前筛选结果
          </button>
          <button
            type="button"
            className="button"
            onClick={clearSelection}
            disabled={busy || selectedTotal === 0}
          >
            取消全选
          </button>
          <span className="meta">
            已选 实体 {selected.entities.length} 条 · 关系 {selected.relations.length} 条
          </span>
        </div>
        <div className="board__batch-actions">
          <button
            type="button"
            className="button button--primary"
            onClick={() => submitBatch('approved')}
            disabled={busy || batchCounts.approve === 0}
          >
            批量批准（{batchCounts.approve}）
          </button>
          <button
            type="button"
            className="button"
            onClick={() => submitBatch('rejected')}
            disabled={busy || batchCounts.reject === 0}
          >
            批量拒绝（{batchCounts.reject}）
          </button>
          <button
            type="button"
            className="button"
            onClick={() => submitBatch('pending')}
            disabled={busy || batchCounts.reset === 0}
          >
            退回待审核（{batchCounts.reset}）
          </button>
        </div>
        <p className="meta">
          批量操作只提交符合状态要求的候选：批准与拒绝只作用于待审核的候选，退回只作用于
          已批准或已拒绝的候选；已发布的候选不参与。批准关系时，端点实体如果也在所选里
          会一起提交。
        </p>
      </div>

      {props.batchError && <ErrorState error={props.batchError} onRetry={props.onReload} />}

      {props.error && <ErrorState error={props.error} onRetry={props.onReload} />}

      {props.loading && <p className="meta">正在读取候选…</p>}

      <section className="board__section">
        <h4 className="board__section-title">
          候选实体
          <span className="badge">显示 {visibleEntities.length}</span>
        </h4>
        {visibleEntities.length === 0 ? (
          <EmptyState
            title={totals.entities === 0 ? '这份文档还没有候选实体' : '当前筛选没有匹配的候选实体'}
            hint={
              totals.entities === 0
                ? '先在上面的「提取知识」里启动一次抽取。'
                : '换一个审核状态或类型再看看。'
            }
          />
        ) : (
          <ul className="candidates">
            {visibleEntities.map((entity) => (
              <CandidateRow
                key={entity.id}
                kind="entity"
                entity={entity}
                runEntities={entitiesByRun.get(entity.extraction_run_id) ?? []}
                entityById={entityById}
                run={runs.find((run) => run.id === entity.extraction_run_id) ?? null}
                entityTypes={props.entityTypes}
                relationTypes={props.relationTypes}
                selected={selectedEntityIds.has(entity.id)}
                onToggleSelect={(candidateId) => toggle(setSelectedEntityIds, candidateId)}
                onReviewStatus={props.onReviewStatus}
                onSave={props.onSave}
                busy={busy || props.busyIds.has(entity.id)}
                error={props.rowErrors[entity.id] ?? null}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="board__section">
        <h4 className="board__section-title">
          候选关系
          <span className="badge">显示 {visibleRelations.length}</span>
        </h4>
        {visibleRelations.length === 0 ? (
          <EmptyState
            title={totals.relations === 0 ? '这份文档还没有候选关系' : '当前筛选没有匹配的候选关系'}
            hint={
              totals.relations === 0
                ? '关系由抽取产出；一次抽取可能只产出实体，也可能两者都有。'
                : '换一个审核状态或类型再看看。'
            }
          />
        ) : (
          <ul className="candidates">
            {visibleRelations.map((relation) => (
              <CandidateRow
                key={relation.id}
                kind="relation"
                relation={relation}
                runEntities={entitiesByRun.get(relation.extraction_run_id) ?? []}
                entityById={entityById}
                run={runs.find((run) => run.id === relation.extraction_run_id) ?? null}
                entityTypes={props.entityTypes}
                relationTypes={props.relationTypes}
                selected={selectedRelationIds.has(relation.id)}
                onToggleSelect={(candidateId) => toggle(setSelectedRelationIds, candidateId)}
                onReviewStatus={props.onReviewStatus}
                onSave={props.onSave}
                busy={busy || props.busyIds.has(relation.id)}
                error={props.rowErrors[relation.id] ?? null}
              />
            ))}
          </ul>
        )}
      </section>

      <PublicationPanel
        workspaceName={publish.workspaceName}
        preview={publication.preview}
        graphAvailable={publish.graphAvailable}
        confirming={confirming}
        busy={publish.busy}
        error={publish.error}
        result={publish.result}
        onRequest={() => setConfirming(true)}
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false)
          publish.onPublish(publication.target)
        }}
      />
    </div>
  )
}

function matchesStatus(
  status: CandidateStatus,
  published: boolean,
  filter: StatusFilter,
): boolean {
  switch (filter) {
    case 'all':
      return true
    case 'published':
      return published
    case 'unpublished':
      return !published
    default:
      return status === filter
  }
}

function count(entities: CandidateEntity[], relations: CandidateRelation[]) {
  const all: (CandidateEntity | CandidateRelation)[] = [...entities, ...relations]
  return {
    entities: entities.length,
    relations: relations.length,
    pending: all.filter((item) => item.status === 'pending').length,
    approved: all.filter((item) => item.status === 'approved').length,
    rejected: all.filter((item) => item.status === 'rejected').length,
    published: all.filter((item) => item.is_published).length,
  }
}

/** 一条候选是否可以参与目标状态为 `status` 的批量操作。 */
function eligible(candidate: CandidateEntity | CandidateRelation, status: CandidateStatus): boolean {
  if (candidate.is_published) return false
  // 退回待审核作用于已批准 / 已拒绝；批准与拒绝只作用于待审核。
  return status === 'pending' ? candidate.status !== 'pending' : candidate.status === 'pending'
}

function batchSize(
  selection: { entities: CandidateEntity[]; relations: CandidateRelation[] },
  status: CandidateStatus,
): number {
  return (
    selection.entities.filter((item) => eligible(item, status)).length +
    selection.relations.filter((item) => eligible(item, status)).length
  )
}

function batchPayload(
  selection: { entities: CandidateEntity[]; relations: CandidateRelation[] },
  status: CandidateStatus,
): { entityIds: string[]; relationIds: string[] } {
  return {
    entityIds: selection.entities.filter((item) => eligible(item, status)).map((item) => item.id),
    relationIds: selection.relations
      .filter((item) => eligible(item, status))
      .map((item) => item.id),
  }
}

function publicationPlan(
  entities: CandidateEntity[],
  relations: CandidateRelation[],
  selectedEntityIds: ReadonlySet<string>,
  selectedRelationIds: ReadonlySet<string>,
  runFilter: string,
): { preview: PublishPreview; target: PublicationTarget } {
  const approvedEntities = entities.filter(
    (entity) => entity.status === 'approved' && !entity.is_published,
  )
  const approvedRelations = relations.filter(
    (relation) => relation.status === 'approved' && !relation.is_published,
  )
  // 用户明确选中了已批准候选时按选中发布；否则按当前抽取任务；再否则按本文档。
  const pickedEntities = approvedEntities.filter((entity) => selectedEntityIds.has(entity.id))
  const pickedRelations = approvedRelations.filter((relation) =>
    selectedRelationIds.has(relation.id),
  )
  if (pickedEntities.length > 0 || pickedRelations.length > 0) {
    return {
      preview: {
        mode: 'candidates',
        entityCount: pickedEntities.length,
        relationCount: pickedRelations.length,
        scope: `所选候选（${describeRuns([...pickedEntities, ...pickedRelations])}）`,
      },
      target: {
        candidate_entity_ids: pickedEntities.map((entity) => entity.id),
        candidate_relation_ids: pickedRelations.map((relation) => relation.id),
      },
    }
  }
  if (runFilter !== ALL_RUNS) {
    return {
      preview: {
        mode: 'run',
        entityCount: approvedEntities.length,
        relationCount: approvedRelations.length,
        scope: `抽取任务 ${shortId(runFilter)} 中全部已批准且未发布的候选`,
      },
      target: { extraction_run_id: runFilter },
    }
  }
  return {
    preview: {
      mode: 'candidates',
      entityCount: approvedEntities.length,
      relationCount: approvedRelations.length,
      scope: `本文档全部已批准且未发布的候选（${describeRuns([
        ...approvedEntities,
        ...approvedRelations,
      ])}）`,
    },
    target: {
      candidate_entity_ids: approvedEntities.map((entity) => entity.id),
      candidate_relation_ids: approvedRelations.map((relation) => relation.id),
    },
  }
}

function describeRuns(candidates: (CandidateEntity | CandidateRelation)[]): string {
  const runs = [...new Set(candidates.map((item) => item.extraction_run_id))]
  if (runs.length === 0) return '没有候选'
  if (runs.length === 1) return `抽取任务 ${shortId(runs[0])}`
  return `跨 ${runs.length} 次抽取任务`
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

function union(first: string[], second: string[]): string[] {
  return [...new Set([...first, ...second])].sort()
}
