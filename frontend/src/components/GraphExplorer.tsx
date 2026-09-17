import { useState } from 'react'

import { ApiRequestError, fetchEntityRelations, searchEntities } from '../api/client'
import type { Entity, EntityRelation } from '../api/types'
import EmptyState from './EmptyState'
import ErrorState from './ErrorState'
import { RelationEvidenceDetail } from './GraphPath'

const ENTITY_TYPES: Record<string, string> = {
  Disease: '疾病',
  Symptom: '症状',
  Drug: '药物',
  Check: '检查',
  Department: '科室',
  Food: '食物',
  Treatment: '治疗',
  Recipe: '食谱',
  Category: '分类',
}

function entityTypeLabel(type: string): string {
  return ENTITY_TYPES[type] ?? type
}

export default function GraphExplorer() {
  const [query, setQuery] = useState('')
  const [entities, setEntities] = useState<Entity[] | null>(null)
  const [entity, setEntity] = useState<Entity | null>(null)
  const [relations, setRelations] = useState<EntityRelation[] | null>(null)
  const [openRelationId, setOpenRelationId] = useState<string | null>(null)
  const [error, setError] = useState<ApiRequestError | null>(null)
  const [busy, setBusy] = useState(false)

  async function runSearch() {
    const trimmed = query.trim()
    if (!trimmed || busy) return
    setBusy(true)
    setError(null)
    setEntity(null)
    setRelations(null)
    setOpenRelationId(null)
    try {
      const result = await searchEntities(trimmed)
      setEntities(result.entities)
    } catch (caught) {
      setError(toApiError(caught))
      setEntities(null)
    } finally {
      setBusy(false)
    }
  }

  async function openEntity(target: Entity) {
    setBusy(true)
    setError(null)
    setEntity(target)
    setRelations(null)
    setOpenRelationId(null)
    try {
      const result = await fetchEntityRelations(target.id)
      setRelations(result.relations)
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="explorer">
      <form
        className="explorer__search"
        onSubmit={(event) => {
          event.preventDefault()
          void runSearch()
        }}
      >
        <label className="field">
          <span className="field__label">查找实体</span>
          <input
            className="field__input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="疾病、症状、检查或药物名称"
            autoComplete="off"
          />
        </label>
        <button type="submit" className="button button--primary" disabled={busy || !query.trim()}>
          查找
        </button>
      </form>

      {error && <ErrorState error={error} onRetry={() => void runSearch()} />}

      {entities !== null && entities.length === 0 && !error && (
        <EmptyState
          title="没有找到匹配实体"
          hint="换一个更完整的名称试试，例如「百日咳」而不是「咳」。"
        />
      )}

      {entities !== null && entities.length > 0 && !entity && (
        <ul className="entities">
          {entities.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className="entities__item"
                onClick={() => void openEntity(item)}
                disabled={busy}
              >
                <span className="entities__name">{item.name}</span>
                <span className="badge">{entityTypeLabel(item.type)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {entity && (
        <div className="relations">
          <header className="relations__head">
            <h3 className="relations__title">
              {entity.name}
              <span className="badge">{entityTypeLabel(entity.type)}</span>
            </h3>
            <button
              type="button"
              className="button button--ghost"
              onClick={() => {
                setEntity(null)
                setRelations(null)
                setOpenRelationId(null)
              }}
            >
              返回结果列表
            </button>
          </header>

          {relations === null && !error && <p className="meta">正在读取关系…</p>}

          {relations !== null && relations.length === 0 && (
            <EmptyState title="该实体没有直接关系" />
          )}

          {relations !== null && relations.length > 0 && (
            <>
              <p className="meta">
                共 {relations.length} 条直接关系（受后端返回上限限制，不代表全部）。
                点击关系查看 DUTMed 来源与原文位置。
              </p>
              <ul className="relations__list">
                {relations.map((relation) => {
                  const open = openRelationId === relation.id
                  const source = relation.source?.name ?? '未知'
                  const target = relation.target?.name ?? '未知'
                  return (
                    <li key={relation.id} className="relations__item">
                      <button
                        type="button"
                        className="relations__row"
                        aria-expanded={open}
                        onClick={() =>
                          setOpenRelationId((current) =>
                            current === relation.id ? null : relation.id,
                          )
                        }
                      >
                        <span className="relations__arrow" aria-hidden="true">
                          {relation.direction === 'outgoing' ? '→' : '←'}
                        </span>
                        <span className="relations__path">
                          {source} <em>{relation.label}</em> {target}
                        </span>
                        <span className="meta">{relation.evidence.length} 处原文</span>
                      </button>
                      {open && (
                        <div className="relations__detail">
                          {relation.evidence.length === 0 ? (
                            <p className="meta">该关系没有登记原文位置。</p>
                          ) : (
                            relation.evidence.map((item) => (
                              <p key={item.chunk_id} className="meta">
                                {item.source_name} · {item.locator}
                              </p>
                            ))
                          )}
                          <RelationEvidenceDetail relationId={relation.id} />
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            </>
          )}
        </div>
      )}

      {entities === null && !entity && !error && (
        <EmptyState
          title="尚未查找实体"
          hint="图谱浏览走的是同一个图后端，多跳检索也以它为起点。"
        />
      )}
    </div>
  )
}

function toApiError(caught: unknown): ApiRequestError {
  if (caught instanceof ApiRequestError) return caught
  return new ApiRequestError(0, 'unknown_error', String(caught))
}
