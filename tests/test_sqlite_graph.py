from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    Entity,
    Relation,
    TraversalDirection,
)
from tracegraph.core.ports import GraphRepository
from tracegraph.storage.graph import InMemoryGraphRepository, SQLiteGraphRepository


# 两个后端的一致性用例都落在默认 Workspace 里。
WORKSPACE = DEFAULT_WORKSPACE_ID

_ENTITIES = (
    Entity("d1", "百日咳", "Disease"),
    Entity("d2", "小儿支原体肺炎", "Disease"),
    Entity("d3", "急性肺炎", "Disease"),
    Entity("m1", "琥乙红霉素片", "Drug"),
    Entity("s1", "阵发性咳嗽", "Symptom"),
    Entity("k1", "儿科", "Department"),
)

_RELATIONS = (
    Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", ("c1",)),
    Relation("r2", "d2", "m1", "RECOMMENDS_DRUG", ("c2",)),
    Relation("r3", "d1", "s1", "HAS_SYMPTOM", ("c3",)),
    Relation("r4", "d3", "s1", "HAS_SYMPTOM", ("c4",)),
    Relation("r5", "d1", "k1", "TREATED_BY", ("c5",)),
    Relation("r6", "d2", "k1", "TREATED_BY", ("c6",)),
    Relation("r7", "d1", "d1", "SELF_LOOP", ("c7",)),
    Relation("r8", "d2", "d1", "RELATED", ("c8", "c9")),
)


def _populate(repository: GraphRepository) -> None:
    for entity in _ENTITIES:
        repository.upsert_entity(entity)
    for relation in _RELATIONS:
        repository.upsert_relation(relation)


def _expand(repository: GraphRepository, nodes: tuple[str, ...], **kwargs):
    return tuple(
        (
            expansion.node_id,
            expansion.total_edges,
            tuple(
                (
                    step.relation.id,
                    step.relation.type,
                    step.relation.source_entity_id,
                    step.relation.target_entity_id,
                    step.relation.evidence_chunk_ids,
                    step.direction.value,
                    step.target.id,
                )
                for step in expansion.steps
            ),
        )
        for expansion in repository.expand_frontier(
            nodes, workspace_id=WORKSPACE, **kwargs
        )
    )


def test_expand_frontier_matches_between_backends(tmp_path) -> None:
    memory = InMemoryGraphRepository()
    _populate(memory)
    sqlite = SQLiteGraphRepository(tmp_path / "graph.db")
    _populate(sqlite)

    cases = (
        ((("d1",)), {"fanout": 32}),
        ((("d1",)), {"fanout": 1}),
        ((("d1", "d2", "k1")), {"fanout": 32}),
        ((("d1",)), {"fanout": 32, "direction": TraversalDirection.OUTGOING}),
        ((("k1",)), {"fanout": 32, "direction": TraversalDirection.INCOMING}),
        ((("d1",)), {"fanout": 32, "relation_types": ("HAS_SYMPTOM",)}),
        ((("d1",)), {"fanout": 32, "relation_types": ("NOPE",)}),
        ((("missing",)), {"fanout": 32}),
        ((), {"fanout": 32}),
    )

    for nodes, kwargs in cases:
        assert _expand(memory, nodes, **kwargs) == _expand(sqlite, nodes, **kwargs), (
            nodes,
            kwargs,
        )

    sqlite.close()


def test_self_loop_is_excluded_from_expansion(tmp_path) -> None:
    sqlite = SQLiteGraphRepository(tmp_path / "graph.db")
    _populate(sqlite)

    expansion = sqlite.expand_frontier(("d1",), workspace_id=WORKSPACE, fanout=32)[0]

    assert "r7" not in {step.relation.id for step in expansion.steps}
    # 出边 r1/r3/r5 + 入边 r8，自环 r7 不计入总数。
    assert expansion.total_edges == 4

    sqlite.close()


def test_statistics_matches_between_backends(tmp_path) -> None:
    memory = InMemoryGraphRepository()
    _populate(memory)
    sqlite = SQLiteGraphRepository(tmp_path / "graph.db")
    _populate(sqlite)

    assert memory.statistics(WORKSPACE) == sqlite.statistics(WORKSPACE)
    # d3 只有 r4 一条入边（s1 无出边），m1/s1/k1 都连着关系，没有孤立实体。
    assert sqlite.statistics(WORKSPACE).entities == 6
    assert sqlite.statistics(WORKSPACE).relations == 8
    assert sqlite.statistics(WORKSPACE).orphan_entities == 0
    assert ("Disease", 3) in sqlite.statistics(WORKSPACE).entity_types

    sqlite.close()


def test_get_relation_returns_evidence(tmp_path) -> None:
    sqlite = SQLiteGraphRepository(tmp_path / "graph.db")
    _populate(sqlite)

    relation = sqlite.get_relation("r8", WORKSPACE)

    assert relation is not None
    assert relation.evidence_chunk_ids == ("c8", "c9")
    assert sqlite.get_relation("nope", WORKSPACE) is None

    sqlite.close()


def test_find_opposing_relations_matches_between_backends(tmp_path) -> None:
    memory = InMemoryGraphRepository()
    sqlite = SQLiteGraphRepository(tmp_path / "graph.db")
    for repository in (memory, sqlite):
        repository.upsert_entity(Entity("d1", "绝经与心血管疾病", "Disease"))
        repository.upsert_entity(Entity("f1", "杏仁", "Food"))
        repository.upsert_entity(Entity("f2", "鸡蛋", "Food"))
        repository.upsert_relation(Relation("p1", "d1", "f1", "SHOULD_EAT", ("c1",)))
        repository.upsert_relation(Relation("n1", "d1", "f1", "SHOULD_NOT_EAT", ("c2",)))
        repository.upsert_relation(Relation("p2", "d1", "f2", "SHOULD_EAT", ("c3",)))

    types = ("SHOULD_EAT", "SHOULD_NOT_EAT")
    expected = tuple(
        (relation.id, relation.type, relation.evidence_chunk_ids)
        for relation in memory.find_opposing_relations("d1", types, WORKSPACE)
    )
    actual = tuple(
        (relation.id, relation.type, relation.evidence_chunk_ids)
        for relation in sqlite.find_opposing_relations("d1", types, WORKSPACE)
    )

    # 杏仁同时有宜吃与忌吃 -> 冲突；鸡蛋只有宜吃 -> 不算。
    assert expected == actual
    assert {item[0] for item in expected} == {"p1", "n1"}

    sqlite.close()
