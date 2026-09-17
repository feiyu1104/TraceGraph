import type { Claim } from '../api/types'
import ClaimList from './ClaimList'

// 这段文案与后端 generation/service.py 的 _DERIVED_NOTICE 一致：
// 多跳结果不是本实体的原文结论，界面上必须说清楚，避免被读成医学结论。
const NOTICE = '推导关联，不是当前实体的直接原文结论'

interface DerivedAssociationsProps {
  associations: Claim[]
  evidenceIndex: Map<string, number>
  onSelectEvidence: (evidenceId: string) => void
}

export default function DerivedAssociations({
  associations,
  evidenceIndex,
  onSelectEvidence,
}: DerivedAssociationsProps) {
  if (associations.length === 0) return null

  return (
    <section className="derived" aria-label="多跳推导关联">
      <header className="derived__head">
        <h3 className="derived__title">图路径推导的关联</h3>
        <span className="badge badge--derived">{NOTICE}</span>
      </header>
      <p className="derived__explain">
        以下内容由其他实体的记录沿图路径推导得到，用于提示可能的关联方向，
        不构成本实体的结论，也不构成医学建议。
      </p>
      <ClaimList
        claims={associations}
        evidenceIndex={evidenceIndex}
        onSelectEvidence={onSelectEvidence}
        variant="derived"
      />
    </section>
  )
}
