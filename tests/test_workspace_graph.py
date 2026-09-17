"""图后端的 Workspace 隔离、旧库迁移，以及两个后端的行为一致性。"""

import sqlite3

import pytest

from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    Entity,
    Relation,
    TraversalDirection,
    Workspace,
)
from tracegraph.core.identity import graph_entity_id, normalize_name
from tracegraph.retrieval.traversal import traverse_paths
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.storage.consistency import check_sqlite_consistency
from tracegraph.storage.graph import InMemoryGraphRepository, SQLiteGraphRepository
from tracegraph.storage.sqlite import SQLiteDocumentRepository

# 两个 Workspace 用同一个实体名称与类型：只要哪一层漏了 workspace_id，
# 「同名同类型不合并」这条立刻会失败。
_OTHER = "ws-b"


def _seed(repository) -> None:
    repository.upsert_entity(Entity("a-d1", "百日咳", "Disease", DEFAULT_WORKSPACE_ID))
    repository.upsert_entity(Entity("a-m1", "琥乙红霉素片", "Drug", DEFAULT_WORKSPACE_ID))
    repository.upsert_entity(Entity("b-d1", "百日咳", "Disease", _OTHER))
    repository.upsert_entity(Entity("b-m1", "琥乙红霉素片", "Drug", _OTHER))
    repository.upsert_relation(
        Relation(
            "a-r1",
            "a-d1",
            "a-m1",
            "RECOMMENDS_DRUG",
            ("a-c1",),
            DEFAULT_WORKSPACE_ID,
        )
    )
    repository.upsert_relation(
        Relation("b-r1", "b-d1", "b-m1", "RECOMMENDS_DRUG", ("b-c1",), _OTHER)
    )


@pytest.fixture(params=("memory", "sqlite"))
def graph(request, tmp_path):
    if request.param == "memory":
        repository = InMemoryGraphRepository()
    else:
        repository = SQLiteGraphRepository(tmp_path / "graph.db")
    _seed(repository)
    yield repository
    if hasattr(repository, "close"):
        repository.close()


def test_same_name_and_type_in_two_workspaces_stay_separate(graph) -> None:
    first = graph.find_entity_by_key(DEFAULT_WORKSPACE_ID, "Disease", normalize_name("百日咳"))
    second = graph.find_entity_by_key(_OTHER, "Disease", normalize_name("百日咳"))

    assert first is not None and second is not None
    assert first.id != second.id
    assert first.workspace_id == DEFAULT_WORKSPACE_ID
    assert second.workspace_id == _OTHER
    assert graph.get_entity(first.id, _OTHER) is None
    assert graph.get_entity(second.id, DEFAULT_WORKSPACE_ID) is None


def test_a_workspace_sees_only_its_own_entities(graph) -> None:
    assert {item.id for item in graph.search_entities("百日咳", DEFAULT_WORKSPACE_ID)} == {
        "a-d1"
    }
    assert {item.id for item in graph.search_entities("百日咳", _OTHER)} == {"b-d1"}


def test_relations_are_scoped_to_the_workspace(graph) -> None:
    assert [item.id for item in graph.list_relations(("a-d1",), DEFAULT_WORKSPACE_ID)] == [
        "a-r1"
    ]
    assert [item.id for item in graph.list_relations(("a-d1",), _OTHER)] == []
    assert graph.get_relation("a-r1", _OTHER) is None
    assert graph.get_relation("b-r1", DEFAULT_WORKSPACE_ID) is None


def test_traversal_never_crosses_workspaces(graph) -> None:
    start = graph.get_entity("a-d1", DEFAULT_WORKSPACE_ID)
    assert start is not None

    paths = traverse_paths(graph, start, workspace_id=DEFAULT_WORKSPACE_ID, max_hops=3)

    assert paths
    visited = {
        node.id for path in paths for node in (path.start, *(step.target for step in path.steps))
    }
    assert visited == {"a-d1", "a-m1"}
    assert "b-d1" not in visited and "b-m1" not in visited


def test_traversal_start_must_belong_to_the_workspace(graph) -> None:
    start = graph.get_entity("a-d1", DEFAULT_WORKSPACE_ID)

    with pytest.raises(ValueError):
        traverse_paths(graph, start, workspace_id=_OTHER)


def test_expansion_is_scoped_to_the_workspace(graph) -> None:
    expansions = graph.expand_frontier(
        ("a-d1",), workspace_id=_OTHER, fanout=10
    )

    assert expansions[0].steps == ()
    assert expansions[0].total_edges == 0


