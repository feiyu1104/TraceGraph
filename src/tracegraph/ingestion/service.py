from dataclasses import dataclass
import hashlib
from pathlib import Path
import uuid

from tracegraph.core.contracts import (
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

    def ingest_text(self, source_name: str, content: str) -> IngestionResult:
        normalized_source = source_name.strip()
        if not normalized_source:
            raise ValueError("source_name 不能为空")
        if Path(normalized_source).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise UnsupportedDocumentError(UNSUPPORTED_DOCUMENT_MESSAGE)

        drafts = split_text(content, self.chunk_size)
        document = self.repository.get_document_by_source(normalized_source)
        if document is None:
            document = Document(
                id=_stable_id("doc", normalized_source.casefold()),
                source_name=normalized_source,
                media_type=_media_type(normalized_source),
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
