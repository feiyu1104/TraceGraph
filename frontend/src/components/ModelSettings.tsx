import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  ApiRequestError,
  deleteModelConnection,
  discoverConnectionModels,
  discoverModels,
  fetchModelConnections,
  fetchModels,
  saveModelConnection,
  setDefaultModel,
} from '../api/client'
import type {
  ConnectionModel,
  ModelConnection,
  ModelInfo,
  ModelsResponse,
} from '../api/types'
import EmptyState from './EmptyState'
import ErrorState from './ErrorState'

interface ModelSettingsProps {
  models: ModelInfo[]
  defaultModelId: string
  onModelsChanged: (listing: ModelsResponse) => void
  onBusyChange: (busy: boolean) => void
}

interface Draft {
  id: string
  label: string
  baseUrl: string
  timeout: string
  apiKey: string
  models: ConnectionModel[]
}

const EMPTY_DRAFT: Draft = {
  id: '',
  label: '',
  baseUrl: '',
  timeout: '15',
  apiKey: '',
  models: [],
}

const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/

const PROVIDER_PRESETS = [
  { id: 'openai', name: 'OpenAI', connectionId: 'openai', baseUrl: 'https://api.openai.com/v1' },
  { id: 'deepseek', name: 'DeepSeek', connectionId: 'deepseek', baseUrl: 'https://api.deepseek.com/v1' },
  { id: 'openrouter', name: 'OpenRouter', connectionId: 'openrouter', baseUrl: 'https://openrouter.ai/api/v1' },
  { id: 'siliconflow', name: '硅基流动', connectionId: 'siliconflow', baseUrl: 'https://api.siliconflow.cn/v1' },
  { id: 'moonshot', name: '月之暗面 Kimi', connectionId: 'moonshot', baseUrl: 'https://api.moonshot.cn/v1' },
  { id: 'zhipu', name: '智谱 GLM', connectionId: 'zhipu', baseUrl: 'https://open.bigmodel.cn/api/paas/v4' },
] as const

type ProviderId = (typeof PROVIDER_PRESETS)[number]['id'] | 'custom'

function providerIdFor(baseUrl: string): ProviderId {
  const normalized = baseUrl.trim().replace(/\/$/, '')
  return PROVIDER_PRESETS.find((provider) => provider.baseUrl === normalized)?.id ?? 'custom'
}

function toDraft(connection: ModelConnection): Draft {
  return {
    id: connection.id,
    label: connection.label,
    baseUrl: connection.base_url,
    timeout: String(connection.timeout),
    apiKey: '',
    models: connection.models.map((model) => ({ ...model })),
  }
}

function toApiError(error: unknown): ApiRequestError {
  return error instanceof ApiRequestError
    ? error
    : new ApiRequestError(0, 'unknown_error', String(error))
}

function localModelId(connectionId: string, remoteId: string, used: Set<string>): string {
  const prefix = connectionId || 'model'
  const cleaned = remoteId
    .toLowerCase()
    .replace(/[^a-z0-9._:-]+/g, '-')
    .replace(/^[^a-z0-9]+/, '')
  const base = `${prefix}:${cleaned || 'model'}`.slice(0, 64)
  let candidate = base
  let index = 2
  while (used.has(candidate) || candidate === 'extractive') {
    const suffix = `-${index}`
    candidate = `${base.slice(0, 64 - suffix.length)}${suffix}`
    index += 1
  }
  return candidate
}