def test_statistics_are_per_workspace(graph) -> None:
    own = graph.statistics(DEFAULT_WORKSPACE_ID)
    other = graph.statistics(_OTHER)

    assert own.entities == 2 and own.relations == 1
    assert other.entities == 2 and other.relations == 1
    assert ("Disease", 1) in own.entity_types


def test_removing_evidence_does_not_touch_another_workspace(graph) -> None:
    graph.remove_evidence(("a-c1",), DEFAULT_WORKSPACE_ID)

    assert graph.get_relation("a-r1", DEFAULT_WORKSPACE_ID) is None
    kept = graph.get_relation("b-r1", _OTHER)
    assert kept is not None and kept.evidence_chunk_ids == ("b-c1",)


def test_deleting_outgoing_relations_is_scoped(graph) -> None:
    graph.delete_outgoing_relations("a-d1", _OTHER)

    assert graph.get_relation("a-r1", DEFAULT_WORKSPACE_ID) is not None

    graph.delete_outgoing_relations("a-d1", DEFAULT_WORKSPACE_ID)

    assert graph.get_relation("a-r1", DEFAULT_WORKSPACE_ID) is None
    assert graph.get_relation("b-r1", _OTHER) is not None


def test_relation_endpoints_must_share_the_workspace(tmp_path) -> None:
    repository = SQLiteGraphRepository(tmp_path / "graph.db")
    _seed(repository)

    with pytest.raises(ValueError):
        repository.upsert_relation(
            Relation("x-r1", "a-d1", "b-m1", "RECOMMENDS_DRUG", ("c",), DEFAULT_WORKSPACE_ID)
        )

    assert repository.get_relation("x-r1", DEFAULT_WORKSPACE_ID) is None
    repository.close()


def test_type_matching_is_case_insensitive_on_both_backends(graph) -> None:
    found = graph.find_entity_by_key(DEFAULT_WORKSPACE_ID, "disease", normalize_name("百日咳"))

    assert found is not None and found.id == "a-d1"


def test_graph_entity_id_is_workspace_scoped() -> None:
    first = graph_entity_id(DEFAULT_WORKSPACE_ID, "Disease", normalize_name("百日咳"))
    second = graph_entity_id(_OTHER, "Disease", normalize_name("百日咳"))

    assert first != second
    # 类型大小写不影响稳定 ID：同一实体不会因为写法不同裂成两个。
    assert graph_entity_id(DEFAULT_WORKSPACE_ID, "disease", normalize_name("百日咳")) == first


def _legacy_database(path) -> None:
    """建一个 Workspace 概念出现之前的图库：没有 workspace_id 列。"""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL
        );
        CREATE TABLE relations (
            id TEXT PRIMARY KEY,
            source_entity_id TEXT NOT NULL REFERENCES entities(id),
            target_entity_id TEXT NOT NULL REFERENCES entities(id),
            type TEXT NOT NULL
        );
        CREATE TABLE relation_evidence (
            relation_id TEXT NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
            chunk_id TEXT NOT NULL,
            PRIMARY KEY (relation_id, chunk_id)
        );
        INSERT INTO entities VALUES ('ent-old', '百日咳', 'Disease');
        INSERT INTO entities VALUES ('ent-old-2', '琥乙红霉素片', 'Drug');
        INSERT INTO relations VALUES ('rel-old', 'ent-old', 'ent-old-2', 'RECOMMENDS_DRUG');
        INSERT INTO relation_evidence VALUES ('rel-old', 'chk-old');
        """
    )
    connection.commit()
    connection.close()


def test_legacy_rows_are_handed_to_the_default_workspace(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_database(path)

    repository = SQLiteGraphRepository(path)

    assert repository.get_entity("ent-old", DEFAULT_WORKSPACE_ID) is not None
    relation = repository.get_relation("rel-old", DEFAULT_WORKSPACE_ID)
    assert relation is not None and relation.evidence_chunk_ids == ("chk-old",)
    assert repository.statistics(DEFAULT_WORKSPACE_ID).entities == 2
    # 数据没有被重建，ID 还是原来那些。
    assert repository.get_entity("ent-old", _OTHER) is None
    repository.close()


def test_migration_is_idempotent_and_does_not_overwrite(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_database(path)
    first = SQLiteGraphRepository(path)
    # 一行已经被划到别的 Workspace：再迁移一次不许把它拽回默认 Workspace。
    first.upsert_entity(Entity("ent-old-2", "琥乙红霉素片", "Drug", _OTHER))
    first.close()

    second = SQLiteGraphRepository(path)
    third = SQLiteGraphRepository(path)

    assert second.get_entity("ent-old-2", _OTHER) is not None
    assert second.get_entity("ent-old-2", DEFAULT_WORKSPACE_ID) is None
    assert second.statistics(_OTHER).entities == 1
    assert second.statistics(DEFAULT_WORKSPACE_ID).entities == 1
    assert third.statistics(DEFAULT_WORKSPACE_ID) == second.statistics(
        DEFAULT_WORKSPACE_ID
    )
    assert third.get_relation("rel-old", DEFAULT_WORKSPACE_ID) is not None
    second.close()
    third.close()


def test_migration_repairs_a_partially_migrated_graph_schema(tmp_path) -> None:
    path = tmp_path / "partial.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'ws-default'
        );
        CREATE TABLE relations (
            id TEXT PRIMARY KEY,
            source_entity_id TEXT NOT NULL REFERENCES entities(id),
            target_entity_id TEXT NOT NULL REFERENCES entities(id),
            type TEXT NOT NULL
        );
        CREATE TABLE relation_evidence (
            relation_id TEXT NOT NULL REFERENCES relations(id) ON DELETE CASCADE,
            chunk_id TEXT NOT NULL,
            PRIMARY KEY (relation_id, chunk_id)
        );
        INSERT INTO entities VALUES ('ent-a', '项目 A', 'Project', 'ws-default');
        INSERT INTO entities VALUES ('ent-b', '任务 B', 'Task', 'ws-default');
        INSERT INTO relations VALUES ('rel-a', 'ent-a', 'ent-b', 'RELATED_TO');
        INSERT INTO relation_evidence VALUES ('rel-a', 'chunk-a');
        """
    )
    connection.commit()
    connection.close()

    repository = SQLiteGraphRepository(path)

    relation = repository.get_relation("rel-a", DEFAULT_WORKSPACE_ID)
    assert relation is not None
    assert relation.workspace_id == DEFAULT_WORKSPACE_ID
    repository.close()


