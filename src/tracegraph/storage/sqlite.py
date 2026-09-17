from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from threading import RLock

from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ADAPTER_ID,
    DEFAULT_WORKSPACE_ID,
    Chunk,
    Document,
    DocumentVersion,
    IngestionJob,
    IngestionStatus,
    Workspace,
)

_DEFAULT_WORKSPACE_NAME = "默认工作区"


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

    def save_workspace(self, workspace: Workspace) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO workspaces (id, name, adapter_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    workspace.id,
                    workspace.name,
                    workspace.adapter_id,
                    workspace.created_at,
                ),
            )

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, name, adapter_id, created_at FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
        return _workspace_from_row(row) if row else None

    def list_workspaces(self) -> tuple[Workspace, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, adapter_id, created_at
                FROM workspaces
                ORDER BY created_at, id
                """
            ).fetchall()
        return tuple(_workspace_from_row(row) for row in rows)

    def get_document_by_source(
        self, source_name: str, workspace_id: str
    ) -> Document | None:
        # 同名来源只在同一个 Workspace 内唯一，因此必须带上归属才能定位。
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, source_name, media_type, workspace_id
                FROM documents
                WHERE workspace_id = ? AND source_key = ?
                """,
                (workspace_id, source_name.casefold()),
            ).fetchone()
        return _document_from_row(row) if row else None

    def get_document(self, document_id: str) -> Document | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, source_name, media_type, workspace_id
                FROM documents
                WHERE id = ?
                """,
                (document_id,),
            ).fetchone()
        return _document_from_row(row) if row else None

    def list_documents(self, workspace_id: str | None = None) -> tuple[Document, ...]:
        query = """
            SELECT id, source_name, media_type, workspace_id
            FROM documents
        """
        parameters: tuple[str, ...] = ()
        if workspace_id is not None:
            query += " WHERE workspace_id = ?"
            parameters = (workspace_id,)
        query += " ORDER BY source_key, id"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return tuple(_document_from_row(row) for row in rows)

    def save_document(self, document: Document) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO documents (id, source_name, source_key, media_type, workspace_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.source_name,
                    document.source_name.casefold(),
                    document.media_type,
                    document.workspace_id,
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
                SELECT id, document_id, number, content_sha256,
                       original_sha256, original_size, stored_path, original_filename
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
                SELECT id, document_id, number, content_sha256,
                       original_sha256, original_size, stored_path, original_filename
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
                SELECT id, document_id, number, content_sha256,
                       original_sha256, original_size, stored_path, original_filename
                FROM document_versions
                WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
        return _version_from_row(row) if row else None

    def attach_original(
        self,
        version_id: str,
        *,
        original_sha256: str,
        original_size: int,
        stored_path: str,
        original_filename: str,
    ) -> DocumentVersion:
        with self._lock, self._connection:
            # 条件写死在 UPDATE 里：先查再写会留下「查到时为空、写的时候已经
            # 被填上」的窗口，正好把别人刚存的原件覆盖掉。
            cursor = self._connection.execute(
                """
                UPDATE document_versions
                SET original_sha256 = ?, original_size = ?,
                    stored_path = ?, original_filename = ?
                WHERE id = ?
                  AND original_sha256 IS NULL
                  AND original_size IS NULL
                  AND stored_path IS NULL
                  AND original_filename IS NULL
                """,
                (
                    original_sha256,
                    original_size,
                    stored_path,
                    original_filename,
                    version_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("只能给还没有原件的版本补存原件")
            row = self._connection.execute(
                """
                SELECT id, document_id, number, content_sha256,
                       original_sha256, original_size, stored_path, original_filename
                FROM document_versions
                WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
        return _version_from_row(row)

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
                INSERT INTO document_versions (
                    id, document_id, number, content_sha256,
                    original_sha256, original_size, stored_path, original_filename
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.id,
                    version.document_id,
                    version.number,
                    version.content_sha256,
                    version.original_sha256,
                    version.original_size,
                    version.stored_path,
                    version.original_filename,
                ),
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
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                    adapter_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    -- 来源只在 Workspace 内唯一：不同 Workspace 允许存在同名文档。
                    UNIQUE (workspace_id, source_key)
                );

                CREATE TABLE IF NOT EXISTS document_versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    number INTEGER NOT NULL CHECK (number > 0),
                    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
                    -- 原件信息整体可空：只有留下原件的版本才填，四列同进同退。
                    original_sha256 TEXT,
                    original_size INTEGER,
                    stored_path TEXT,
                    original_filename TEXT,
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
            self._adopt_documents_into_workspace()
            self._adopt_version_original_columns()
        with self._lock:
            # 重建要开关外键，而外键开关在事务内不生效，因此必须在上面的写事务提交之后。
            if not self._documents_schema_is_current():
                self._rebuild_documents_table()

    def _adopt_documents_into_workspace(self) -> None:
        """给没有 Workspace 概念的既有库补上 workspace_id。

        只做加法：ADD COLUMN 不重写表，既有文档、版本、切片和入库任务全部
        原样保留；随后把每一行回填到 default Workspace，因此迁移完不会留下
        归属不明的文档，也不需要清空任何数据。
        """
        self._connection.execute(
            """
            INSERT OR IGNORE INTO workspaces (id, name, adapter_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                DEFAULT_WORKSPACE_ID,
                _DEFAULT_WORKSPACE_NAME,
                DEFAULT_WORKSPACE_ADAPTER_ID,
                datetime.now(UTC).isoformat(),
            ),
        )
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(documents)")
        }
        if "workspace_id" not in columns:
            # 非空列无法带子查询默认值，因此先加成可空列再回填。
            self._connection.execute(
                "ALTER TABLE documents ADD COLUMN workspace_id TEXT REFERENCES workspaces(id)"
            )
        self._connection.execute(
            "UPDATE documents SET workspace_id = ? WHERE workspace_id IS NULL",
            (DEFAULT_WORKSPACE_ID,),
        )

    def _adopt_version_original_columns(self) -> None:
        """给既有库的 document_versions 补上原件四列。

        四列都可空，因此 ADD COLUMN 不重写表，既有版本原样保留，
        original_* 全为 NULL 就表示这个版本没有留下原件。
        """
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(document_versions)")
        }
        for name, column_type in (
            ("original_sha256", "TEXT"),
            ("original_size", "INTEGER"),
            ("stored_path", "TEXT"),
            ("original_filename", "TEXT"),
        ):
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE document_versions ADD COLUMN {name} {column_type}"
                )

    def _documents_schema_is_current(self) -> bool:
        """documents 是否已是「workspace_id 非空 + 来源仅在 Workspace 内唯一」。

        既有的两步迁移（加列 → 重建）都会收敛到这一形态，所以这个判断同时
        是「迁移做完了」的标志，使迁移可以重复执行。
        """
        columns = {
            row["name"]: row
            for row in self._connection.execute("PRAGMA table_info(documents)")
        }
        workspace_id = columns.get("workspace_id")
        if workspace_id is None or not workspace_id["notnull"]:
            return False
        # 旧的全局唯一约束必须已经消失，否则跨 Workspace 的同名文档仍然写不进去。
        return ("source_key",) not in self._unique_index_columns()

    def _unique_index_columns(self) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(
                column["name"]
                for column in self._connection.execute(
                    f'PRAGMA index_info("{index["name"]}")'
                )
            )
            for index in self._connection.execute("PRAGMA index_list(documents)")
            if index["unique"]
        )

    def _rebuild_documents_table(self) -> None:
        """重建 documents 表，把 workspace_id 收紧为非空并改掉唯一约束。

        SQLite 没有 DROP CONSTRAINT，改列约束只能重建表，因此按官方推荐的
        顺序来：关外键 → 开事务 → 建新表 → 搬数据 → 删旧表 → 改名 → 校验
        → 提交 → 恢复外键。全程一个事务，失败即整表回滚，不会留下半截数据。
        """
        self._connection.commit()  # 事务内关外键是空操作，先确保没有未提交事务
        self._connection.execute("PRAGMA foreign_keys = OFF")
        if self._connection.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError("无法在重建 documents 前关闭外键约束")
        previous_isolation = self._connection.isolation_level
        self._connection.isolation_level = None  # 事务边界改由下面显式控制
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute("DROP TABLE IF EXISTS documents_rebuild")
                self._connection.execute(
                    """
                    CREATE TABLE documents_rebuild (
                        id TEXT PRIMARY KEY,
                        source_name TEXT NOT NULL,
                        source_key TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                        UNIQUE (workspace_id, source_key)
                    )
                    """
                )
                self._connection.execute(
                    """
                    INSERT INTO documents_rebuild (
                        id, source_name, source_key, media_type, workspace_id
                    )
                    SELECT id, source_name, source_key, media_type, workspace_id
                    FROM documents
                    """
                )
                self._connection.execute("DROP TABLE documents")
                self._connection.execute(
                    "ALTER TABLE documents_rebuild RENAME TO documents"
                )
                broken = self._connection.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise RuntimeError(
                        f"documents 重建后出现 {len(broken)} 条失配的外键引用，已回滚"
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        finally:
            self._connection.isolation_level = previous_isolation
            self._connection.execute("PRAGMA foreign_keys = ON")


def _document_from_row(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        source_name=row["source_name"],
        media_type=row["media_type"],
        workspace_id=row["workspace_id"],
    )


def _workspace_from_row(row: sqlite3.Row) -> Workspace:
    return Workspace(
        id=row["id"],
        name=row["name"],
        adapter_id=row["adapter_id"],
        created_at=row["created_at"],
    )


def _version_from_row(row: sqlite3.Row) -> DocumentVersion:
    return DocumentVersion(
        id=row["id"],
        document_id=row["document_id"],
        number=row["number"],
        content_sha256=row["content_sha256"],
        original_sha256=row["original_sha256"],
        original_size=row["original_size"],
        stored_path=row["stored_path"],
        original_filename=row["original_filename"],
    )


def _job_from_row(row: sqlite3.Row) -> IngestionJob:
    return IngestionJob(
        id=row["id"],
        document_id=row["document_id"],
        status=IngestionStatus(row["status"]),
        processed_chunks=row["processed_chunks"],
        error=row["error"],
    )