export default function ModelSettings({
  models,
  defaultModelId,
  onModelsChanged,
  onBusyChange,
}: ModelSettingsProps) {
  const [connections, setConnections] = useState<ModelConnection[] | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  const [discovered, setDiscovered] = useState<string[]>([])
  const [loadingError, setLoadingError] = useState<ApiRequestError | null>(null)
  const [actionError, setActionError] = useState<ApiRequestError | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [deleteArmed, setDeleteArmed] = useState(false)
  const [providerId, setProviderId] = useState<ProviderId>('custom')

  const selected = connections?.find((connection) => connection.id === selectedId) ?? null
  const busy = busyAction !== null
  const isNew = selected === null

  const loadConnections = useCallback(async (preferredId?: string) => {
    try {
      const listing = await fetchModelConnections()
      setConnections(listing.connections)
      setLoadingError(null)
      const nextId =
        (preferredId && listing.connections.some((item) => item.id === preferredId)
          ? preferredId
          : listing.connections[0]?.id) ?? null
      setSelectedId(nextId)
      setDraft(
        nextId
          ? toDraft(listing.connections.find((item) => item.id === nextId)!)
          : EMPTY_DRAFT,
      )
      setProviderId(
        nextId
          ? providerIdFor(listing.connections.find((item) => item.id === nextId)!.base_url)
          : 'custom',
      )
    } catch (error) {
      setConnections([])
      setSelectedId(null)
      setDraft(EMPTY_DRAFT)
      setProviderId('custom')
      setLoadingError(toApiError(error))
    }
  }, [])

  useEffect(() => {
    void loadConnections()
  }, [loadConnections])

  useEffect(() => {
    onBusyChange(busy)
    return () => onBusyChange(false)
  }, [busy, onBusyChange])

  const connectionModelIds = useMemo(
    () => new Set(connections?.flatMap((connection) => connection.models.map((model) => model.id))),
    [connections],
  )

  function chooseConnection(connection: ModelConnection) {
    if (busy) return
    setSelectedId(connection.id)
    setDraft(toDraft(connection))
    setProviderId(providerIdFor(connection.base_url))
    setDiscovered([])
    setActionError(null)
    setNotice(null)
    setDeleteArmed(false)
  }

  function startNew() {
    if (busy) return
    setSelectedId(null)
    setDraft(EMPTY_DRAFT)
    setProviderId('custom')
    setDiscovered([])
    setActionError(null)
    setNotice(null)
    setDeleteArmed(false)
  }

  function chooseProvider(nextId: ProviderId) {
    setProviderId(nextId)
    if (nextId === 'custom') return
    const preset = PROVIDER_PRESETS.find((provider) => provider.id === nextId)
    if (!preset) return
    setDraft((current) => {
      const presetIds = new Set<string>(
        PROVIDER_PRESETS.map((provider) => provider.connectionId),
      )
      return {
        ...current,
        id: isNew && (!current.id || presetIds.has(current.id)) ? preset.connectionId : current.id,
        label: preset.name,
        baseUrl: preset.baseUrl,
      }
    })
    setDiscovered([])
    setActionError(null)
    setNotice(null)
  }

  async function discover() {
    const timeout = Number(draft.timeout)
    if (!draft.baseUrl.trim()) {
      setActionError(new ApiRequestError(0, 'invalid_form', '请先填写 API 地址。'))
      return
    }
    if (!Number.isFinite(timeout) || timeout < 1 || timeout > 60) {
      setActionError(new ApiRequestError(0, 'invalid_form', '超时时间必须在 1 到 60 秒之间。'))
      return
    }
    const canUseSavedKey =
      selected &&
      !draft.apiKey &&
      draft.baseUrl.trim().replace(/\/$/, '') === selected.base_url &&
      timeout === selected.timeout
    if (!draft.apiKey && !canUseSavedKey) {
      setActionError(
        new ApiRequestError(0, 'missing_api_key', '新连接或已修改地址时，需要填写 API Key 才能获取模型。'),
      )
      return
    }

    setBusyAction('discover')
    setActionError(null)
    setNotice(null)
    try {
      const result = canUseSavedKey
        ? await discoverConnectionModels(selected.id)
        : await discoverModels(draft.baseUrl.trim(), draft.apiKey.trim(), timeout)
      setDiscovered(result.models)
      setNotice(
        result.models.length > 0
          ? `已从 ${result.base_url} 获取 ${result.models.length} 个模型。`
          : '连接成功，但服务端没有返回模型。你仍可手动添加。',
      )
    } catch (error) {
      setActionError(toApiError(error))
    } finally {
      setBusyAction(null)
    }
  }

  function addRemoteModel(remoteId: string) {
    const used = new Set([...connectionModelIds, ...draft.models.map((model) => model.id)])
    setDraft((current) => ({
      ...current,
      models: [
        ...current.models,
        { id: localModelId(current.id, remoteId, used), label: remoteId, model: remoteId },
      ],
    }))
  }

  function addManualModel() {
    const used = new Set([...connectionModelIds, ...draft.models.map((model) => model.id)])
    setDraft((current) => ({
      ...current,
      models: [
        ...current.models,
        { id: localModelId(current.id, 'model', used), label: '', model: '' },
      ],
    }))
  }

  function updateModel(index: number, changes: Partial<ConnectionModel>) {
    setDraft((current) => ({
      ...current,
      models: current.models.map((model, position) =>
        position === index ? { ...model, ...changes } : model,
      ),
    }))
  }

  function removeModel(index: number) {
    setDraft((current) => ({
      ...current,
      models: current.models.filter((_, position) => position !== index),
    }))
  }

  async function save() {
    const id = draft.id.trim()
    const timeout = Number(draft.timeout)
    if (!id || id.length > 64 || !ID_PATTERN.test(id)) {
      setActionError(
        new ApiRequestError(
          0,
          'invalid_form',
          '连接 ID 最多 64 个字符，只能包含字母、数字与 . _ : -，并以字母或数字开头。',
        ),
      )
      return
    }
    if (!draft.baseUrl.trim()) {
      setActionError(new ApiRequestError(0, 'invalid_form', '请填写 API 地址。'))
      return
    }
    if (!Number.isFinite(timeout) || timeout < 1 || timeout > 60) {
      setActionError(new ApiRequestError(0, 'invalid_form', '超时时间必须在 1 到 60 秒之间。'))
      return
    }
    if (isNew && !draft.apiKey.trim()) {
      setActionError(new ApiRequestError(0, 'invalid_form', '创建连接时必须填写 API Key。'))
      return
    }
    if (
      draft.models.some(
        (model) => !model.id.trim() || !ID_PATTERN.test(model.id.trim()) || !model.model.trim(),
      )
    ) {
      setActionError(
        new ApiRequestError(0, 'invalid_form', '每个已启用模型都需要有效的本地 ID 和远端模型 ID。'),
      )
      return
    }

    setBusyAction('save')
    setActionError(null)
    setNotice(null)
    try {
      await saveModelConnection(id, {
        label: draft.label.trim(),
        base_url: draft.baseUrl.trim(),
        timeout,
        ...(draft.apiKey.trim() ? { api_key: draft.apiKey.trim() } : {}),
        models: draft.models.map((model) => ({
          id: model.id.trim(),
          label: model.label.trim(),
          model: model.model.trim(),
        })),
      })
      const listing = await fetchModels()
      onModelsChanged(listing)
      await loadConnections(id)
      setNotice('连接已保存，模型选择器已同步更新。')
    } catch (error) {
      setActionError(toApiError(error))
    } finally {
      setBusyAction(null)
    }
  }

  async function removeConnection() {
    if (!selected) return
    setBusyAction('delete')
    setActionError(null)
    setNotice(null)
    try {
      await deleteModelConnection(selected.id)
      const listing = await fetchModels()
      onModelsChanged(listing)
      await loadConnections()
      setNotice(`连接「${selected.label || selected.id}」已删除。`)
      setDeleteArmed(false)
    } catch (error) {
      setActionError(toApiError(error))
    } finally {
      setBusyAction(null)
    }
  }

  async function changeDefault(modelId: string) {
    setBusyAction('default')
    setActionError(null)
    setNotice(null)
    try {
      const listing = await setDefaultModel(modelId)
      onModelsChanged(listing)
      setNotice(`默认模型已切换为「${listing.models.find((model) => model.id === modelId)?.label ?? modelId}」。`)
    } catch (error) {
      setActionError(toApiError(error))
    } finally {
      setBusyAction(null)
    }
  }

  return (
    <section className="model-settings" aria-label="模型设置">
      <header className="model-settings__intro">
        <div>
          <h2 className="model-settings__title">模型设置</h2>
          <p className="model-settings__lead">
            连接兼容 OpenAI 接口的模型服务。密钥只保存在本机后端，页面不会读取或回显。
          </p>
        </div>
        <label className="field model-settings__default">
          <span className="field__label">全局默认模型</span>
          <select
            className="field__input"
            value={defaultModelId}
            disabled={busy}
            onChange={(event) => void changeDefault(event.target.value)}
          >
            {models.map((model) => (
              <option key={model.id} value={model.id} disabled={!model.available}>
                {model.label}{model.available ? '' : '（不可用）'}
              </option>
            ))}
          </select>
        </label>
      </header>

      {loadingError && <ErrorState error={loadingError} onRetry={() => void loadConnections()} />}

      <div className="model-settings__layout">
        <aside className="connection-list" aria-label="模型连接">
          <div className="connection-list__head">
            <div>
              <h3>连接</h3>
              <p>{connections?.length ?? 0} 个已保存</p>
            </div>
            <button type="button" className="button button--primary" onClick={startNew} disabled={busy}>
              新建连接
            </button>
          </div>
          {connections === null ? (
            <p className="connection-list__empty">正在读取连接…</p>
          ) : connections.length === 0 ? (
            <p className="connection-list__empty">还没有在线模型连接。离线摘录仍可正常使用。</p>
          ) : (
            <div className="connection-list__items">
              {connections.map((connection) => (
                <button
                  key={connection.id}
                  type="button"
                  className={`connection-list__item${selectedId === connection.id ? ' connection-list__item--active' : ''}`}
                  aria-pressed={selectedId === connection.id}
                  disabled={busy}
                  onClick={() => chooseConnection(connection)}
                >
                  <strong>{connection.label || connection.id}</strong>
                  <span>{connection.base_url}</span>
                  <small>{connection.models.length} 个模型</small>
                </button>
              ))}
            </div>
          )}
        </aside>

        <div className="connection-editor">
          {connections === null ? (
            <EmptyState title="正在准备模型设置" hint="连接清单读到后即可编辑。" />
          ) : (
            <>
              <div className="connection-editor__head">
                <div>
                  <h3>{isNew ? '新建模型连接' : `编辑 ${selected?.label || selected?.id}`}</h3>
                  <p>{isNew ? '先验证连接，再选择要在工作台中使用的模型。' : '留空 API Key 会沿用已保存的密钥。'}</p>
                </div>
                {selected?.has_api_key && <span className="badge badge--fact">密钥已保存</span>}
              </div>

              <div className="connection-form">
                <label className="field connection-form__provider">
                  <span className="field__label">服务商预设</span>
                  <select
                    className="field__input"
                    value={providerId}
                    disabled={busy}
                    onChange={(event) => chooseProvider(event.target.value as ProviderId)}
                  >
                    <option value="custom">自定义或本地模型服务</option>
                    {PROVIDER_PRESETS.map((provider) => (
                      <option key={provider.id} value={provider.id}>{provider.name}</option>
                    ))}
                  </select>
                  <span className="connection-form__hint">
                    选择预设会填写名称与 API 地址，保存前仍可修改。
                  </span>
                </label>
                <label className="field">
                  <span className="field__label">连接 ID</span>
                  <input
                    className="field__input field__input--mono"
                    value={draft.id}
                    maxLength={64}
                    autoComplete="off"
                    disabled={!isNew || busy}
                    placeholder="例如 openai-main"
                    onChange={(event) => setDraft((current) => ({ ...current, id: event.target.value }))}
                  />
                </label>
                <label className="field">
                  <span className="field__label">显示名称</span>
                  <input
                    className="field__input"
                    value={draft.label}
                    maxLength={80}
                    autoComplete="off"
                    disabled={busy}
                    placeholder="例如 主力模型"
                    onChange={(event) => setDraft((current) => ({ ...current, label: event.target.value }))}
                  />
                </label>
                <label className="field connection-form__wide">
                  <span className="field__label">API 地址</span>
                  <input
                    className="field__input field__input--mono"
                    type="url"
                    value={draft.baseUrl}
                    autoComplete="url"
                    disabled={busy}
                    placeholder="https://api.example.com/v1"
                    onChange={(event) => setDraft((current) => ({ ...current, baseUrl: event.target.value }))}
                  />
                </label>
                <label className="field">
                  <span className="field__label">API Key</span>
                  <input
                    className="field__input field__input--mono"
                    type="password"
                    value={draft.apiKey}
                    maxLength={4096}
                    autoComplete="new-password"
                    disabled={busy}
                    placeholder={isNew ? '创建时必填' : '留空则保持不变'}
                    onChange={(event) => setDraft((current) => ({ ...current, apiKey: event.target.value }))}
                  />
                </label>
                <label className="field">
                  <span className="field__label">超时（秒）</span>
                  <input
                    className="field__input"
                    type="number"
                    min="1"
                    max="60"
                    step="1"
                    value={draft.timeout}
                    disabled={busy}
                    onChange={(event) => setDraft((current) => ({ ...current, timeout: event.target.value }))}
                  />
                </label>
              </div>

              <div className="model-discovery">
                <div className="model-discovery__head">
                  <div>
                    <h3>可用模型</h3>
                    <p>获取服务端模型列表，选择后才会出现在问答与抽取页面。</p>
                  </div>
                  <div className="model-discovery__actions">
                    <button type="button" className="button" disabled={busy} onClick={() => void discover()}>
                      {busyAction === 'discover' ? '正在获取…' : '获取模型列表'}
                    </button>
                    <button type="button" className="button button--ghost" disabled={busy} onClick={addManualModel}>
                      手动添加
                    </button>
                  </div>
                </div>

                {discovered.length > 0 && (
                  <div className="discovered-models" aria-label="发现的模型">
                    {discovered.map((remoteId) => {
                      const added = draft.models.some((model) => model.model === remoteId)
                      return (
                        <div className="discovered-models__row" key={remoteId}>
                          <code>{remoteId}</code>
                          <button
                            type="button"
                            className="button button--compact"
                            disabled={busy || added}
                            onClick={() => addRemoteModel(remoteId)}
                          >
                            {added ? '已启用' : '启用'}
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}

                <div className="enabled-models">
                  <div className="enabled-models__labels" aria-hidden="true">
                    <span>显示名称</span><span>本地模型 ID</span><span>远端模型 ID</span><span />
                  </div>
                  {draft.models.length === 0 ? (
                    <p className="enabled-models__empty">尚未启用模型。保存空连接不会影响现有离线功能。</p>
                  ) : (
                    draft.models.map((model, index) => (
                      <div className="enabled-models__row" key={`${index}:${model.id}`}>
                        <label className="field">
                          <span className="sr-only">显示名称</span>
                          <input className="field__input" value={model.label} maxLength={80} disabled={busy} onChange={(event) => updateModel(index, { label: event.target.value })} />
                        </label>
                        <label className="field">
                          <span className="sr-only">本地模型 ID</span>
                          <input className="field__input field__input--mono" value={model.id} maxLength={64} disabled={busy} onChange={(event) => updateModel(index, { id: event.target.value })} />
                        </label>
                        <label className="field">
                          <span className="sr-only">远端模型 ID</span>
                          <input className="field__input field__input--mono" value={model.model} maxLength={200} disabled={busy} onChange={(event) => updateModel(index, { model: event.target.value })} />
                        </label>
                        <button type="button" className="button button--danger-ghost" disabled={busy} onClick={() => removeModel(index)}>
                          移除
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </div>

              {actionError && <ErrorState error={actionError} />}
              {notice && <p className="connection-editor__notice" role="status">{notice}</p>}

              <div className="connection-editor__footer">
                <button type="button" className="button button--primary" disabled={busy} onClick={() => void save()}>
                  {busyAction === 'save' ? '正在保存…' : '保存连接'}
                </button>
                {!isNew && (
                  deleteArmed ? (
                    <div className="delete-confirm" role="alert">
                      <span>删除后，这个连接下的模型会立即从选择器移除。</span>
                      <button type="button" className="button button--danger" disabled={busy} onClick={() => void removeConnection()}>
                        {busyAction === 'delete' ? '正在删除…' : '确认删除'}
                      </button>
                      <button type="button" className="button" disabled={busy} onClick={() => setDeleteArmed(false)}>取消</button>
                    </div>
                  ) : (
                    <button type="button" className="button button--danger-ghost" disabled={busy} onClick={() => setDeleteArmed(true)}>
                      删除连接
                    </button>
                  )
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
