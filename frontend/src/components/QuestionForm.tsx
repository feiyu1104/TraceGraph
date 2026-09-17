import type { KeyboardEvent, ReactNode } from 'react'

// 示例问题全部来自真实 DUTMed 数据，可在「图谱浏览」中查到对应实体。
const EXAMPLES = [
  '百日咳用什么药',
  '苯中毒有哪些症状',
  '肺泡蛋白质沉积症需要做什么检查',
]

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
}: QuestionFormProps) {
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
        <span className="field__label">临床或知识库问题</span>
        <textarea
          className="field__input field__input--area"
          value={question}
          onChange={(event) => onQuestionChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="例如：百日咳用什么药"
          rows={3}
          autoComplete="off"
        />
      </label>

      {modelSelector}

      <fieldset className="hops">
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
      </fieldset>

      <div className="question__actions">
        <button type="submit" className="button button--primary" disabled={busy || !question.trim()}>
          {busy ? '查询中…' : '查询知识库'}
        </button>
        <button type="button" className="button" onClick={onClear} disabled={busy}>
          清空
        </button>
      </div>

      <div className="examples">
        <span className="examples__label">示例：</span>
        {EXAMPLES.map((example) => (
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

      <p className="disclaimer">
        本系统仅用于知识检索与学习，输出不构成诊断或治疗建议；紧急情况请立即就医。
      </p>
    </form>
  )
}
