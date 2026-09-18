import { ApiRequestError } from '../api/client'
import type { Answer, AnswerStatus } from '../api/types'
import DerivedAssociations from './DerivedAssociations'
import EmptyState from './EmptyState'
import ErrorState from './ErrorState'
import EvidenceDrawer from './EvidenceDrawer'

type Tone = 'ok' | 'warn' | 'danger' | 'muted'

const STATUS_META: Record<AnswerStatus, { label: string; tone: Tone; hint: string }> = {
  answered: { label: '已基于证据作答', tone: 'ok', hint: '' },
  insufficient_evidence: {
    label: '证据不足',
    tone: 'warn',
    hint: '知识库中没有足够的原文事实支持回答，系统不做跨节点推测。',
  },
  conflicting_evidence: {
    label: '证据冲突',
    tone: 'warn',
    hint: '知识库中存在方向相反的证据，无法给出确定回答，需人工核对来源。',
  },
  out_of_scope: { label: '超出知识库范围', tone: 'muted', hint: '' },
  emergency_escalation: { label: '需要紧急处理', tone: 'danger', hint: '' },
  system_error: {
    label: '生成失败',
    tone: 'danger',
    hint: '检索已经完成，但生成阶段失败。下面的证据仍然可用。',
  },
}

interface AnswerPanelProps {
  answer: Answer | null
  error: ApiRequestError | null
  busy: boolean
  hasAsked: boolean
  /** 实际产出本次回答的生成器显示名；后端 metrics.generator 是权威来源。 */
  generatorLabel: string
  /** 本次回答真正生效的知识库 / 适配器 / 检索链路，全部取自响应 metrics。 */
  workspaceLabel: string
  workspaceId: string
  adapterLabel: string
  retrieverLabel: string
  drawerOpen: boolean
  onToggleDrawer: () => void
  reveal: { evidenceId: string; nonce: number } | null
  onSelectEvidence: (evidenceId: string) => void
  onRetry: () => void
}

export default function AnswerPanel({
  answer,
  error,
  busy,
  hasAsked,
  generatorLabel,
  workspaceLabel,
  workspaceId,
  adapterLabel,
  retrieverLabel,
  drawerOpen,
  onToggleDrawer,
  reveal,
  onSelectEvidence,
  onRetry,
}: AnswerPanelProps) {
  if (busy) {
    return (
      <div className="answer" aria-live="polite" aria-busy="true">
        {/* 只有一次请求，后端也只回一个响应：这里就只说这一件事。 */}
        <p className="answer__pending">正在检索证据并组织回答…</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="answer">
        <ErrorState error={error} onRetry={onRetry} />
      </div>
    )
  }

  if (!answer) {
    return (
      <div className="answer">
        <EmptyState
          title={hasAsked ? '本次查询没有返回结果' : '尚未发起查询'}
          hint="输入问题后按 Enter 或点击「查询知识库」。"
        />
      </div>
    )
  }

  const status = STATUS_META[answer.status]
  const evidenceIndex = new Map(
    answer.evidences.map((evidence, index) => [evidence.id, index + 1]),
  )
  const degraded = answer.metrics.generation_degraded === true

  return (
    <div className="answer">
      <div className={`banner banner--${status.tone}`}>
        <span className="banner__label">{status.label}</span>
        {status.hint && <span className="banner__hint">{status.hint}</span>}
      </div>

      {degraded && (
        <div className="banner banner--warn">
          <span className="banner__label">生成已降级</span>
          <span className="banner__hint">
            模型调用失败，已按 TRACEGRAPH_LLM_FALLBACK 改用离线摘录生成器，
            本次回答由摘录产出。
          </span>
        </div>
      )}

      {answer.error_code && (
        <p className="meta">错误码：{answer.error_code}</p>
      )}

      {answer.claims.length > 0 ? (
        <p className="answer__text answer__text--cited">
          {answer.claims.map((claim, claimIndex) => (
            <span className="answer__sentence" key={`${claimIndex}-${claim.text.slice(0, 12)}`}>
              {sentenceText(claim.text)}
              {claim.evidence_ids.map((evidenceId) => {
                const index = evidenceIndex.get(evidenceId)
                return index === undefined ? null : (
                  <button
                    key={evidenceId}
                    type="button"
                    className="citation"
                    aria-label={`查看引用 ${index}`}
                    onClick={() => onSelectEvidence(evidenceId)}
                  >
                    [{index}]
                  </button>
                )
              })}
              。
            </span>
          ))}
        </p>
      ) : answer.text ? (
        <p className="answer__text">{answer.text}</p>
      ) : null}

      <DerivedAssociations
        associations={answer.derived_associations}
        evidences={answer.evidences}
        workspaceId={workspaceId}
      />

      {answer.warnings.length > 0 && (
        <section className="block" aria-label="风险提示">
          <h3 className="block__title">风险提示</h3>
          <ul className="warnings">
            {answer.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </section>
      )}

      {answer.evidences.length > 0 && (
        <EvidenceDrawer
          evidences={answer.evidences}
          open={drawerOpen}
          onToggle={onToggleDrawer}
          reveal={reveal}
        />
      )}

      <dl className="metrics">
        <div>
          <dt>知识库</dt>
          <dd>{workspaceLabel}</dd>
        </div>
        <div>
          <dt>适配器</dt>
          <dd>{adapterLabel}</dd>
        </div>
        <div>
          <dt>检索方式</dt>
          <dd>{retrieverLabel}</dd>
        </div>
        <div>
          <dt>跳数</dt>
          <dd>{answer.metrics.max_hops ?? '未使用'}</dd>
        </div>
        <div>
          <dt>证据</dt>
          <dd>{answer.metrics.evidence_count ?? answer.evidences.length}</dd>
        </div>
        <div>
          <dt>推导关联</dt>
          <dd>{answer.metrics.derived_association_count ?? 0}</dd>
        </div>
        <div>
          <dt>生成模型</dt>
          <dd>{generatorLabel}</dd>
        </div>
      </dl>
    </div>
  )
}

function sentenceText(text: string): string {
  return text.trim().replace(/[。！？.!?]+$/, '')
}
