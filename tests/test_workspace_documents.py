"""Workspace 文档标识与约束的开发期回归检查。"""

import sqlite3

import pytest

from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ADAPTER_ID,
    DEFAULT_WORKSPACE_ID,
    IngestionStatus,
    Workspace,
)
from tracegraph.ingestion.service import TextIngestionService, UnknownWorkspaceError
from tracegraph.storage.memory import InMemoryDocumentRepository
from tracegraph.storage.sqlite import SQLiteDocumentRepository

# Workspace 概念出现之前的表结构：没有 workspaces，source_key 全局唯一。
_LEGACY_SCHEMA = """
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL
);
CREATE TABLE document_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    number INTEGER NOT NULL CHECK (number > 0),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    UNIQUE (document_id, number),
    UNIQUE (document_id, content_sha256)
);
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    locator TEXT NOT NULL,
    UNIQUE (document_version_id, chunk_index)
);
CREATE TABLE ingestion_jobs (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    status TEXT NOT NULL,
    processed_chunks INTEGER NOT NULL CHECK (processed_chunks >= 0),
    error TEXT
);
"""

_COUNTED_TABLES = ("documents", "document_versions", "chunks", "ingestion_jobs")


@pytest.fixture(params=["sqlite", "memory"])
def repository(request, tmp_path):
    """同一套用例分别跑在两种仓储实现上，保证行为一致。"""
    if request.param == "sqlite":
        with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as sqlite_repository:
            yield sqlite_repository
    else:
        yield InMemoryDocumentRepository()


def _workspace(workspace_id: str, name: str) -> Workspace:
    return Workspace(
        id=workspace_id,
        name=name,
        adapter_id=DEFAULT_WORKSPACE_ADAPTER_ID,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _counts(database) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _COUNTED_TABLES
        }
    finally:
        connection.close()


def _build_legacy_database(database) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(_LEGACY_SCHEMA)
        connection.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?)",
            (
                ("doc-1", "指南.md", "指南.md", "text/markdown"),
                ("doc-2", "说明.txt", "说明.txt", "text/plain"),
            ),
        )
        connection.executemany(
            "INSERT INTO document_versions VALUES (?, ?, ?, ?)",
            (
                ("ver-1", "doc-1", 1, "a" * 64),
                ("ver-2", "doc-2", 1, "b" * 64),
            ),
        )
        connection.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
            (
                ("chk-1", "doc-1", "ver-1", 0, "患者应定期监测血压。", ""),
                ("chk-2", "doc-1", "ver-1", 1, "可根据情况进行动态血压监测。", "检查"),
                ("chk-3", "doc-2", "ver-2", 0, "第一版内容。", ""),
            ),
        )
        connection.executemany(
            "INSERT INTO ingestion_jobs VALUES (?, ?, ?, ?, ?)",
            (
                ("job-1", "doc-1", "succeeded", 2, None),
                ("job-2", "doc-2", "skipped", 1, None),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_legacy_database_migrates_without_losing_rows(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)
    before = _counts(database)

    with SQLiteDocumentRepository(database) as repository:
        assert len(repository.list_documents(DEFAULT_WORKSPACE_ID)) == 2
        assert _counts(database) == before
        # 旧文档的入库时间无从追溯，迁移不许拿「现在」冒充它。
        migrated = repository.get_document("doc-1")
        assert migrated.created_at == "" and migrated.updated_at == ""
        assert repository.get_version("ver-1").created_at == ""

    connection = sqlite3.connect(database)
    try:
        assert _counts(database) == before
        # 迁移可重复执行：再打开一次不改变任何计数，也不重复建 Workspace。
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1
    finally:
        connection.close()

    with SQLiteDocumentRepository(database):
        assert _counts(database) == before


def test_migrated_documents_schema_matches_final_shape(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    _build_legacy_database(database)

    with SQLiteDocumentRepository(database):
        pass

    connection = sqlite3.connect(database)
    try:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(documents)")}
        assert columns["workspace_id"][3] == 1  # notnull
        # 时间列必须活着穿过重建：重建按固定列清单搬数据，漏写一列就丢一列。
        assert {"created_at", "updated_at"} <= set(columns)
        assert [
            row[2] for row in connection.execute("PRAGMA foreign_key_list(documents)")
        ] == ["workspaces"]

        unique_indexes = {
            tuple(column[2] for column in connection.execute(f'PRAGMA index_info("{row[1]}")'))
            for row in connection.execute("PRAGMA index_list(documents)")
            if row[2]
        }
        assert ("workspace_id", "source_key") in unique_indexes
        assert ("source_key",) not in unique_indexes

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # 既有文档全部归入 default Workspace，没有归属不明的行。
        assert connection.execute(
            "SELECT COUNT(*) FROM documents WHERE workspace_id IS NULL"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_same_source_name_can_live_in_two_workspaces(repository) -> None:
    repository.save_workspace(_workspace("ws-cardio", "心内科"))
    service = TextIngestionService(repository)

    default = service.ingest_text("指南.md", "# 高血压\n\n患者应定期监测血压。")
    cardio = service.ingest_text(
        "指南.md", "# 高血压\n\n患者应定期监测血压。", workspace_id="ws-cardio"
    )

    assert default.document.id != cardio.document.id
    assert repository.get_document_by_source("指南.md", DEFAULT_WORKSPACE_ID) == default.document
    assert repository.get_document_by_source("指南.md", "ws-cardio") == cardio.document
    assert repository.list_documents("ws-cardio") == (cardio.document,)
    assert repository.list_documents(DEFAULT_WORKSPACE_ID) == (default.document,)


def test_reimport_in_same_workspace_reuses_document(repository) -> None:
    service = TextIngestionService(repository)

    first = service.ingest_text("指南.md", "# 高血压\n\n患者应定期监测血压。")
    second = service.ingest_text("指南.md", "# 高血压\n\n患者应定期监测血压。")

    assert second.document == first.document
    assert second.job.status is IngestionStatus.SKIPPED
    assert repository.list_documents() == (first.document,)


def test_ingestion_stamps_document_and_version_times(repository) -> None:
    service = TextIngestionService(repository)

    first = service.ingest_text("指南.md", "# 高血压\n\n患者应定期监测血压。")
    stored = repository.get_document(first.document.id)

    assert stored.created_at and stored.updated_at
    assert first.version.created_at == stored.created_at

    # 换个正文再传一次：同一个文档多了个版本，更新时间跟着走到新版本。
    second = service.ingest_text("指南.md", "# 高血压\n\n患者应每日监测血压。")
    after = repository.get_document(first.document.id)

    assert after.created_at == stored.created_at
    assert after.updated_at == second.version.created_at
    assert after.updated_at >= stored.updated_at


def test_unknown_workspace_cannot_receive_documents(repository) -> None:
    service = TextIngestionService(repository)

    with pytest.raises(UnknownWorkspaceError, match="ws-missing"):
        service.ingest_text(
            "指南.md", "# 高血压\n\n患者应定期监测血压。", workspace_id="ws-missing"
        )

    # 没有产生孤立 Document，也没有留下任何入库痕迹。
    assert repository.list_documents() == ()
    assert repository.get_document_by_source("指南.md", "ws-missing") is None
    assert [workspace.id for workspace in repository.list_workspaces()] == [
        DEFAULT_WORKSPACE_ID
    ]
