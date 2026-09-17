import { useEffect } from 'react'

import type { Evidence } from '../api/types'
import GraphPath from './GraphPath'

export function evidenceAnchorId(evidenceId: string): string {
  return `evidence-${evidenceId}`
}

interface EvidenceDrawerProps {
  evidences: Evidence[]
  workspaceId: string
  open: boolean
  onToggle: () => void
  /** 从引用角标跳过来的目标；nonce 变化即重新滚动一次 */
  reveal: { evidenceId: string; nonce: number } | null
}

export default function EvidenceDrawer({
  evidences,
  workspaceId,
  open,
  onToggle,
  reveal,
}: EvidenceDrawerProps) {
  useEffect(() => {
    if (!open || !reveal) return
    const anchor = document.getElementById(evidenceAnchorId(reveal.evidenceId))
    anchor?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [open, reveal])

  const graphCount = evidences.filter((evidence) => evidence.graph_path !== null).length

  return (
    <section className="drawer">
      <button
        type="button"
        className="drawer__toggle"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className="drawer__title">证据来源（{evidences.length}）</span>
        <span className="meta">
          图路径 {graphCount} 条 · 关键词 {evidences.length - graphCount} 条
        </span>
        <span className="drawer__chevron" aria-hidden="true">
          {open ? '收起' : '展开'}
        </span>
      </button>

      {open && (
        <ol className="evidences">
          {evidences.map((evidence, index) => (
            <li
              key={evidence.id}
              id={evidenceAnchorId(evidence.id)}
              className={`evidences__item${
                reveal?.evidenceId === evidence.id ? ' evidences__item--highlight' : ''
              }`}
            >
              <div className="evidences__head">
                <span className="evidences__index">[{index + 1}]</span>
                <span className="badge badge--method">{evidence.retrieval_method}</span>
                <span className="meta">相关度 {evidence.retrieval_score.toFixed(3)}</span>
              </div>
              <p className="meta">
                来源：{evidence.source_name} · 位置：{evidence.locator} · 片段：
                {evidence.chunk_id}
              </p>
              <p className="evidences__content">{evidence.content}</p>
              {evidence.graph_path && (
                <GraphPath
                  path={evidence.graph_path}
                  workspaceId={workspaceId}
                  showHopBadge={false}
                />
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
