import { useState } from 'react'

import { ApiRequestError, createWorkspace } from '../api/client'
import type { AdapterInfo, WorkspaceInfo } from '../api/types'
import ErrorState from './ErrorState'

interface WorkspaceBarProps {
  workspaces: WorkspaceInfo[]
  selectedId: string
  onSelect: (workspaceId: string) => void
  /** 内置适配器清单，同时是「新建知识库」里唯一的适配器选项来源。 */
  adapters: AdapterInfo[]
  adaptersError: string | null
  /** 知识库清单读取失败时的明确提示；不是错误清单，是回退说明。 */
  listNotice: string | null
  /** 查询或上传进行中：此时不允许切换知识库。 */
  disabled: boolean
  onCreated: (workspace: WorkspaceInfo) => void
}

function adapterLabel(adapters: AdapterInfo[], adapterId: string): string {
  return adapters.find((adapter) => adapter.id === adapterId)?.label ?? adapterId
}

export default function WorkspaceBar({
  workspaces,
  selectedId,
  onSelect,
  adapters,
  adaptersError,
  listNotice,
  disabled,
  onCreated,
}: WorkspaceBarProps) {
  const [formOpen, setFormOpen] = useState(false)
  const [name, setName] = useState('')
  const [adapterId, setAdapterId] = useState(adapters[0]?.id ?? '')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<ApiRequestError | null>(null)
  const [created, setCreated] = useState<WorkspaceInfo | null>(null)

  const current = workspaces.find((workspace) => workspace.id === selectedId)
  // 清单可能比选择慢一拍（或读取失败回退），这里如实显示「未知」而不是编一个名字。
  const currentLabel = current?.name ?? selectedId
  const currentAdapter = current
    ? adapterLabel(adapters, current.adapter_id)
    : '—'

  async function submit() {
    const trimmed = name.trim()
    if (!trimmed || creating) return
    setCreating(true)
    setCreateError(null)
    setCreated(null)
    try {
      const workspace = await createWorkspace(trimmed, adapterId)
      setCreated(workspace)
      setName('')
      setFormOpen(false)
      // 列表与当前选择都由 App 统一持有，这里只上报事实。
      onCreated(workspace)
    } catch (error) {
      // 失败原因原样来自后端 detail；invalid_adapter 不会被改写成别的适配器。
      setCreateError(
        error instanceof ApiRequestError
          ? error
          : new ApiRequestError(0, 'unknown_error', String(error)),
      )
    } finally {
      setCreating(false)
    }
  }

  return (
    <section className="workspace-bar" aria-label="知识库">
      <div className="workspace-bar__main">
        <label className="field workspace-bar__field">
          <span className="field__label">知识库</span>
          <select
            className="field__input"
            value={selectedId}
            onChange={(event) => onSelect(event.target.value)}
            disabled={disabled}
          >
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.name} · {adapterLabel(adapters, workspace.adapter_id)}
              </option>
            ))}
          </select>
        </label>

        <div className="workspace-bar__meta">
          <span className="badge">适配器：{currentAdapter}</span>
          <span className="badge">
            检索：{current?.adapter_id === 'medical' ? '混合检索' : '关键词检索'}
          </span>
          <button
            type="button"
            className="button button--ghost"
            onClick={() => {
              setFormOpen((open) => !open)
              setCreated(null)
            }}
            disabled={disabled || adapters.length === 0}
          >
            {formOpen ? '收起' : '新建知识库'}
          </button>
        </div>
      </div>

      {disabled && (
        <p className="workspace-bar__notice">查询或上传进行中，暂时不能切换知识库。</p>
      )}

      {listNotice && (
        <p className="workspace-bar__notice workspace-bar__notice--warn" role="status">
          {listNotice}
        </p>
      )}

      {created && (
        <p className="workspace-bar__notice workspace-bar__notice--ok" role="status">
          已创建并切换到「{created.name}」（{adapterLabel(adapters, created.adapter_id)}）。
        </p>
      )}

      {formOpen && (
        <form
          className="workspace-create"
          onSubmit={(event) => {
            event.preventDefault()
            void submit()
          }}
        >
          <label className="field">
            <span className="field__label">名称</span>
            <input
              className="field__input"
              value={name}
              maxLength={60}
              autoComplete="off"
              disabled={creating}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：心内科指南"
            />
          </label>

          {adapters.length === 0 ? (
            <p className="workspace-create__hint workspace-create__hint--warn">
              {adaptersError ?? '适配器清单还没有读到，暂时无法新建知识库。'}
            </p>
          ) : (
            <label className="field">
              <span className="field__label">内置适配器</span>
              <select
                className="field__input"
                value={adapterId}
                disabled={creating}
                onChange={(event) => setAdapterId(event.target.value)}
              >
                {adapters.map((adapter) => (
                  <option key={adapter.id} value={adapter.id}>
                    {adapter.label}（{adapter.id}）
                  </option>
                ))}
              </select>
            </label>
          )}

          {adapters.find((adapter) => adapter.id === adapterId) && (
            <p className="workspace-create__hint">
              {adapters.find((adapter) => adapter.id === adapterId)?.description}
              建库后不能修改适配器。
            </p>
          )}

          <div className="workspace-create__actions">
            <button
              type="submit"
              className="button button--primary"
              disabled={creating || !name.trim() || adapters.length === 0}
            >
              {creating ? '创建中…' : '创建'}
            </button>
            <button
              type="button"
              className="button"
              onClick={() => {
                setFormOpen(false)
                setCreateError(null)
              }}
              disabled={creating}
            >
              取消
            </button>
          </div>

          {createError && <ErrorState error={createError} />}
        </form>
      )}

      {!formOpen && current && (
        <p className="workspace-bar__notice">
          当前知识库「{currentLabel}」使用 {currentAdapter} 适配器；上传与查询都作用于它。
        </p>
      )}
    </section>
  )
}
