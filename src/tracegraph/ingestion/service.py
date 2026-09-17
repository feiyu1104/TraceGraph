from dataclasses import dataclass, replace
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
from tracegraph.core.ports import DocumentRepository, OriginalDocumentStore
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
    def __init__(
        self,
        repository: DocumentRepository,
        chunk_size: int = 800,
        original_store: OriginalDocumentStore | None = None,
    ):
        if chunk_size < 20:
            raise ValueError("chunk_size 不能小于 20")
        self.repository = repository
        self.chunk_size = chunk_size
        # 不装配时只入库解析结果，不保留原件：既有调用方因此不受影响。
        self.original_store = original_store

    def ingest_file(self, path: Path) -> IngestionResult:
        content = read_text_document(path)
        return self.ingest_text(path.name, content)

    def ingest_bytes(
        self,
        source_name: str,
        raw: bytes,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> IngestionResult:
        content = read_document_bytes(source_name, raw)
        original = raw if self.original_store is not None else None
        return self.ingest_text(source_name, content, workspace_id, original=original)

    def ingest_text(
        self,
        source_name: str,
        content: str,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
        original: bytes | None = None,
    ) -> IngestionResult:
        normalized_source = source_name.strip()
        if not normalized_source:
            raise ValueError("source_name 不能为空")
        suffix = Path(normalized_source).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise UnsupportedDocumentError(UNSUPPORTED_DOCUMENT_MESSAGE)
        # 先确认归属再落库，否则会留下没有 Workspace 的 Document。
        if self.repository.get_workspace(workspace_id) is None:
            raise UnknownWorkspaceError(workspace_id)
        if original is not None and self.original_store is None:
            raise ValueError("未装配原件存储，无法保存原件")

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
        # 版本身份是解析后的正文而不是原件字节：同一份原件反复上传必然解析出
        # 同一段正文，因此这一步已经覆盖了「重复上传同一原件」，而换了编码或
        # 换行重新上传的同一份内容也会落在这里，不会再多出一个版本。
        existing = self.repository.find_version_by_hash(document.id, content_hash)
        if existing is not None:
            # 命中去重时原件一个字节都不落盘：已有版本指向的原件才是它该有的那份。
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
        # 原件必须先落盘、数据库后写：反过来的话，写文件失败就会在库里留下一条
        # 指向不存在原件的版本。而文件写成功、数据库失败时，下面把它删掉即可。
        if original is not None:
            stored_path = self.original_store.save(
                workspace_id=workspace_id,
                document_id=document.id,
                version_id=version.id,
                suffix=suffix,
                raw=original,
            )
            version = replace(
                version,
                original_sha256=hashlib.sha256(original).hexdigest(),
                original_size=len(original),
                stored_path=stored_path,
                original_filename=normalized_source,
            )

        job = IngestionJob(
            id=f"job-{uuid.uuid4().hex}",
            document_id=document.id,
            status=IngestionStatus.SUCCEEDED,
            processed_chunks=len(chunks),
        )
        try:
            self.repository.save_ingestion(version, chunks, job)
        except Exception:
            if original is not None:
                # 数据库里没有这条版本，磁盘上那份原件就成了查不到主人的半成品。
                self.original_store.remove_version(
                    workspace_id=workspace_id,
                    document_id=document.id,
                    version_id=version.id,
                )
            raise
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
