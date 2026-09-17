import type { KeyboardEvent, ReactNode } from 'react'

// 示例问题全部来自真实 DUTMed 数据，可在「图谱浏览」中查到对应实体。
const MEDICAL_EXAMPLES = [
  '百日咳用什么药',
  '苯中毒有哪些症状',
  '肺泡蛋白质沉积症需要做什么检查',
]

const MEDICAL_DISCLAIMER =
  '本系统仅用于知识检索与学习，输出不构成诊断或治疗建议；紧急情况请立即就医。'

interface AdapterCopy {
  label: string
  placeholder: string
  /** 只放当前知识库里确实存在的示例；没有就不放，不编造内容。 */
  examples: readonly string[]
  disclaimer: string | null
}

// 文案跟着知识库的适配器走：在个人笔记库里摆医疗示例、挂诊断免责声明，
// 既误导也失真。未知适配器一律走通用文案，而不是默认成医疗。
const ADAPTER_COPY: Record<string, AdapterCopy> = {
  medical: {
    label: '临床或知识库问题',
    placeholder: '例如：百日咳用什么药',
    examples: MEDICAL_EXAMPLES,
    disclaimer: MEDICAL_DISCLAIMER,
  },
  general: {
    label: '知识库问题',
    placeholder: '输入要在这个知识库里查找的问题',
    examples: [],
    disclaimer: null,
  },
  'personal-notes': {
    label: '笔记问题',
    placeholder: '输入要在笔记里查找的问题',
    examples: [],
    disclaimer: null,
  },
}

const FALLBACK_COPY: AdapterCopy = {
  label: '知识库问题',
  placeholder: '输入要查找的问题',
  examples: [],
  disclaimer: null,
}

const HOP_OPTIONS = [
  { value: 1, label: '一跳', hint: '仅原文事实' },
  { value: 2, label: '二跳', hint: '含推导关联' },
  { value: 3, label: '三跳', hint: '含推导关联' },
]

interface QuestionFormProps {
  question: string
  onQuestionChange: (value: string) => void
  maxHops: number
  onMaxHopsChange: (value: number) => void
  onSubmit: () => void
  onClear: () => void
  busy: boolean
  /** 模型选择器；与跳数一样属于「这次怎么问」，因此放在问题输入区。 */
  modelSelector?: ReactNode
  /** 只有走混合检索的知识库才有多跳，其余知识库把跳数选择整体禁用。 */
  multiHopEnabled: boolean
  /** 禁用多跳的原因，直接显示在跳数区下方；可用时为 null。 */
  retrievalNotice: string | null
  /** 当前知识库记录的适配器 ID；问题区文案按它切换。 */
  adapterId: string
}

export default function QuestionForm({
  question,
  onQuestionChange,
  maxHops,
  onMaxHopsChange,
  onSubmit,
  onClear,
  busy,
  modelSelector,
  multiHopEnabled,
  retrievalNotice,
  adapterId,
}: QuestionFormProps) {
  const copy = ADAPTER_COPY[adapterId] ?? FALLBACK_COPY

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter 直接查询，Shift+Enter 换行 —— 键盘用户不必去点按钮。
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      onSubmit()
    }
  }

  return (
    <form
      className="question"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit()
      }}
    >
      <label className="field">
        <span className="field__label">{copy.label}</span>
        <textarea
          className="field__input field__input--area"
          value={question}
          onChange={(event) => onQuestionChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={copy.placeholder}
          rows={3}
          autoComplete="off"
        />
      </label>

      {modelSelector}

      <fieldset className="hops" disabled={!multiHopEnabled}>
        <legend className="field__label">检索跳数</legend>
        <div className="hops__options">
          {HOP_OPTIONS.map((option) => (
            <label
              key={option.value}
              className={`hops__option${maxHops === option.value ? ' hops__option--active' : ''}`}
            >
              <input
                type="radio"
                name="max-hops"
                value={option.value}
                checked={maxHops === option.value}
                onChange={() => onMaxHopsChange(option.value)}
              />
              <span className="hops__title">{option.label}</span>
              <span className="hops__hint">{option.hint}</span>
            </label>
          ))}
        </div>
        {retrievalNotice && <p className="hops__notice">{retrievalNotice}</p>}
      </fieldset>

      <div className="question__actions">
        <button type="submit" className="button button--primary" disabled={busy || !question.trim()}>
          {busy ? '查询中…' : '查询知识库'}
        </button>
        <button type="button" className="button" onClick={onClear} disabled={busy}>
          清空
        </button>
      </div>

      {copy.examples.length > 0 && (
        <div className="examples">
          <span className="examples__label">示例：</span>
          {copy.examples.map((example) => (
            <button
              key={example}
              type="button"
              className="examples__item"
              onClick={() => onQuestionChange(example)}
              disabled={busy}
            >
              {example}
            </button>
          ))}
        </div>
      )}

      {copy.disclaimer && <p className="disclaimer">{copy.disclaimer}</p>}
    </form>
  )
}
