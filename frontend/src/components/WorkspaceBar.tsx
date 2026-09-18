import { useEffect, useState } from 'react'

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
  /**
   * 当前知识库是否走混合检索。由 App 按「ws-default + medical」判定后传入：
   * 只看 adapter_id 会把一个非默认的 medical 知识库错报成混合检索。
   * 这个值与 App 控制图谱浏览、多跳开关用的是同一个判定。
   */
  supportsGraph: boolean
  /** 查询或写入进行中：此时不允许切换知识库或功能页，也不允许建库。 */
  disabled: boolean
  /** 建库成功后回调；返回是否真的切换过去了（进行中的请求会阻止切换）。 */
  onCreated: (workspace: WorkspaceInfo) => boolean
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
  supportsGraph,
  disabled,
  onCreated,
}: WorkspaceBarProps) {
  const [formOpen, setFormOpen] = useState(false)
  const [name, setName] = useState('')
  const [adapterId, setAdapterId] = useState(() => adapters[0]?.id ?? '')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<ApiRequestError | null>(null)
  const [created, setCreated] = useState<{ workspace: WorkspaceInfo; switched: boolean } | null>(
    null,
  )

  // 适配器清单是异步到达的：组件挂载时它往往还是空的。清单变化后必须重新对齐
  // 选中项 —— 保留仍然有效的选择，为空或已失效就落到第一项（清单为空则保持空）。
  // 否则用户不动下拉框就建不了库：初值 '' 不在清单里，提交会被校验拦下。
  useEffect(() => {
    setAdapterId((current) =>
      adapters.some((adapter) => adapter.id === current) ? current : (adapters[0]?.id ?? ''),
    )
  }, [adapters])

  const current = workspaces.find((workspace) => workspace.id === selectedId)
  // 清单可能比选择慢一拍（或读取失败回退），这里如实显示「未知」而不是编一个名字。
  const currentLabel = current?.name ?? selectedId
  const currentAdapter = current ? adapterLabel(adapters, current.adapter_id) : '—'

  async function submit() {
    // 不能只靠按钮的 disabled：表单还可以由回车提交，而且进行中的请求会改变
    // disabled。这里是最后一道闸门。
    if (disabled || creating) return
    const trimmed = name.trim()
    if (!trimmed) return
    // 再对齐一次适配器：清单可能在表单打开后才到，或已经变化。
    const chosen = adapters.some((adapter) => adapter.id === adapterId)
      ? adapterId
      : adapters[0]?.id
    if (!chosen) return
    setCreating(true)
    setCreateError(null)
    setCreated(null)
    try {
      const workspace = await createWorkspace(trimmed, chosen)
      setName('')
      setFormOpen(false)
      // 列表与当前选择都由 App 统一持有，这里只上报事实。
      setCreated({ workspace, switched: onCreated(workspace) })
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

  // 创建与进行中的请求会互相打断，这里统一收敛成一个开关。
  const locked = disabled || creating

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
          <span className="badge">检索：{supportsGraph ? '混合检索' : '关键词检索'}</span>
          <button
            type="button"
            className="button button--ghost"
            onClick={() => {
              setFormOpen((open) => !open)
              setCreated(null)
            }}
            // 进行中不能「打开」表单；但已经打开的允许收起 —— 收起不改动任何状态。
            disabled={creating || adapters.length === 0 || (disabled && !formOpen)}
          >
            {formOpen ? '收起' : '新建知识库'}
          </button>
        </div>
      </div>

      {disabled && (
        <p className="workspace-bar__notice">
          查询或写入进行中，暂时不能切换知识库、切换功能页或新建。
        </p>
      )}

      {listNotice && (
        <p className="workspace-bar__notice workspace-bar__notice--warn" role="status">
          {listNotice}
        </p>
      )}

      {created && (
        <p
          className={`workspace-bar__notice workspace-bar__notice--${created.switched ? 'ok' : 'warn'}`}
          role="status"
        >
          {created.switched
            ? `已创建并切换到「${created.workspace.name}」（${adapterLabel(adapters, created.workspace.adapter_id)}）。`
            : `已创建「${created.workspace.name}」（${adapterLabel(adapters, created.workspace.adapter_id)}），` +
              '但查询或上传正在进行，本次没有切换；完成后请在上方选择器里选中它。'}
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
              disabled={locked}
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
                disabled={locked}
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
              disabled={locked || !name.trim() || adapters.length === 0}
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
              disabled={locked}
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
