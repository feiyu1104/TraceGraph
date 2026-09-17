from pathlib import Path

import pytest

from tracegraph.core.contracts import IngestionStatus
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.ingestion.text import UnsupportedDocumentError
from tracegraph.storage.memory import InMemoryDocumentRepository


def build_service(chunk_size: int = 80) -> tuple[TextIngestionService, InMemoryDocumentRepository]:
    repository = InMemoryDocumentRepository()
    return TextIngestionService(repository, chunk_size=chunk_size), repository


def test_markdown_ingestion_preserves_heading_locator() -> None:
    service, repository = build_service(chunk_size=30)

    result = service.ingest_text(
        "高血压指南.md",
        "# 高血压\n\n患者应定期监测血压。\n\n## 检查\n\n可根据情况进行动态血压监测。",
    )

    assert result.job.status is IngestionStatus.SUCCEEDED
    assert result.document.source_name == "高血压指南.md"
    assert result.version.number == 1
    assert len(result.chunks) == 2
    assert result.chunks[0].locator == "高血压"
    assert result.chunks[1].locator == "高血压 > 检查"
    assert repository.list_chunks(result.version.id) == result.chunks


def test_same_content_is_idempotent() -> None:
    service, repository = build_service()
    content = "# 用药说明\n\n药物应在医生指导下使用。"

    first = service.ingest_text("说明.md", content)
    second = service.ingest_text("说明.md", content)

    assert first.job.status is IngestionStatus.SUCCEEDED
    assert second.job.status is IngestionStatus.SKIPPED
    assert second.version.id == first.version.id
    assert len(repository.list_versions(first.document.id)) == 1


def test_changed_content_creates_new_version() -> None:
    service, repository = build_service()

    first = service.ingest_text("说明.txt", "第一版内容。")
    second = service.ingest_text("说明.txt", "第二版内容。")

    assert second.job.status is IngestionStatus.SUCCEEDED
    assert second.version.number == 2
    assert second.version.id != first.version.id
    assert len(repository.list_versions(first.document.id)) == 2


def test_unsupported_file_type_is_rejected() -> None:
    service, _ = build_service()

    with pytest.raises(UnsupportedDocumentError, match="仅支持"):
        service.ingest_text("report.docx", "not-a-real-docx")


def test_heading_only_document_is_rejected() -> None:
    service, repository = build_service()

    with pytest.raises(ValueError, match="没有可入库正文"):
        service.ingest_text("empty.md", "# 只有标题")

    assert repository.get_document_by_source("empty.md") is None


def test_utf8_markdown_file_can_be_ingested() -> None:
    service, _ = build_service()
    fixture = Path(__file__).parent / "fixtures" / "sample.md"

    result = service.ingest_file(fixture)

    assert result.document.source_name == "sample.md"
    assert result.chunks[0].locator == "示例知识"
