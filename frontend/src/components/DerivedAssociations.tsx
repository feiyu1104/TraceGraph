import type { Claim, Evidence } from '../api/types'
import GraphPath from './GraphPath'

// 这段文案与后端 generation/service.py 的 _DERIVED_NOTICE 一致：
// 多跳结果不是本实体的原文结论，界面上必须明确区分直接事实与推导。
const NOTICE = '推导关联，不是当前实体的直接原文结论'

interface DerivedAssociationsProps {
  associations: Claim[]
  evidences: Evidence[]
  workspaceId: string
}

export default function DerivedAssociations({
  associations,
  evidences,
  workspaceId,
}: DerivedAssociationsProps) {
  const paths = evidences.filter(
    (evidence) => evidence.graph_path && evidence.graph_path.steps.length > 1,
  )
  if (associations.length === 0 || paths.length === 0) return null

  return (
    <section className="derived" aria-label="多跳推导关联">
      <header className="derived__head">
        <h3 className="derived__title">推导路径</h3>
        <span className="badge badge--derived">{NOTICE}</span>
      </header>
      <p className="derived__explain">
        以下路径说明系统如何从已知实体找到关联信息。路径是检索线索，
        不是原文直接写出的结论。
      </p>
      <div className="derived__paths">
        {paths.map((evidence) => (
          <GraphPath
            key={evidence.id}
            path={evidence.graph_path!}
            workspaceId={workspaceId}
            showHopBadge={false}
          />
        ))}
      </div>
    </section>
  )
}
