import { useCallback, useEffect, useState } from 'react'

import {
  ApiRequestError,
  askQuestion,
  fetchHealth,
  fetchModels,
  fetchSystem,
} from './api/client'
import type { Answer, ModelInfo, SystemInfo } from './api/types'
import AnswerPanel from './components/AnswerPanel'
import DocumentImport from './components/DocumentImport'
import GraphExplorer from './components/GraphExplorer'
import Header from './components/Header'
import ModelSelector from './components/ModelSelector'
import QuestionForm from './components/QuestionForm'

const OFFLINE_MODEL: ModelInfo = {
  id: 'extractive',
  label: '离线摘录（不调用任何模型）',
  model: '',
  available: true,
  kind: 'extractive',
  reason: '',
}

export default function App() {
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [systemError, setSystemError] = useState<string | null>(null)
  const [healthy, setHealthy] = useState<boolean | null>(null)

  const [models, setModels] = useState<ModelInfo[]>([OFFLINE_MODEL])
  const [defaultModelId, setDefaultModelId] = useState(OFFLINE_MODEL.id)
  const [modelNotice, setModelNotice] = useState<string | null>(null)
  const [generatorId, setGeneratorId] = useState(OFFLINE_MODEL.id)

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
    return () => {
      cancelled = true
    }
  }, [])

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
      setAnswer(await askQuestion(trimmed, maxHops, generatorId))
    } catch (error) {
      setQueryError(
        error instanceof ApiRequestError
          ? error
          : new ApiRequestError(0, 'unknown_error', String(error)),
      )
    } finally {
      setBusy(false)
    }
  }, [generatorId, maxHops, question])

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

  return (
    <div className="app">
      <Header system={system} systemError={systemError} healthy={healthy} />

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
            <GraphExplorer />
          </div>

          <div className="panel">
            <h2 className="panel__title">文档导入</h2>
            <DocumentImport />
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
