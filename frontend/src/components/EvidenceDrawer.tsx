import { useEffect } from 'react'

import type { Evidence } from '../api/types'

export function evidenceAnchorId(evidenceId: string): string {
  return `evidence-${evidenceId}`
}

interface EvidenceDrawerProps {
  evidences: Evidence[]
  open: boolean
  onToggle: () => void
  /** 从引用角标跳过来的目标；nonce 变化即重新滚动一次 */
  reveal: { evidenceId: string; nonce: number } | null
}

export default function EvidenceDrawer({
  evidences,
  open,
  onToggle,
  reveal,
}: EvidenceDrawerProps) {
  useEffect(() => {
    if (!open || !reveal) return
    const anchor = document.getElementById(evidenceAnchorId(reveal.evidenceId))
    anchor?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [open, reveal])

  return (
    <section className="drawer">
      <button
        type="button"
        className="drawer__toggle"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className="drawer__title">引用原文（{evidences.length}）</span>
        <span className="meta">回答中的引用均可在这里核对</span>
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
              <dl className="evidences__source">
                <div><dt>来源</dt><dd>{evidence.source_name}</dd></div>
                <div><dt>位置</dt><dd>{evidence.locator}</dd></div>
                <div><dt>片段</dt><dd>{evidence.chunk_id}</dd></div>
              </dl>
              <p className="evidences__content">{evidence.content}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
