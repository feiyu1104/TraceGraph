import pytest

from tracegraph.core.contracts import Entity, Relation
from tracegraph.graph_backend import create_graph_repository
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.storage.consistency import check_sqlite_consistency
from tracegraph.storage.graph import SQLiteGraphRepository
from tracegraph.storage.sqlite import SQLiteDocumentRepository


def _database(tmp_path):
    return tmp_path / "tracegraph.db"


def test_sqlite_is_selected_without_any_neo4j_configuration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRACEGRAPH_GRAPH_BACKEND", "sqlite")

    graph, selection = create_graph_repository(_database(tmp_path))
    graph.close()

    assert selection.active == "sqlite"
    assert selection.degraded is False
    assert selection.to_dict()["graph_degraded"] == "false"


def test_missing_neo4j_configuration_fails_instead_of_degrading(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRACEGRAPH_GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("TRACEGRAPH_GRAPH_FALLBACK", "none")
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    # 默认不降级：换了后端就可能换掉查询结果，必须显式同意。
    with pytest.raises(RuntimeError, match="NEO4J_PASSWORD"):
        create_graph_repository(_database(tmp_path))


def test_degradation_is_recorded_when_explicitly_allowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRACEGRAPH_GRAPH_BACKEND", "neo4j")
    monkeypatch.setenv("TRACEGRAPH_GRAPH_FALLBACK", "sqlite")
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    graph, selection = create_graph_repository(_database(tmp_path))
    graph.close()

    assert selection.requested == "neo4j"
    assert selection.active == "sqlite"
    assert selection.degraded is True
    assert selection.to_dict() == {
        "graph_requested": "neo4j",
        "graph_degraded": "true",
        "graph_detail": "使用 Neo4j 时必须设置 NEO4J_PASSWORD",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("TRACEGRAPH_GRAPH_BACKEND", "postgres"),
        ("TRACEGRAPH_GRAPH_FALLBACK", "neo4j"),
    ),
)
def test_unknown_backend_settings_are_rejected(tmp_path, monkeypatch, name, value) -> None:
    monkeypatch.setenv("TRACEGRAPH_GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("TRACEGRAPH_GRAPH_FALLBACK", "none")
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name):
        create_graph_repository(_database(tmp_path))


def test_consistent_database_reports_no_issues(tmp_path) -> None:
    database = _database(tmp_path)
    documents = SQLiteDocumentRepository(database)
    # 直接建 SQLite 图仓储，不经过后端选择 —— 这里验的是库本身，与配置无关。
    graph = SQLiteGraphRepository(database)
    try:
        chunk_id = TextIngestionService(documents).ingest_text(
            "dutmed-百日咳.md", "百日咳的推荐药物包括琥乙红霉素片。"
        ).chunks[0].id
        graph.upsert_entity(Entity("d1", "百日咳", "Disease"))
        graph.upsert_entity(Entity("m1", "琥乙红霉素片", "Drug"))
        graph.upsert_relation(
            Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", (chunk_id,))
        )
    finally:
        documents.close()
        graph.close()

    report = check_sqlite_consistency(database)

    assert report.is_consistent
    assert report.statistics.entities == 2
    assert report.statistics.relations == 1
    assert report.statistics.relation_types == (("RECOMMENDS_DRUG", 1),)
    assert report.statistics.entity_types == (("Disease", 1), ("Drug", 1))


def test_deleting_a_document_leaves_a_detectable_dangling_reference(tmp_path) -> None:
    database = _database(tmp_path)
    documents = SQLiteDocumentRepository(database)
    graph = SQLiteGraphRepository(database)
    try:
        result = TextIngestionService(documents).ingest_text(
            "dutmed-百日咳.md", "百日咳的推荐药物包括琥乙红霉素片。"
        )
        graph.upsert_entity(Entity("d1", "百日咳", "Disease"))
        graph.upsert_entity(Entity("m1", "琥乙红霉素片", "Drug"))
        graph.upsert_relation(
            Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", (result.chunks[0].id,))
        )
        # 绕过生命周期服务直接删文档：图侧的证据引用就悬空了。
        documents.delete_document(result.document.id)
    finally:
        documents.close()
        graph.close()

    report = check_sqlite_consistency(database)

    assert not report.is_consistent
    assert report.dangling_evidence == (("r1", result.chunks[0].id),)
