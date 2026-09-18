import { useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiRequestError,
  batchReviewCandidates,
  fetchExtractionRun,
  fetchExtractionRuns,
  fetchRunCandidates,
  fetchWorkspaceCandidates,
  fetchWorkspaceDocuments,
  publishGraph,
  reviewCandidateEntity,
  reviewCandidateRelation,
  startExtraction,
  type PublicationTarget,
} from '../api/client'
import type {
  CandidateEntity,
  CandidateRelation,
  CandidateStatus,
  ExtractionRun,
  ModelInfo,
  PublicationResult,
  WorkspaceDocument,
} from '../api/types'
import CandidateBoard, { ALL_RUNS } from './CandidateBoard'
import type { CandidateEdit } from './CandidateRow'
import DocumentImport from './DocumentImport'
import DocumentList from './DocumentList'
import EmptyState from './EmptyState'
import ExtractionPanel, { isTerminal } from './ExtractionPanel'

// 后端目前是同步执行：POST /extractions 直接返回终态。这里的轮询只是兜底，
// 万一将来改成长任务，界面也能看到 pending / running 直到终态。
const POLL_INTERVAL_MS = 800
const POLL_ATTEMPTS = 12

const OFFLINE_ZERO_NOTICE =
  '这次抽取没有产出任何候选：当前的离线抽取器没有适用于该适配器的确定性规则，' +
  '可以配置在线模型后重新抽取。'

const MODEL_ZERO_NOTICE =
  '模型这次没有从这份文档里抽出候选。可以确认文档里是否包含可抽取的事实，' +
  '或者换一个模型再试一次。'

interface DocumentsWorkbenchProps {
  workspaceId: string
  workspaceName: string
  adapterId: string
  adapterLabel: string
  /** 当前适配器声明的类型；候选修正时的下拉选项来自这里。 */
  entityTypes: string[]
  relationTypes: string[]
  models: ModelInfo[]
  maxUploadBytes: number | null
  graphAvailable: boolean
  /** 发布成功后通知页面重新挂载图谱浏览。 */
  onGraphChanged: () => void
  /** 上传、抽取、审核、发布任一进行中；页面据此锁住知识库切换。 */
  onBusyChange: (busy: boolean) => void
}

