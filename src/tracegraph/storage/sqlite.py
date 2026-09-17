from pathlib import Path
import sqlite3
from threading import RLock

from tracegraph.core.contracts import (
    Chunk,
    Document,
    DocumentVersion,
    IngestionJob,
    IngestionStatus,
)


class SQLiteDocumentRepository:
    """使用 SQLite 持久化文档、版本、片段和入库任务。"""

    def __init__(self, database: str | Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteDocumentRepository":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get_document_by_source(self, source_name: str) -> Document | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, source_name, media_type
                FROM documents
                WHERE source_key = ?
                """,
                (source_name.casefold(),),
            ).fetchone()
        return _document_from_row(row) if row else None

    def get_document(self, document_id: str) -> Document | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, source_name, media_type FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        return _document_from_row(row) if row else None

    def list_documents(self) -> tuple[Document, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, source_name, media_type
                FROM documents
                ORDER BY source_key, id
                """
            ).fetchall()
        return tuple(_document_from_row(row) for row in rows)

    def save_document(self, document: Document) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO documents (id, source_name, source_key, media_type)
                VALUES (?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.source_name,
                    document.source_name.casefold(),
                    document.media_type,
                ),
            )

    def delete_document(self, document_id: str) -> tuple[str, ...]:
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT id FROM chunks WHERE document_id = ? ORDER BY id",
                (document_id,),
            ).fetchall()
            chunk_ids = tuple(row["id"] for row in rows)
            self._connection.execute(
                "DELETE FROM ingestion_jobs WHERE document_id = ?", (document_id,)
            )
            self._connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            self._connection.execute(
                "DELETE FROM document_versions WHERE document_id = ?", (document_id,)
            )
            self._connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return chunk_ids

    def find_version_by_hash(
        self, document_id: str, content_sha256: str
    ) -> DocumentVersion | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, document_id, number, content_sha256
                FROM document_versions
                WHERE document_id = ? AND content_sha256 = ?
                """,
                (document_id, content_sha256),
            ).fetchone()
        return _version_from_row(row) if row else None

    def list_versions(self, document_id: str) -> tuple[DocumentVersion, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, document_id, number, content_sha256
                FROM document_versions
                WHERE document_id = ?
                ORDER BY number
                """,
                (document_id,),
            ).fetchall()
        return tuple(_version_from_row(row) for row in rows)

    def get_version(self, version_id: str) -> DocumentVersion | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, document_id, number, content_sha256
                FROM document_versions
                WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
        return _version_from_row(row) if row else None

    def list_chunks(self, document_version_id: str) -> tuple[Chunk, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, document_id, document_version_id, chunk_index, content, locator
                FROM chunks
                WHERE document_version_id = ?
                ORDER BY chunk_index
                """,
                (document_version_id,),
            ).fetchall()
        return tuple(
            Chunk(
                id=row["id"],
                document_id=row["document_id"],
                document_version_id=row["document_version_id"],
                index=row["chunk_index"],
                content=row["content"],
                locator=row["locator"],
            )
            for row in rows
        )

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, document_id, document_version_id, chunk_index, content, locator
                FROM chunks
                WHERE id = ?
                """,
                (chunk_id,),
            ).fetchone()
        if row is None:
            return None
        return Chunk(
            id=row["id"],
            document_id=row["document_id"],
            document_version_id=row["document_version_id"],
            index=row["chunk_index"],
            content=row["content"],
            locator=row["locator"],
        )

    def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, document_id, status, processed_chunks, error
                FROM ingestion_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        return _job_from_row(row) if row else None

    def save_ingestion_job(self, job: IngestionJob) -> None:
        with self._lock, self._connection:
            self._insert_job(job)

    def save_ingestion(
        self,
        version: DocumentVersion,
        chunks: tuple[Chunk, ...],
        job: IngestionJob,
    ) -> None:
        if any(
            chunk.document_id != version.document_id
            or chunk.document_version_id != version.id
            for chunk in chunks
        ):
            raise ValueError("所有 Chunk 必须属于待保存的文档版本")
        if job.document_id != version.document_id:
            raise ValueError("入库任务必须属于待保存的文档")

        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO document_versions (id, document_id, number, content_sha256)
                VALUES (?, ?, ?, ?)
                """,
                (version.id, version.document_id, version.number, version.content_sha256),
            )
            self._connection.executemany(
                """
                INSERT INTO chunks (
                    id, document_id, document_version_id, chunk_index, content, locator
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        chunk.id,
                        chunk.document_id,
                        chunk.document_version_id,
                        chunk.index,
                        chunk.content,
                        chunk.locator,
                    )
                    for chunk in chunks
                ),
            )
            self._insert_job(job)

    def _insert_job(self, job: IngestionJob) -> None:
        self._connection.execute(
            """
            INSERT INTO ingestion_jobs (
                id, document_id, status, processed_chunks, error
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.document_id,
                job.status.value,
                job.processed_chunks,
                job.error,
            ),
        )

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    source_key TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    number INTEGER NOT NULL CHECK (number > 0),
                    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
                    UNIQUE (document_id, number),
                    UNIQUE (document_id, content_sha256)
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
                    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
                    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
                    locator TEXT NOT NULL,
                    UNIQUE (document_version_id, chunk_index)
                );

                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    status TEXT NOT NULL,
                    processed_chunks INTEGER NOT NULL CHECK (processed_chunks >= 0),
                    error TEXT
                );
                """
            )


def _document_from_row(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        source_name=row["source_name"],
        media_type=row["media_type"],
    )


def _version_from_row(row: sqlite3.Row) -> DocumentVersion:
    return DocumentVersion(
        id=row["id"],
        document_id=row["document_id"],
        number=row["number"],
        content_sha256=row["content_sha256"],
    )


def _job_from_row(row: sqlite3.Row) -> IngestionJob:
    return IngestionJob(
        id=row["id"],
        document_id=row["document_id"],
        status=IngestionStatus(row["status"]),
        processed_chunks=row["processed_chunks"],
        error=row["error"],
    )
