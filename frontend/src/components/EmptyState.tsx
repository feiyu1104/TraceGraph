interface EmptyStateProps {
  title: string
  hint?: string
}

export default function EmptyState({ title, hint }: EmptyStateProps) {
  return (
    <div className="empty">
      <p className="empty__title">{title}</p>
      {hint && <p className="empty__hint">{hint}</p>}
    </div>
  )
}
