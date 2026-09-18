export type TabKey = 'qa' | 'knowledge' | 'graph'

const TABS: { key: TabKey; label: string; hint: string }[] = [
  { key: 'qa', label: '知识问答', hint: '按证据回答当前知识库里的问题' },
  { key: 'knowledge', label: '文档与知识', hint: '上传、抽取、审核并发布到图谱' },
  { key: 'graph', label: '图谱浏览', hint: '查看已经发布到图谱的实体与关系' },
]

interface TabBarProps {
  active: TabKey
  onChange: (tab: TabKey) => void
  /**
   * 上传、抽取、审核或发布进行中：此时不允许切换功能页。
   * 切走「文档与知识」会卸载工作台，跑着的写入既会丢掉界面状态，
   * 也会让页面误以为它已经结束。
   */
  disabled: boolean
}

export default function TabBar({ active, onChange, disabled }: TabBarProps) {
  return (
    <nav className="tabs" aria-label="功能分区">
      {TABS.map((tab) => (
        <button
          key={tab.key}
          type="button"
          className={`tabs__tab${active === tab.key ? ' tabs__tab--active' : ''}`}
          aria-current={active === tab.key ? 'page' : undefined}
          onClick={() => onChange(tab.key)}
          disabled={disabled}
        >
          <span className="tabs__label">{tab.label}</span>
          <span className="tabs__hint">{tab.hint}</span>
        </button>
      ))}
    </nav>
  )
}