export default function DocumentsWorkbench(props: DocumentsWorkbenchProps) {
  const { workspaceId, models, onBusyChange, onGraphChanged } = props

  const [uploadBusy, setUploadBusy] = useState(false)

  const [documents, setDocuments] = useState<WorkspaceDocument[] | null>(null)
  const [documentsLoading, setDocumentsLoading] = useState(true)
  const [documentsError, setDocumentsError] = useState<ApiRequestError | null>(null)

  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null)

  const [runs, setRuns] = useState<ExtractionRun[] | null>(null)
  const [runsLoading, setRunsLoading] = useState(false)
  const [runsError, setRunsError] = useState<ApiRequestError | null>(null)

  const [runFilter, setRunFilter] = useState<string>(ALL_RUNS)
  const [candidates, setCandidates] = useState<{
    entities: CandidateEntity[]
    relations: CandidateRelation[]
  } | null>(null)
  const [candidatesLoading, setCandidatesLoading] = useState(false)
  const [candidatesError, setCandidatesError] = useState<ApiRequestError | null>(null)

  const [modelId, setModelId] = useState('')
  const [extractionBusy, setExtractionBusy] = useState(false)
  const [activeRun, setActiveRun] = useState<ExtractionRun | null>(null)
  const [startError, setStartError] = useState<ApiRequestError | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(new Set())
  const [rowErrors, setRowErrors] = useState<Record<string, ApiRequestError>>({})
  const [batchBusy, setBatchBusy] = useState(false)
  const [batchError, setBatchError] = useState<ApiRequestError | null>(null)

  const [publishBusy, setPublishBusy] = useState(false)
  const [publishError, setPublishError] = useState<ApiRequestError | null>(null)
  const [publishResult, setPublishResult] = useState<PublicationResult | null>(null)

  // 换文档 / 换抽取任务之后，旧请求的返回必须丢弃：否则会有一瞬间
  // 把上一个文档的候选显示在新文档下面。
  const runsToken = useRef(0)
  const candidatesToken = useRef(0)
  // 异步流程里要判断「现在选中的还是不是当初那份文档」。
  const documentIdRef = useRef<string | null>(null)

  useEffect(() => {
    documentIdRef.current = selectedDocumentId
  }, [selectedDocumentId])

  const selectedDocument =
    documents?.find((document) => document.id === selectedDocumentId) ?? null

  const loadDocuments = useCallback(async () => {
    setDocumentsLoading(true)
    try {
      const payload = await fetchWorkspaceDocuments(workspaceId)
      setDocuments(payload.documents)
      setDocumentsError(null)
    } catch (error) {
      // 失败时保留已经加载到的清单：一次刷新失败不该把页面清空。
      setDocumentsError(toApiError(error))
    } finally {
      setDocumentsLoading(false)
    }
  }, [workspaceId])

  const loadRuns = useCallback(
    async (documentId: string) => {
      const token = ++runsToken.current
      setRunsLoading(true)
      try {
        const payload = await fetchExtractionRuns(workspaceId, documentId)
        if (token !== runsToken.current) return
        setRuns(payload.runs)
        setRunsError(null)
      } catch (error) {
        if (token !== runsToken.current) return
        setRunsError(toApiError(error))
      } finally {
        if (token === runsToken.current) setRunsLoading(false)
      }
    },
    [workspaceId],
  )

  const loadCandidates = useCallback(
    async (documentId: string, runId: string) => {
      const token = ++candidatesToken.current
      setCandidatesLoading(true)
      try {
        const payload =
          runId === ALL_RUNS
            ? await fetchWorkspaceCandidates(workspaceId, documentId)
            : await fetchRunCandidates(runId)
        if (token !== candidatesToken.current) return
        setCandidates({ entities: payload.entities, relations: payload.relations })
        setCandidatesError(null)
      } catch (error) {
        if (token !== candidatesToken.current) return
        setCandidatesError(toApiError(error))
      } finally {
        if (token === candidatesToken.current) setCandidatesLoading(false)
      }
    },
    [workspaceId],
  )

  const refreshRuns = useCallback(() => {
    if (!selectedDocumentId) return Promise.resolve()
    return loadRuns(selectedDocumentId)
  }, [loadRuns, selectedDocumentId])

  const refreshCandidates = useCallback(() => {
    if (!selectedDocumentId) return Promise.resolve()
    return loadCandidates(selectedDocumentId, runFilter)
  }, [loadCandidates, runFilter, selectedDocumentId])

  useEffect(() => {
    void loadDocuments()
  }, [loadDocuments])

  // 清单到位后选中第一份文档；刷新不该把用户已经选中的文档换掉。
  useEffect(() => {
    if (documents === null) return
    setSelectedDocumentId((current) =>
      current && documents.some((document) => document.id === current)
        ? current
        : (documents[0]?.id ?? null),
    )
  }, [documents])

  useEffect(() => {
    if (!selectedDocumentId) return
    void loadRuns(selectedDocumentId)
  }, [loadRuns, selectedDocumentId])

  useEffect(() => {
    if (!selectedDocumentId) return
    void loadCandidates(selectedDocumentId, runFilter)
  }, [loadCandidates, runFilter, selectedDocumentId])

  // 模型清单是异步来的；选中的模型不在清单里时（首次进入、清单刷新）
  // 退回第一个可用模型。
  useEffect(() => {
    if (modelId && models.some((model) => model.id === modelId && model.available)) return
    setModelId(models.find((model) => model.available)?.id ?? '')
  }, [modelId, models])

  const busy =
    uploadBusy || extractionBusy || batchBusy || publishBusy || busyIds.size > 0

  useEffect(() => {
    onBusyChange(busy)
    // 卸载时（换标签页、换知识库）必须把忙状态收回，否则页面的切换会被永久锁住。
    return () => {
      if (busy) onBusyChange(false)
    }
  }, [busy, onBusyChange])

  function selectDocument(documentId: string) {
    if (documentId === selectedDocumentId) return
    setSelectedDocumentId(documentId)
    // 上一个文档的候选、任务、错误与勾选全部立刻清掉，不等新请求回来。
    setRunFilter(ALL_RUNS)
    setRuns(null)
    setCandidates(null)
    setRunsError(null)
    setCandidatesError(null)
    setActiveRun(null)
    setStartError(null)
    setNotice(null)
    setRowErrors({})
    setBatchError(null)
    setPublishError(null)
    setPublishResult(null)
  }

  function selectRun(runId: string) {
    setRowErrors({})
    setBatchError(null)
    setPublishError(null)
    setPublishResult(null)
    setRunFilter(runId)
  }

  const startExtractionRun = useCallback(async () => {
    const document = selectedDocument
    if (!document) return
    setExtractionBusy(true)
    setStartError(null)
    setNotice(null)
    setActiveRun(null)
    try {
      let run = await startExtraction(workspaceId, document.id, modelId)
      // 启动返回时用户可能已经换到别的文档：旧文档的任务不能显示过去。
      if (documentIdRef.current !== document.id) return
      setActiveRun(run)
      for (
        let attempt = 0;
        attempt < POLL_ATTEMPTS && !isTerminal(run.status);
        attempt += 1
      ) {
        await delay(POLL_INTERVAL_MS)
        run = await fetchExtractionRun(run.id)
        // 轮询期间换了文档：这次任务的结果不再属于当前界面。
        if (documentIdRef.current !== document.id) return
        setActiveRun(run)
      }
      if (run.status === 'succeeded' && run.entity_count + run.relation_count === 0) {
        setNotice(
          isOfflineModel(models, run.model_id) ? OFFLINE_ZERO_NOTICE : MODEL_ZERO_NOTICE,
        )
      }
      await Promise.all([refreshRuns(), refreshCandidates(), loadDocuments()])
    } catch (error) {
      setStartError(toApiError(error))
      // 失败的抽取同样落了库，任务历史里应该看得到这条记录。
      await refreshRuns()
    } finally {
      setExtractionBusy(false)
    }
  }, [loadDocuments, modelId, models, refreshCandidates, refreshRuns, selectedDocument, workspaceId])

  const markBusy = useCallback((candidateId: string, value: boolean) => {
    setBusyIds((current) => {
      const next = new Set(current)
      if (value) next.add(candidateId)
      else next.delete(candidateId)
      return next
    })
  }, [])

  const reviewStatus = useCallback(
    async (kind: 'entity' | 'relation', candidateId: string, status: CandidateStatus) => {
      markBusy(candidateId, true)
      setRowErrors((current) => omit(current, candidateId))
      try {
        if (kind === 'entity') {
          await reviewCandidateEntity(candidateId, workspaceId, { status })
        } else {
          await reviewCandidateRelation(candidateId, workspaceId, { status })
        }
        await Promise.all([refreshCandidates(), loadDocuments()])
      } catch (error) {
        // 冲突信息直接贴回这条候选，而不是弹一个和候选对不上的全局提示。
        setRowErrors((current) => ({ ...current, [candidateId]: toApiError(error) }))
      } finally {
        markBusy(candidateId, false)
      }
    },
    [loadDocuments, markBusy, refreshCandidates, workspaceId],
  )

  const saveCandidate = useCallback(
    async (kind: 'entity' | 'relation', candidateId: string, edit: CandidateEdit) => {
      markBusy(candidateId, true)
      setRowErrors((current) => omit(current, candidateId))
      try {
        if (kind === 'entity') {
          await reviewCandidateEntity(candidateId, workspaceId, edit)
        } else {
          await reviewCandidateRelation(candidateId, workspaceId, edit)
        }
        await refreshCandidates()
      } catch (error) {
        setRowErrors((current) => ({ ...current, [candidateId]: toApiError(error) }))
      } finally {
        markBusy(candidateId, false)
      }
    },
    [markBusy, refreshCandidates, workspaceId],
  )

  const batchReview = useCallback(
    async (status: CandidateStatus, entityIds: string[], relationIds: string[]) => {
      setBatchBusy(true)
      setBatchError(null)
      try {
        await batchReviewCandidates(workspaceId, status, entityIds, relationIds)
        await Promise.all([refreshCandidates(), loadDocuments()])
      } catch (error) {
        // 整批要么全改要么一条都不改：失败时不假设部分成功，按服务端状态重取。
        setBatchError(toApiError(error))
        await refreshCandidates()
      } finally {
        setBatchBusy(false)
      }
    },
    [loadDocuments, refreshCandidates, workspaceId],
  )

  const publish = useCallback(
    async (target: PublicationTarget) => {
      setPublishBusy(true)
      setPublishError(null)
      try {
        const result = await publishGraph(workspaceId, target)
        setPublishResult(result)
        // 候选的发布标记与文档统计都会变，图谱也要重新加载。
        await Promise.all([refreshCandidates(), loadDocuments()])
        onGraphChanged()
      } catch (error) {
        setPublishError(toApiError(error))
      } finally {
        setPublishBusy(false)
      }
    },
    [loadDocuments, onGraphChanged, refreshCandidates, workspaceId],
  )

  const adapterMissing = !props.adapterId

  return (
    <div className="workbench">
      <section className="panel" aria-label="文档导入">
        <h2 className="panel__title">文档导入</h2>
        <DocumentImport
          workspaceId={workspaceId}
          workspaceName={props.workspaceName}
          maxUploadBytes={props.maxUploadBytes}
          // 上传的忙状态先汇总到这里，再统一上报给页面，避免两个写入方互相覆盖。
          onBusyChange={setUploadBusy}
          // 入库成功就刷新清单：新文档的切片数、候选统计都在这个列表里。
          onImported={() => void loadDocuments()}
        />
      </section>

      <div className="workbench__columns">
        <section className="panel" aria-label="文档清单">
          <DocumentList
            documents={documents}
            selectedId={selectedDocumentId}
            onSelect={selectDocument}
            loading={documentsLoading}
            refreshing={documentsLoading && documents !== null}
            error={documentsError}
            onRefresh={() => void loadDocuments()}
          />
        </section>

        <section className="panel" aria-label="知识抽取">
          {selectedDocument ? (
            <ExtractionPanel
              document={selectedDocument}
              adapterLabel={props.adapterLabel}
              adapterId={props.adapterId}
              models={models}
              modelId={modelId}
              onModelChange={setModelId}
              onStart={() => void startExtractionRun()}
              busy={extractionBusy}
              activeRun={activeRun}
              startError={startError}
              notice={notice}
              runs={runs}
              runsLoading={runsLoading}
              runsError={runsError}
              onOpenRun={selectRun}
              onReloadRuns={() => void refreshRuns()}
            />
          ) : (
            <EmptyState
              title="还没有选中文档"
              hint={
                documents === null
                  ? '正在读取当前知识库的文档清单。'
                  : '先在左边的清单里选一份文档，再在这里选择模型启动知识抽取。'
              }
            />
          )}
        </section>
      </div>

      <section className="panel" aria-label="候选审核与发布">
        {selectedDocument ? (
          <CandidateBoard
            entities={candidates?.entities ?? []}
            relations={candidates?.relations ?? []}
            loading={candidatesLoading}
            error={candidatesError}
            onReload={() => void refreshCandidates()}
            runs={runs ?? []}
            runFilter={runFilter}
            onRunFilterChange={selectRun}
            entityTypes={props.entityTypes}
            relationTypes={props.relationTypes}
            busyIds={busyIds}
            rowErrors={rowErrors}
            batchBusy={batchBusy}
            batchError={batchError}
            onReviewStatus={(kind, candidateId, status) =>
              void reviewStatus(kind, candidateId, status)
            }
            onSave={(kind, candidateId, edit) => void saveCandidate(kind, candidateId, edit)}
            onBatchReview={(status, entityIds, relationIds) =>
              void batchReview(status, entityIds, relationIds)
            }
            publish={{
              workspaceName: props.workspaceName,
              graphAvailable: props.graphAvailable,
              busy: publishBusy,
              error: publishError,
              result: publishResult,
              onPublish: (target) => void publish(target),
            }}
          />
        ) : (
          <EmptyState
            title="选中一份文档后可以审核它的候选"
            hint="候选实体与候选关系来自这个文档的抽取任务；审核通过后才会被发布到图谱。"
          />
        )}
        {adapterMissing && (
          <p className="panel__note">
            当前知识库的适配器不在服务端清单里，暂时无法确认可用的实体与关系类型；
            抽取与审核仍以服务端返回的候选为准。
          </p>
        )}
      </section>
    </div>
  )
}

function toApiError(error: unknown): ApiRequestError {
  return error instanceof ApiRequestError
    ? error
    : new ApiRequestError(0, 'unknown_error', String(error))
}

function omit(
  errors: Record<string, ApiRequestError>,
  key: string,
): Record<string, ApiRequestError> {
  if (!(key in errors)) return errors
  const next = { ...errors }
  delete next[key]
  return next
}

function isOfflineModel(models: ModelInfo[], modelId: string): boolean {
  const found = models.find((model) => model.id === modelId)
  return found ? found.kind === 'extractive' : modelId === 'extractive'
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}
