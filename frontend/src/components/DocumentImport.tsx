import { useEffect, useRef, useState } from 'react'

import {
  ApiRequestError,
  SUPPORTED_UPLOAD_SUFFIXES,
  uploadDocument,
} from '../api/client'
import type { IngestionResult } from '../api/types'
import ErrorState from './ErrorState'

const ACCEPT = SUPPORTED_UPLOAD_SUFFIXES.join(',')

// 与后端 ingestion/text.py 的 UNSUPPORTED_DOCUMENT_MESSAGE 对应。
const SUPPORTED_HINT =
  '支持 TXT、Markdown（.md）、JSON、JSONL、CSV，以及可以提取文本的 PDF。'

const SCOPE_NOTICE =
  '普通上传文档会进入 SQLite 文本知识库，可参与关键词检索；' +
  '当前不会自动生成 Neo4j 实体关系，因此不会自动参与图关系和多跳推导。'

const STATUS_TEXT: Record<string, string> = {
  succeeded: '入库成功',
  skipped: '内容未变化，已跳过',
  failed: '入库失败',
  pending: '处理中',
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MiB`
}

interface DocumentImportProps {
  /** 本次上传进入哪个知识库；由页面当前选择决定，不是组件自己的状态。 */
  workspaceId: string
  workspaceName: string
  /** 服务端生效的上传上限；页面已经从 /system 拿到，组件不重复请求。 */
  maxUploadBytes: number | null
  /** 上传忙状态上报给页面，用于在上传期间锁住知识库切换。 */
  onBusyChange: (busy: boolean) => void
  /**
   * 这次上传确实落库之后回调一次（含「内容未变化」的 skipped），
   * 由页面决定要不要重取文档清单。上传失败不会调用。
   */
  onImported?: () => void
}

export default function DocumentImport({
  workspaceId,
  workspaceName,
  maxUploadBytes,
  onBusyChange,
  onImported,
}: DocumentImportProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [requestError, setRequestError] = useState<ApiRequestError | null>(null)
  const [result, setResult] = useState<IngestionResult | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    onBusyChange(busy)
  }, [busy, onBusyChange])

  function pick(selected: File | null) {
    setResult(null)
    setRequestError(null)
    setLocalError(null)
    if (selected && maxUploadBytes !== null && selected.size > maxUploadBytes) {
      // 先在这里拦一次，省掉一次注定失败的上传；后端仍会独立复检。
      setLocalError(
        `${selected.name} 有 ${formatSize(selected.size)}，超过 ${formatSize(maxUploadBytes)} 上限。`,
      )
      setFile(null)
      return
    }
    setFile(selected)
  }

  async function submit() {
    if (!file || busy) return
    setBusy(true)
    setRequestError(null)
    setResult(null)
    try {
      setResult(await uploadDocument(file, workspaceId))
      onImported?.()
    } catch (error) {
      setRequestError(
        error instanceof ApiRequestError
          ? error
          : new ApiRequestError(0, 'unknown_error', String(error)),
      )
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    pick(null)
    if (inputRef.current) inputRef.current.value = ''
  }

  return (
    <div className="importer">
      <p className="importer__target">
        文档将进入：<strong>{workspaceName}</strong>
      </p>

      <label className="field">
        <span className="field__label">选择文档</span>
        <input
          ref={inputRef}
          className="field__input field__input--file"
          type="file"
          accept={ACCEPT}
          disabled={busy}
          onChange={(event) => pick(event.target.files?.[0] ?? null)}
        />
      </label>

      <p className="importer__hint">{SUPPORTED_HINT}</p>

      {file && !localError && (
        <p className="importer__file">
          待上传：<strong>{file.name}</strong> · {formatSize(file.size)}
        </p>
      )}

      {localError && (
        <p className="importer__error" role="alert">
          {localError}
        </p>
      )}

      <div className="importer__actions">
        <button
          type="button"
          className="button button--primary"
          onClick={() => void submit()}
          disabled={busy || !file}
        >
          {busy ? '上传中…' : '上传并入库'}
        </button>
        {(file || result || requestError) && (
          <button type="button" className="button" onClick={reset} disabled={busy}>
            清除
          </button>
        )}
      </div>

      {requestError && <ErrorState error={requestError} />}

      {result && (
        <div className="importer__result" role="status">
          <p className="importer__status">
            <strong>{STATUS_TEXT[result.job.status] ?? result.job.status}</strong>
            {' · '}
            {result.document.source_name}
          </p>
          <dl className="metrics">
            <div>
              <dt>知识库</dt>
              <dd>{workspaceName}</dd>
            </div>
            <div>
              <dt>文档 ID</dt>
              <dd>{result.document.id}</dd>
            </div>
            <div>
              <dt>版本</dt>
              <dd>{result.version.number}</dd>
            </div>
            <div>
              <dt>切片</dt>
              <dd>{result.job.processed_chunks}</dd>
            </div>
          </dl>
          {result.job.status === 'skipped' && (
            <p className="importer__hint">
              内容哈希与已有版本一致，没有新建版本，已有切片继续可用。
            </p>
          )}
          {result.job.error && <p className="importer__error">{result.job.error}</p>}
        </div>
      )}

      <p className="importer__notice">{SCOPE_NOTICE}</p>
    </div>
  )
}
