import { Fragment, useEffect, useState } from 'react'

import { fetchRelationEvidence } from '../api/client'
import type {
  GraphPath as GraphPathData,
  RelationEvidenceChunk,
  RelationPublicationSource,
} from '../api/types'

type DetailState =
  | { status: 'loading' }
  | {
      status: 'ready'
      chunks: RelationEvidenceChunk[]
      sources: RelationPublicationSource[]
    }
  | { status: 'error'; message: string }

/**
 * 某条关系溯源到的原文，点击关系时才去取。
 * 原文不进每次回答的响应体，避免把整段正文塞进列表接口。
 */
export function RelationEvidenceDetail({
  relationId,
  workspaceId,
}: {
  relationId: string
  workspaceId: string
}) {
  const [state, setState] = useState<DetailState>({ status: 'loading' })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })
    fetchRelationEvidence(relationId, workspaceId)
      .then((data) => {
        if (!cancelled) {
          setState({ status: 'ready', chunks: data.evidence, sources: data.sources })
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setState({
          status: 'error',
          message: error instanceof Error ? error.message : '原文读取失败',
        })
      })
    return () => {
      cancelled = true
    }
  }, [relationId, workspaceId])

  if (state.status === 'loading') {
    return <div className="relation-evidence is-muted">正在读取关系原文…</div>
  }
  if (state.status === 'error') {
    return <div className="relation-evidence is-error">{state.message}</div>
  }
  if (state.chunks.length === 0) {
    return <div className="relation-evidence is-muted">该关系没有可追溯的原文片段。</div>
  }
  return (
    <div className="relation-evidence">
      {state.sources.length > 0 && (
        <div className="relation-evidence__item">
          <p className="meta">
            发布来源：{state.sources.length} 条已审核候选
          </p>
          {state.sources.map((source) => (
            <p key={source.candidate_relation_id} className="meta">
              文档版本 {source.document_version_id} · 抽取任务{' '}
              {source.extraction_run_id} · 候选 {source.candidate_relation_id}
            </p>
          ))}
        </div>
      )}
      {state.chunks.map((chunk) => (
        <div key={chunk.chunk_id} className="relation-evidence__item">
          <p className="meta">{chunk.source_name} · {chunk.locator}</p>
          <p className="relation-evidence__text">{chunk.content}</p>
        </div>
      ))}
    </div>
  )
}

interface GraphPathProps {
  path: GraphPathData
  workspaceId: string
  /** 路径块自身的「直接事实 / 推导关联」标记 */
  showHopBadge?: boolean
}

export default function GraphPath({ path, workspaceId, showHopBadge = true }: GraphPathProps) {
  const [openRelationId, setOpenRelationId] = useState<string | null>(null)
  const nameById = new Map(path.nodes.map((node) => [node.id, node.name]))
  const isDerived = path.steps.length > 1

  return (
    <div className="gpath">
      {showHopBadge && (
        <span className={`badge${isDerived ? ' badge--derived' : ' badge--fact'}`}>
          {isDerived ? `${path.steps.length} 跳 · 推导关联` : '1 跳 · 直接事实'}
        </span>
      )}

      <div className="gpath__track">
        <span className="gpath__node gpath__node--start">{path.nodes[0]?.name}</span>
        {path.steps.map((step, index) => {
          const open = openRelationId === step.relation_id
          return (
            <Fragment key={`${step.relation_id}-${index}`}>
              <button
                type="button"
                className={`gpath__edge gpath__edge--${step.direction}`}
                aria-expanded={open}
                title="点击查看该关系对应的原文"
                onClick={() =>
                  setOpenRelationId((current) =>
                    current === step.relation_id ? null : step.relation_id,
                  )
                }
              >
                <span aria-hidden="true">{step.direction === 'outgoing' ? '→' : '←'}</span>
                <span className="gpath__edge-label">{step.label}</span>
                <span aria-hidden="true">{step.direction === 'outgoing' ? '→' : '←'}</span>
              </button>
              <span className="gpath__node">{step.target.name}</span>
            </Fragment>
          )
        })}
      </div>

      {openRelationId && (
        <RelationEvidenceDetail relationId={openRelationId} workspaceId={workspaceId} />
      )}

      {path.truncations.length > 0 && (
        <p className="gpath__truncation">
          {path.truncations
            .map(
              (item) =>
                `${nameById.get(item.entity_id) ?? item.entity_id} 共关联 ${item.total_edges} 条，` +
                `本次仅展示前 ${item.shown_edges} 条`,
            )
            .join('；')}
        </p>
      )}
    </div>
  )
}
