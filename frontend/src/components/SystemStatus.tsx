import type { SystemInfo } from '../api/types'

interface SystemStatusProps {
  system: SystemInfo | null
  systemError: string | null
  healthy: boolean | null
}

type Tone = 'default' | 'accent' | 'warn' | 'danger'

interface Chip {
  key: string
  label: string
  value: string
  tone: Tone
}

function graphBackendName(value: string): string {
  if (value === 'neo4j') return 'Neo4j'
  if (value === 'sqlite') return 'SQLite'
  return value
}

export default function SystemStatus({
  system,
  systemError,
  healthy,
}: SystemStatusProps) {
  if (systemError) {
    return (
      <div className="status-bar">
        <span className="chip chip--danger">
          <span className="chip__label">系统状态</span>不可用
        </span>
        <span className="status-bar__detail">{systemError}</span>
      </div>
    )
  }

  if (!system) {
    return (
      <div className="status-bar">
        <span className="chip">
          <span className="chip__label">系统状态</span>读取中…
        </span>
      </div>
    )
  }

  const graphDegraded = system.graph_degraded === 'true'
  const usesLlm = system.llm_configured === 'true'

  const chips: Chip[] = [
    {
      key: 'graph',
      label: '图后端',
      value: graphBackendName(system.graph_backend),
      tone: graphDegraded ? 'warn' : 'accent',
    },
    {
      key: 'generator',
      label: '生成器',
      value: usesLlm ? '大模型' : '离线摘录',
      tone: usesLlm ? 'accent' : 'default',
    },
  ]
  if (usesLlm) {
    chips.push({
      key: 'model',
      label: '模型',
      value: system.llm_model || '未命名',
      tone: 'default',
    })
  }
  chips.push({
    key: 'health',
    label: '健康',
    value: healthy === null ? '检查中' : healthy ? '正常' : '异常',
    tone: healthy === false ? 'danger' : 'default',
  })

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
      {graphDegraded && (
        <p className="status-bar__detail status-bar__detail--warn" role="status">
          图后端已降级：请求 {graphBackendName(system.graph_requested ?? '')}，
          实际使用 {graphBackendName(system.graph_backend)}。
          {system.graph_detail ? ` 原因：${system.graph_detail}` : ''}
        </p>
      )}
    </div>
  )
}
