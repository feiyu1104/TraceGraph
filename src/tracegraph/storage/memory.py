from threading import RLock

from tracegraph.core.contracts import Chunk, Document, DocumentVersion, IngestionJob


class InMemoryDocumentRepository:
    """用于本地开发和测试的线程安全文档存储。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._documents: dict[str, Document] = {}
        self._document_ids_by_source: dict[str, str] = {}
        self._versions: dict[str, DocumentVersion] = {}
        self._version_ids_by_document: dict[str, list[str]] = {}
        self._chunks_by_version: dict[str, tuple[Chunk, ...]] = {}
        self._jobs: dict[str, IngestionJob] = {}

    def get_document_by_source(self, source_name: str) -> Document | None:
        with self._lock:
            document_id = self._document_ids_by_source.get(source_name.casefold())
            return self._documents.get(document_id) if document_id else None

    def get_document(self, document_id: str) -> Document | None:
        with self._lock:
            return self._documents.get(document_id)

    def list_documents(self) -> tuple[Document, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._documents.values(),
                    key=lambda document: (document.source_name.casefold(), document.id),
                )
            )

    def save_document(self, document: Document) -> None:
        with self._lock:
            self._documents[document.id] = document
            self._document_ids_by_source[document.source_name.casefold()] = document.id
            self._version_ids_by_document.setdefault(document.id, [])

    def delete_document(self, document_id: str) -> tuple[str, ...]:
        with self._lock:
            document = self._documents.pop(document_id, None)
            if document is None:
                return ()
            self._document_ids_by_source.pop(document.source_name.casefold(), None)
            version_ids = self._version_ids_by_document.pop(document_id, [])
            chunk_ids = tuple(
                chunk.id
                for version_id in version_ids
                for chunk in self._chunks_by_version.pop(version_id, ())
            )
            for version_id in version_ids:
                self._versions.pop(version_id, None)
            self._jobs = {
                job_id: job
                for job_id, job in self._jobs.items()
                if job.document_id != document_id
            }
            return chunk_ids

    def find_version_by_hash(
        self, document_id: str, content_sha256: str
    ) -> DocumentVersion | None:
        return next(
            (
                version
                for version in self.list_versions(document_id)
                if version.content_sha256 == content_sha256
            ),
            None,
        )

    def list_versions(self, document_id: str) -> tuple[DocumentVersion, ...]:
        with self._lock:
            version_ids = self._version_ids_by_document.get(document_id, [])
            return tuple(self._versions[version_id] for version_id in version_ids)

    def get_version(self, version_id: str) -> DocumentVersion | None:
        with self._lock:
            return self._versions.get(version_id)

    def list_chunks(self, document_version_id: str) -> tuple[Chunk, ...]:
        with self._lock:
            return self._chunks_by_version.get(document_version_id, ())

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        with self._lock:
            return next(
                (
                    chunk
                    for chunks in self._chunks_by_version.values()
                    for chunk in chunks
                    if chunk.id == chunk_id
                ),
                None,
            )

    def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def save_ingestion_job(self, job: IngestionJob) -> None:
        with self._lock:
            if job.document_id not in self._documents:
                raise ValueError("保存任务前必须先保存 Document")
            if job.id in self._jobs:
                raise ValueError("入库任务已存在")
            self._jobs[job.id] = job

    def save_ingestion(
        self,
        version: DocumentVersion,
        chunks: tuple[Chunk, ...],
        job: IngestionJob,
    ) -> None:
        if any(chunk.document_version_id != version.id for chunk in chunks):
            raise ValueError("所有 Chunk 必须属于待保存的文档版本")

        with self._lock:
            if version.document_id not in self._documents:
                raise ValueError("保存版本前必须先保存 Document")
            if version.id in self._versions:
                raise ValueError("文档版本已存在")
            self._versions[version.id] = version
            self._version_ids_by_document[version.document_id].append(version.id)
            self._chunks_by_version[version.id] = chunks
            self._jobs[job.id] = job
