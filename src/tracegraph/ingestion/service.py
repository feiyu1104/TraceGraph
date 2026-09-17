from dataclasses import dataclass, replace
from datetime import UTC, datetime
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


class OriginalConflictError(ValueError):
    """同一段解析正文已经绑定了另一份原件。

    正文哈希相同说明两次上传解析出的是同一段文本，原始字节却不同（换编码、
    换行等）。已有版本记录的原件是权威的那一份，既不静默覆盖也不丢弃，
    而是如实报出冲突。
    """

    def __init__(self, stored_sha256: str, incoming_sha256: str) -> None:
        super().__init__(
            "该版本已保存另一份原件，与本次上传的原始字节不一致"
            f"（已有 {stored_sha256[:12]}…，本次 {incoming_sha256[:12]}…）；"
            "如需保留两份原件，请改用不同的文件名上传。"
        )
        self.stored_sha256 = stored_sha256
        self.incoming_sha256 = incoming_sha256


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

    def ingest_file(
        self, path: Path, workspace_id: str = DEFAULT_WORKSPACE_ID
    ) -> IngestionResult:
        """按路径入库一份文档：读取原始字节后与上传走同一条链路。

        原件的大小和哈希必须按真实字节算，所以这里不做解码，直接交给
        ingest_bytes；装配了原件存储时原件也一并保存。
        """
        return self.ingest_bytes(path.name, path.read_bytes(), workspace_id)

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
        # 入库时间由服务层打：仓储层只负责存，不负责判断「现在几点」。
        ingested_at = datetime.now(UTC).isoformat()
        document = self.repository.get_document_by_source(normalized_source, workspace_id)
        if document is None:
            document = Document(
                # Workspace 参与稳定 ID：不同 Workspace 的同名文件是不同的文档。
                id=_stable_id("doc", workspace_id, normalized_source.casefold()),
                source_name=normalized_source,
                media_type=_media_type(normalized_source),
                workspace_id=workspace_id,
                created_at=ingested_at,
                updated_at=ingested_at,
            )
            self.repository.save_document(document)

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        # 版本身份是解析后的正文而不是原件字节：同一份原件反复上传必然解析出
        # 同一段正文，因此这一步已经覆盖了「重复上传同一原件」，而换了编码或
        # 换行重新上传的同一份内容也会落在这里，不会再多出一个版本。
        existing = self.repository.find_version_by_hash(document.id, content_hash)
        if existing is not None:
            return self._settle_existing_version(
                document,
                existing,
                workspace_id=workspace_id,
                suffix=suffix,
                original_filename=normalized_source,
                original=original,
            )

        version_number = len(self.repository.list_versions(document.id)) + 1
        version = DocumentVersion(
            id=_stable_id("ver", document.id, content_hash),
            document_id=document.id,
            number=version_number,
            content_sha256=content_hash,
            created_at=ingested_at,
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

    def _settle_existing_version(
        self,
        document: Document,
        existing: DocumentVersion,
        *,
        workspace_id: str,
        suffix: str,
        original_filename: str,
        original: bytes | None,
    ) -> IngestionResult:
        """命中已有版本：不新建版本与 Chunk，只把原件这块补齐或验明。"""
        version = existing
        if original is not None:
            original_hash = hashlib.sha256(original).hexdigest()
            if version.stored_path is None:
                # 只入库过解析结果的历史版本在这里补存原件：用户后来才第一次
                # 上传原件时正文哈希仍是同一个，命中的正好是这条版本。
                version = self._attach_original(
                    version,
                    workspace_id=workspace_id,
                    suffix=suffix,
                    original_filename=original_filename,
                    original=original,
                    original_hash=original_hash,
                )
            elif original_hash != version.original_sha256:
                raise OriginalConflictError(version.original_sha256, original_hash)

        chunks = self.repository.list_chunks(version.id)
        job = IngestionJob(
            id=f"job-{uuid.uuid4().hex}",
            document_id=document.id,
            status=IngestionStatus.SKIPPED,
            processed_chunks=len(chunks),
        )
        self.repository.save_ingestion_job(job)
        return IngestionResult(document=document, version=version, chunks=chunks, job=job)

    def _attach_original(
        self,
        version: DocumentVersion,
        *,
        workspace_id: str,
        suffix: str,
        original_filename: str,
        original: bytes,
        original_hash: str,
    ) -> DocumentVersion:
        store = self.original_store
        if store is None:
            # 调用方只会在装配了存储时把 original 带下来，这里再确认一次。
            raise ValueError("未装配原件存储，无法保存原件")
        # 与首次入库同样的顺序：原件先落盘、数据库后写。反过来的话，写文件
        # 失败就会在库里留下一条指向不存在原件的版本。
        stored_path = store.save(
            workspace_id=workspace_id,
            document_id=version.document_id,
            version_id=version.id,
            suffix=suffix,
            raw=original,
        )
        try:
            return self.repository.attach_original(
                version.id,
                original_sha256=original_hash,
                original_size=len(original),
                stored_path=stored_path,
                original_filename=original_filename,
            )
        except Exception:
            # 存储层没认下这份原件（写失败，或这条版本已经不是「没有原件」
            # 的状态），磁盘上就不能留一个查不到主人的文件。
            store.remove_version(
                workspace_id=workspace_id,
                document_id=version.document_id,
                version_id=version.id,
            )
            raise


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
