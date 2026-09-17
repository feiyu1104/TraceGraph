from dataclasses import dataclass
import hashlib
from pathlib import Path
import uuid

from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    Chunk,
    Document,
    DocumentVersion,
    IngestionJob,
    IngestionStatus,
)
from tracegraph.core.ports import DocumentRepository
from tracegraph.ingestion.text import (
    SUPPORTED_SUFFIXES,
    UNSUPPORTED_DOCUMENT_MESSAGE,
    UnsupportedDocumentError,
    read_document_bytes,
    read_text_document,
    split_text,
)


class UnknownWorkspaceError(ValueError):
    """要写入的 Workspace 不存在。

    继承 ValueError 是为了让既有的入参校验分支继续按「请求有问题」处理，
    同时给调用方一个可以精确识别的类型。
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"Workspace 不存在：{workspace_id}")
        self.workspace_id = workspace_id


@dataclass(frozen=True, slots=True)
class IngestionResult:
    document: Document
    version: DocumentVersion
    chunks: tuple[Chunk, ...]
    job: IngestionJob


class TextIngestionService:
    def __init__(self, repository: DocumentRepository, chunk_size: int = 800):
        if chunk_size < 20:
            raise ValueError("chunk_size 不能小于 20")
        self.repository = repository
        self.chunk_size = chunk_size

    def ingest_file(self, path: Path) -> IngestionResult:
        content = read_text_document(path)
        return self.ingest_text(path.name, content)

    def ingest_bytes(self, source_name: str, raw: bytes) -> IngestionResult:
        content = read_document_bytes(source_name, raw)
        return self.ingest_text(source_name, content)

    def ingest_text(
        self,
        source_name: str,
        content: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> IngestionResult:
        normalized_source = source_name.strip()
        if not normalized_source:
            raise ValueError("source_name 不能为空")
        if Path(normalized_source).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise UnsupportedDocumentError(UNSUPPORTED_DOCUMENT_MESSAGE)
        # 先确认归属再落库，否则会留下没有 Workspace 的 Document。
        if self.repository.get_workspace(workspace_id) is None:
            raise UnknownWorkspaceError(workspace_id)

        drafts = split_text(content, self.chunk_size)
        document = self.repository.get_document_by_source(normalized_source, workspace_id)
        if document is None:
            document = Document(
                # Workspace 参与稳定 ID：不同 Workspace 的同名文件是不同的文档。
                id=_stable_id("doc", workspace_id, normalized_source.casefold()),
                source_name=normalized_source,
                media_type=_media_type(normalized_source),
                workspace_id=workspace_id,
            )
            self.repository.save_document(document)

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = self.repository.find_version_by_hash(document.id, content_hash)
        if existing is not None:
            chunks = self.repository.list_chunks(existing.id)
            job = IngestionJob(
                id=f"job-{uuid.uuid4().hex}",
                document_id=document.id,
                status=IngestionStatus.SKIPPED,
                processed_chunks=len(chunks),
            )
            self.repository.save_ingestion_job(job)
            return IngestionResult(
                document=document,
                version=existing,
                chunks=chunks,
                job=job,
            )

        version_number = len(self.repository.list_versions(document.id)) + 1
        version = DocumentVersion(
            id=_stable_id("ver", document.id, content_hash),
            document_id=document.id,
            number=version_number,
            content_sha256=content_hash,
        )
        chunks = tuple(
            Chunk(
                id=_stable_id("chk", version.id, str(index), draft.content),
                document_id=document.id,
                document_version_id=version.id,
                index=index,
                content=draft.content,
                locator=draft.locator,
            )
            for index, draft in enumerate(drafts)
        )
        job = IngestionJob(
            id=f"job-{uuid.uuid4().hex}",
            document_id=document.id,
            status=IngestionStatus.SUCCEEDED,
            processed_chunks=len(chunks),
        )
        self.repository.save_ingestion(version, chunks, job)
        return IngestionResult(document=document, version=version, chunks=chunks, job=job)


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:20]}"


def _media_type(source_name: str) -> str:
    suffix = Path(source_name).suffix.lower()
    return {
        ".csv": "text/csv",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".md": "text/markdown",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }[suffix]
