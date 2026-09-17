import { useCallback, useEffect, useState } from 'react'

import {
  ApiRequestError,
  askQuestion,
  fetchAdapters,
  fetchHealth,
  fetchModels,
  fetchSystem,
  fetchWorkspaces,
} from './api/client'
import type { AdapterInfo, Answer, ModelInfo, SystemInfo, WorkspaceInfo } from './api/types'
import AnswerPanel from './components/AnswerPanel'
import DocumentImport from './components/DocumentImport'
import GraphExplorer from './components/GraphExplorer'
import Header from './components/Header'
import ModelSelector from './components/ModelSelector'
import QuestionForm from './components/QuestionForm'
import WorkspaceBar from './components/WorkspaceBar'

const OFFLINE_MODEL: ModelInfo = {
  id: 'extractive',
  label: '离线摘录（不调用任何模型）',
  model: '',
  available: true,
  kind: 'extractive',
  reason: '',
}

// 清单读不到时的兼容回退，与服务端 DEFAULT_WORKSPACE_ID / 默认适配器一致。
// name 直接写 ID 而不是编一个中文名：这个名字我们并不知道，界面应该显示得出来
// 「现在显示的不是服务端的名字」。提示由 workspacesNotice 明确给出。
const FALLBACK_WORKSPACE: WorkspaceInfo = {
  id: 'ws-default',
  name: 'ws-default',
  adapter_id: 'medical',
  created_at: '',
}

const RETRIEVER_LABELS: Record<string, string> = {
  hybrid: '混合检索（关键词 + 图）',
  keyword: '关键词检索',
}

const KEYWORD_ONLY_NOTICE = '当前知识库暂时使用关键词检索。'

