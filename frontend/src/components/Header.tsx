import SystemStatus from './SystemStatus'

interface HeaderProps {
  systemError: string | null
  healthy: boolean | null
  modelLabel: string
  modelAvailable: boolean
  modelKind: string
  modelLink: 'checking' | 'ready' | 'failed'
}

export default function Header(props: HeaderProps) {
  return (
    <header className="app-header">
      <div className="app-header__brand">
        <h1 className="app-header__title">TraceGraph</h1>
        <p className="app-header__tagline">
          证据优先的知识工作台。让文档、关系与回答保持可追溯
        </p>
      </div>
      <SystemStatus {...props} />
    </header>
  )
}