def test_consistency_report_is_scoped_to_a_workspace(tmp_path) -> None:
    path = tmp_path / "tracegraph.db"
    documents = SQLiteDocumentRepository(path)
    repository = SQLiteGraphRepository(path)
    try:
        documents.save_workspace(
            Workspace(
                id=_OTHER,
                name=_OTHER,
                adapter_id="medical",
                created_at="2026-01-01T00:00:00+00:00",
            )
        )
        for workspace_id, text in (
            (DEFAULT_WORKSPACE_ID, "百日咳的推荐药物包括琥乙红霉素片。"),
            (_OTHER, "支原体肺炎的推荐药物也包括琥乙红霉素片。"),
        ):
            result = TextIngestionService(documents).ingest_text(
                f"notes-{workspace_id}.md", text, workspace_id
            )
            # ID 在三个后端里都是全局唯一的，因此两个 Workspace 用各自的
            # 前缀；发布服务生成的稳定 ID 天然带 workspace_id，不会撞。
            prefix = "a" if workspace_id == DEFAULT_WORKSPACE_ID else "b"
            chunk_ids = (result.chunks[0].id,)
            repository.upsert_entity(
                Entity(f"{prefix}-d1", "百日咳", "Disease", workspace_id)
            )
            repository.upsert_entity(
                Entity(f"{prefix}-m1", "琥乙红霉素片", "Drug", workspace_id)
            )
            repository.upsert_relation(
                Relation(
                    f"{prefix}-r1",
                    f"{prefix}-d1",
                    f"{prefix}-m1",
                    "RECOMMENDS_DRUG",
                    chunk_ids,
                    workspace_id,
                )
            )
    finally:
        documents.close()
        repository.close()

    report = check_sqlite_consistency(path, DEFAULT_WORKSPACE_ID)

    assert report.statistics.entities == 2
    assert report.statistics.relations == 1
    assert report.dangling_evidence == ()
    assert report.is_consistent
    # 另一个 Workspace 的合计数不会混进来；两边各自自洽。
    other = check_sqlite_consistency(path, _OTHER)
    assert other.statistics.entities == 2
    assert other.statistics.relations == 1
    assert other.is_consistent


def test_direction_filter_stays_inside_the_workspace(graph) -> None:
    outgoing = graph.expand_frontier(
        ("a-d1",),
        workspace_id=DEFAULT_WORKSPACE_ID,
        fanout=10,
        direction=TraversalDirection.OUTGOING,
    )
    incoming = graph.expand_frontier(
        ("a-d1",),
        workspace_id=DEFAULT_WORKSPACE_ID,
        fanout=10,
        direction=TraversalDirection.INCOMING,
    )
    foreign = graph.expand_frontier(
        ("b-d1",), workspace_id=DEFAULT_WORKSPACE_ID, fanout=10
    )

    assert {step.relation.id for step in outgoing[0].steps} == {"a-r1"}
    assert incoming[0].steps == ()
    assert foreign[0].steps == ()
