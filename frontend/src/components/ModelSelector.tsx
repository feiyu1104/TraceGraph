import type { ModelInfo } from '../api/types'

/**
 * 模型选择器只认后端 `/models` 给出的 ID。
 * 它不知道、也无从填写网关地址或密钥 —— 后端从来不提供这两样东西。
 */
interface ModelSelectorProps {
  models: ModelInfo[]
  selected: string
  defaultId: string
  onChange: (generatorId: string) => void
  disabled: boolean
  error: string | null
}

function optionLabel(model: ModelInfo): string {
  const name = model.model ? `${model.label}（${model.model}）` : model.label
  return model.available ? name : `${name} — 不可用：${model.reason}`
}

export default function ModelSelector({
  models,
  selected,
  defaultId,
  onChange,
  disabled,
  error,
}: ModelSelectorProps) {
  const current = models.find((model) => model.id === selected)

  return (
    <div className="models">
      <label className="field">
        <span className="field__label">回答模型</span>
        <select
          className="field__input"
          value={selected}
          onChange={(event) => onChange(event.target.value)}
          disabled={disabled}
        >
          {models.map((model) => (
            <option key={model.id} value={model.id} disabled={!model.available}>
              {optionLabel(model)}
            </option>
          ))}
        </select>
      </label>

      {error && (
        <p className="models__note models__note--warn" role="status">
          {error}
        </p>
      )}

      {current && (
        <p className="models__note">
          {current.kind === 'extractive'
            ? '离线摘录：直接引用知识库原文，不调用任何外部模型。'
            : '在线模型：只提交本次召回的原文证据，引用 ID 由后端逐条校验。'}
          {current.id === defaultId && ' 这是服务端默认模型。'}
        </p>
      )}
    </div>
  )
}