export default function App() {
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [systemError, setSystemError] = useState<string | null>(null)
  const [healthy, setHealthy] = useState<boolean | null>(null)

  const [models, setModels] = useState<ModelInfo[]>([OFFLINE_MODEL])
  const [defaultModelId, setDefaultModelId] = useState(OFFLINE_MODEL.id)
  const [modelNotice, setModelNotice] = useState<string | null>(null)
  const [generatorId, setGeneratorId] = useState(OFFLINE_MODEL.id)

  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([FALLBACK_WORKSPACE])
  const [workspacesNotice, setWorkspacesNotice] = useState<string | null>(null)
  const [adapters, setAdapters] = useState<AdapterInfo[]>([])
  const [adaptersError, setAdaptersError] = useState<string | null>(null)
  const [workspaceId, setWorkspaceId] = useState(FALLBACK_WORKSPACE.id)
  const [uploadBusy, setUploadBusy] = useState(false)

  const [question, setQuestion] = useState('')
  const [maxHops, setMaxHops] = useState(2)
  const [busy, setBusy] = useState(false)
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [queryError, setQueryError] = useState<ApiRequestError | null>(null)
  const [hasAsked, setHasAsked] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [reveal, setReveal] = useState<{ evidenceId: string; nonce: number } | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchSystem()
      .then((info) => {
        if (cancelled) return
        setSystem(info)
        setSystemError(null)
      })
      .catch((error: unknown) => {
        if (cancelled) return
        setSystemError(error instanceof Error ? error.message : '系统状态读取失败')
      })
    fetchHealth()
      .then(() => {
        if (!cancelled) setHealthy(true)
      })
      .catch(() => {
        if (!cancelled) setHealthy(false)
      })
    fetchModels()
      .then((listing) => {
        if (cancelled) return
        setModels(listing.models)
        setDefaultModelId(listing.default)
        setGeneratorId(listing.default)
        setModelNotice(null)
      })
      .catch(() => {
        if (cancelled) return
        // 清单读不到也不该挡住提问：离线摘录是内置选项，永远可选。
        setModelNotice('模型清单读取失败，当前只能使用离线摘录。')
      })
    fetchWorkspaces()
      .then((listing) => {
        if (cancelled) return
        setWorkspaces(listing.workspaces)
        setWorkspacesNotice(null)
        // 首选 ws-default；服务端没有它时退回清单第一项，而不是继续用一个不存在的 ID 发请求。
        setWorkspaceId((current) =>
          listing.workspaces.some((workspace) => workspace.id === current)
            ? current
            : (listing.workspaces.find((workspace) => workspace.id === FALLBACK_WORKSPACE.id)
                ?.id ?? listing.workspaces[0]?.id ?? current),
        )
      })
      .catch((error: unknown) => {
        if (cancelled) return
        // 不悄悄显示一份错误清单：明确说明当前是回退状态。
        setWorkspaces([FALLBACK_WORKSPACE])
        setWorkspaceId(FALLBACK_WORKSPACE.id)
        setWorkspacesNotice(
          `知识库清单读取失败（${error instanceof Error ? error.message : '未知原因'}），` +
            '列表可能不完整：当前只保证默认知识库 ws-default 可用。',
        )
      })
    fetchAdapters()
      .then((listing) => {
        if (cancelled) return
        setAdapters(listing.adapters)
        setAdaptersError(null)
      })
      .catch(() => {
        if (cancelled) return
        setAdapters([])
        setAdaptersError('适配器清单读取失败，暂时无法新建知识库；现有知识库仍可正常使用。')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const currentWorkspace = workspaces.find((workspace) => workspace.id === workspaceId) ?? null
  // 只有「默认知识库 + 医疗适配器」走混合检索（关键词 + Neo4j 多跳），其余知识库
  // 一律关键词检索。判定条件与后端 _retrieval_route 一致，这里只用来决定界面
  // 允许点什么；真正走哪条链路仍由服务端按 Workspace 记录决定。
  const supportsGraph =
    workspaceId === FALLBACK_WORKSPACE.id && currentWorkspace?.adapter_id === 'medical'
  // 非图知识库把跳数收敛到 1，但不覆盖用户的选择：切回医疗库时偏好还在。
  const effectiveMaxHops = supportsGraph ? maxHops : 1

  const selectWorkspace = useCallback((nextId: string) => {
    setWorkspaceId(nextId)
    // 上一次的回答属于另一个知识库，换库后整体清掉。问题文本保留 ——
    // 用户往往就是想拿同一个问题去另一个库里再问一次。
    setAnswer(null)
    setQueryError(null)
    setDrawerOpen(false)
    setReveal(null)
    setHasAsked(false)
  }, [])

  const handleCreated = useCallback(
    (workspace: WorkspaceInfo) => {
      setWorkspaces((current) =>
        current.some((item) => item.id === workspace.id) ? current : [...current, workspace],
      )
      selectWorkspace(workspace.id)
    },
    [selectWorkspace],
  )

  const runQuery = useCallback(async () => {
    const trimmed = question.trim()
    if (!trimmed) return
    setHasAsked(true)
    setAnswer(null)
    setQueryError(null)
    setDrawerOpen(false)
    setReveal(null)
    setBusy(true)
    try {
      // workspace_id 每次显式带上：不依赖服务端默认值，也不放进任何全局状态。
      setAnswer(await askQuestion(trimmed, workspaceId, effectiveMaxHops, generatorId))
    } catch (error) {
      setQueryError(
        error instanceof ApiRequestError
          ? error
          : new ApiRequestError(0, 'unknown_error', String(error)),
      )
    } finally {
      setBusy(false)
    }
  }, [effectiveMaxHops, generatorId, question, workspaceId])

  const selectEvidence = useCallback((evidenceId: string) => {
    setDrawerOpen(true)
    setReveal((current) => ({ evidenceId, nonce: (current?.nonce ?? 0) + 1 }))
  }, [])

  function clearAll() {
    setQuestion('')
    setAnswer(null)
    setQueryError(null)
    setHasAsked(false)
    setDrawerOpen(false)
    setReveal(null)
  }

  // 回答区显示的是**本次真正生效**的生成器，不是选择器里的当前值：
  // 降级时两者不同，用户必须看得到这个差别。
  const usedGeneratorId = answer?.metrics.generator
  const generatorLabel = usedGeneratorId
    ? (models.find((model) => model.id === usedGeneratorId)?.label ?? usedGeneratorId)
    : '—'

  // 同样取回答里带回来的事实，而不是当前选择器里的值：用户必须能看到
  // 「这次回答到底是谁产出的」。
  const usedWorkspaceId = answer?.metrics.workspace_id
  const usedWorkspaceLabel = usedWorkspaceId
    ? (workspaces.find((workspace) => workspace.id === usedWorkspaceId)?.name ?? usedWorkspaceId)
    : '—'
  const usedAdapterId = answer?.metrics.adapter_id
  const usedAdapterLabel = usedAdapterId
    ? (adapters.find((adapter) => adapter.id === usedAdapterId)?.label ?? usedAdapterId)
    : '—'
  const usedRetriever = answer?.metrics.retriever
  const usedRetrieverLabel = usedRetriever
    ? (RETRIEVER_LABELS[usedRetriever] ?? usedRetriever)
    : '—'

  const currentWorkspaceName = currentWorkspace?.name ?? workspaceId

  return (
    <div className="app">
      <Header system={system} systemError={systemError} healthy={healthy} />

      <WorkspaceBar
        workspaces={workspaces}
        selectedId={workspaceId}
        onSelect={selectWorkspace}
        adapters={adapters}
        adaptersError={adaptersError}
        listNotice={workspacesNotice}
        disabled={busy || uploadBusy}
        onCreated={handleCreated}
      />

      <main className="app__grid">
        <section className="app__column app__column--main" aria-label="知识问答">
          <div className="panel">
            <h2 className="panel__title">知识问答</h2>
            <QuestionForm
              question={question}
              onQuestionChange={setQuestion}
              maxHops={maxHops}
              onMaxHopsChange={setMaxHops}
              onSubmit={() => void runQuery()}
              onClear={clearAll}
              busy={busy}
              multiHopEnabled={supportsGraph}
              retrievalNotice={supportsGraph ? null : KEYWORD_ONLY_NOTICE}
              modelSelector={
                <ModelSelector
                  models={models}
                  selected={generatorId}
                  defaultId={defaultModelId}
                  onChange={setGeneratorId}
                  disabled={busy}
                  error={modelNotice}
                />
              }
            />
          </div>

          <div className="panel">
            <h2 className="panel__title">回答</h2>
            <AnswerPanel
              answer={answer}
              error={queryError}
              busy={busy}
              hasAsked={hasAsked}
              generatorLabel={generatorLabel}
              workspaceLabel={usedWorkspaceLabel}
              adapterLabel={usedAdapterLabel}
              retrieverLabel={usedRetrieverLabel}
              drawerOpen={drawerOpen}
              onToggleDrawer={() => setDrawerOpen((open) => !open)}
              reveal={reveal}
              onSelectEvidence={selectEvidence}
              onRetry={() => void runQuery()}
            />
          </div>
        </section>

        <section className="app__column app__column--side" aria-label="图谱浏览与文档导入">
          <div className="panel">
            <h2 className="panel__title">图谱浏览</h2>
            {supportsGraph ? (
              <GraphExplorer />
            ) : (
              // 图索引目前只有 DUTMed 医疗图，且没有 Workspace 维度：
              // 这里给说明，而不是伪造一张空图，也不把医疗图搬进别的知识库。
              <p className="panel__note">
                当前知识库尚未发布审核后的图关系，因此暂时只能使用文本检索。
              </p>
            )}
          </div>

          <div className="panel">
            <h2 className="panel__title">文档导入</h2>
            <DocumentImport
              // 换库即重挂：上一次的入库结果属于另一个知识库，不能留在界面上。
              key={workspaceId}
              workspaceId={workspaceId}
              workspaceName={currentWorkspaceName}
              maxUploadBytes={
                typeof system?.max_upload_bytes === 'number' ? system.max_upload_bytes : null
              }
              onBusyChange={setUploadBusy}
            />
          </div>
        </section>
      </main>

      <footer className="app-footer">
        <span>
          TraceGraph {system?.version ?? ''} · 图后端 {system?.graph_backend ?? '—'} · 默认模型{' '}
          {system?.llm_model || system?.generator || '—'}
        </span>
        <a className="app-footer__link" href="/docs">
          接口文档
        </a>
      </footer>
    </div>
  )
}
