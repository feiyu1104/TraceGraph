import type { Claim } from '../api/types'

interface ClaimListProps {
  claims: Claim[]
  /** evidence id → 证据在列表中的序号，用于渲染可点击的引用角标 */
  evidenceIndex: Map<string, number>
  onSelectEvidence: (evidenceId: string) => void
  variant?: 'fact' | 'derived'
}

export default function ClaimList({
  claims,
  evidenceIndex,
  onSelectEvidence,
  variant = 'fact',
}: ClaimListProps) {
  return (
    <ol className={`claims claims--${variant}`}>
      {claims.map((claim, index) => (
        <li key={`${index}-${claim.text.slice(0, 12)}`} className="claims__item">
          <span className="claims__text">{claim.text}</span>
          <span className="claims__citations">
            {claim.evidence_ids.map((evidenceId) => {
              const position = evidenceIndex.get(evidenceId)
              return (
                <button
                  key={evidenceId}
                  type="button"
                  className="citation"
                  onClick={() => onSelectEvidence(evidenceId)}
                  title="查看这条结论对应的证据"
                >
                  [{position ?? '?'}]
                </button>
              )
            })}
          </span>
        </li>
      ))}
    </ol>
  )
}
