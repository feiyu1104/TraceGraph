import pytest

from tracegraph.ingestion.service import TextIngestionService
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.storage.memory import InMemoryDocumentRepository
from tracegraph.storage.sqlite import SQLiteDocumentRepository


def test_retrieval_returns_ranked_traceable_evidence() -> None:
    repository = InMemoryDocumentRepository()
    ingestion = TextIngestionService(repository, chunk_size=30)
    ingestion.ingest_text(
        "高血压指南.md",
        "# 检查\n\n高血压患者应定期监测血压。\n\n# 治疗\n\n治疗方案应由医生制定。",
    )

    evidences = KeywordRetriever(repository).retrieve("高血压监测", limit=1)

    assert len(evidences) == 1
    assert evidences[0].source_name == "高血压指南.md"
    assert evidences[0].locator == "检查"
    assert evidences[0].retrieval_method == "keyword"
    assert evidences[0].retrieval_score > 0
    assert evidences[0].chunk_id
    assert evidences[0].document_version


def test_retrieval_only_searches_latest_document_version() -> None:
    repository = InMemoryDocumentRepository()
    ingestion = TextIngestionService(repository)
    ingestion.ingest_text("指南.txt", "旧版包含阿司匹林用药说明。")
    latest = ingestion.ingest_text("指南.txt", "新版仅包含血压监测说明。")

    retriever = KeywordRetriever(repository)

    assert retriever.retrieve("阿司匹林") == ()
    evidence = retriever.retrieve("血压监测")[0]
    assert evidence.document_version == latest.version.id


def test_retrieval_returns_empty_when_terms_do_not_match() -> None:
    repository = InMemoryDocumentRepository()
    TextIngestionService(repository).ingest_text("指南.txt", "患者应定期监测血压。")

    assert KeywordRetriever(repository).retrieve("阿司匹林") == ()


@pytest.mark.parametrize(("query", "limit"), (("", 5), ("血压", 0)))
def test_retrieval_validates_query_and_limit(query: str, limit: int) -> None:
    retriever = KeywordRetriever(InMemoryDocumentRepository())

    with pytest.raises(ValueError):
        retriever.retrieve(query, limit)


def test_retrieval_works_with_sqlite_repository(tmp_path) -> None:
    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        TextIngestionService(repository).ingest_text("指南.txt", "患者应定期监测血压。")

        evidence = KeywordRetriever(repository).retrieve("血压监测")[0]

        assert evidence.source_name == "指南.txt"
