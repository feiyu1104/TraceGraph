import sqlite3

import pytest

from tracegraph.core.contracts import Chunk, DocumentVersion, IngestionJob, IngestionStatus
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.storage.sqlite import SQLiteDocumentRepository


def test_ingestion_survives_repository_restart(tmp_path) -> None:
    database = tmp_path / "tracegraph.db"

    with SQLiteDocumentRepository(database) as repository:
        first = TextIngestionService(repository).ingest_text(
            "指南.md", "# 高血压\n\n患者应定期监测血压。"
        )

    with SQLiteDocumentRepository(database) as repository:
        second = TextIngestionService(repository).ingest_text(
            "指南.md", "# 高血压\n\n患者应定期监测血压。"
        )

        assert second.job.status is IngestionStatus.SKIPPED
        assert second.document == first.document
        assert second.version == first.version
        assert second.chunks == first.chunks
        assert repository.get_ingestion_job(second.job.id) == second.job


def test_changed_content_creates_persistent_second_version(tmp_path) -> None:
    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        service = TextIngestionService(repository)
        first = service.ingest_text("说明.txt", "第一版内容。")
        second = service.ingest_text("说明.txt", "第二版内容。")

        assert repository.list_versions(first.document.id) == (
            first.version,
            second.version,
        )


def test_failed_chunk_insert_rolls_back_whole_ingestion(tmp_path) -> None:
    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        successful = TextIngestionService(repository).ingest_text("说明.txt", "已有内容。")
        failed_version = DocumentVersion(
            id="ver-failed",
            document_id=successful.document.id,
            number=2,
            content_sha256="f" * 64,
        )
        duplicate_chunk_ids = (
            Chunk(
                id="duplicate",
                document_id=successful.document.id,
                document_version_id=failed_version.id,
                index=0,
                content="片段一",
                locator="",
            ),
            Chunk(
                id="duplicate",
                document_id=successful.document.id,
                document_version_id=failed_version.id,
                index=1,
                content="片段二",
                locator="",
            ),
        )
        job = IngestionJob(
            id="job-failed",
            document_id=successful.document.id,
            status=IngestionStatus.SUCCEEDED,
            processed_chunks=2,
        )

        with pytest.raises(sqlite3.IntegrityError):
            repository.save_ingestion(failed_version, duplicate_chunk_ids, job)

        assert repository.list_versions(successful.document.id) == (successful.version,)
        assert repository.list_chunks(failed_version.id) == ()
