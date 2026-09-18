interface SystemStatusProps {
  systemError: string | null
  healthy: boolean | null
  modelLabel: string
  modelAvailable: boolean
  modelKind: string
  modelLink: 'checking' | 'ready' | 'failed'
}

type Tone = 'default' | 'accent' | 'warn' | 'danger'

interface Chip {
  key: string
  label: string
  value: string
  tone: Tone
}

export default function SystemStatus({
  systemError,
  healthy,
  modelLabel,
  modelAvailable,
  modelKind,
  modelLink,
}: SystemStatusProps) {
  const connection = connectionStatus(
    healthy,
    modelAvailable,
    modelKind,
    modelLink,
  )
  const chips: Chip[] = [
    {
      key: 'model',
      label: '当前模型',
      value: modelLabel,
      tone: modelAvailable ? 'accent' : 'danger',
    },
    {
      key: 'connection',
      label: '连接',
      value: connection.value,
      tone: connection.tone,
    },
  ]

  return (
    <div className="status-bar">
      <ul className="status-bar__chips">
        {chips.map((chip) => (
          <li key={chip.key} className={`chip chip--${chip.tone}`}>
            <span className="chip__label">{chip.label}</span>
            {chip.value}
          </li>
        ))}
      </ul>
      {systemError && (
        <p className="status-bar__detail status-bar__detail--danger" role="status">
          {systemError}
        </p>
      )}
    </div>
  )
}

function connectionStatus(
  healthy: boolean | null,
  modelAvailable: boolean,
  modelKind: string,
  modelLink: 'checking' | 'ready' | 'failed',
): { value: string; tone: Tone } {
  if (healthy === false) return { value: '服务断开', tone: 'danger' }
  if (healthy === null) return { value: '检查中', tone: 'default' }
  if (!modelAvailable) return { value: '模型不可用', tone: 'danger' }
  if (modelKind === 'extractive') return { value: '本地可用', tone: 'default' }
  if (modelLink === 'ready') return { value: '连接正常', tone: 'accent' }
  if (modelLink === 'failed') return { value: '模型异常', tone: 'danger' }
  return { value: '等待验证', tone: 'default' }
}
