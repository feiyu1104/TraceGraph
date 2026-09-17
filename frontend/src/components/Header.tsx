import type { SystemInfo } from '../api/types'
import SystemStatus from './SystemStatus'

interface HeaderProps {
  system: SystemInfo | null
  systemError: string | null
  healthy: boolean | null
}

export default function Header({ system, systemError, healthy }: HeaderProps) {
  return (
    <header className="app-header">
      <div className="app-header__brand">
        <h1 className="app-header__title">TraceGraph</h1>
        <p className="app-header__tagline">
          证据优先的专业知识检索与回答 · 每条结论都可回溯到 DUTMed 原文
        </p>
      </div>
      <SystemStatus system={system} systemError={systemError} healthy={healthy} />
    </header>
  )
}
