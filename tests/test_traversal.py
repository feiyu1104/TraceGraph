import pytest

from tracegraph.core.contracts import Entity, Relation, TraversalDirection
from tracegraph.retrieval.traversal import traverse_paths
from tracegraph.storage.graph import InMemoryGraphRepository


def _build() -> tuple[InMemoryGraphRepository, dict[str, Entity]]:
    entities = {
        "d1": Entity("d1", "百日咳", "Disease"),
        "d2": Entity("d2", "小儿支原体肺炎", "Disease"),
        "d3": Entity("d3", "急性肺炎", "Disease"),
        "m1": Entity("m1", "琥乙红霉素片", "Drug"),
        "s1": Entity("s1", "阵发性咳嗽", "Symptom"),
        "k1": Entity("k1", "儿科", "Department"),
    }
    relations = (
        Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", ("c1",)),
        Relation("r2", "d2", "m1", "RECOMMENDS_DRUG", ("c2",)),
        Relation("r3", "d1", "s1", "HAS_SYMPTOM", ("c3",)),
        Relation("r4", "d3", "s1", "HAS_SYMPTOM", ("c4",)),
        Relation("r5", "d1", "k1", "TREATED_BY", ("c5",)),
        Relation("r6", "d2", "k1", "TREATED_BY", ("c6",)),
        Relation("r7", "d1", "d1", "SELF_LOOP", ("c7",)),
        Relation("r8", "d2", "d1", "RELATED", ("c8",)),
    )
    graph = InMemoryGraphRepository()
    for entity in entities.values():
        graph.upsert_entity(entity)
    for relation in relations:
        graph.upsert_relation(relation)
    return graph, entities


def test_single_hop_lists_every_incident_relation() -> None:
    graph, entities = _build()

    paths = traverse_paths(graph, entities["d1"], max_hops=1)

    assert {path.steps[0].relation.id for path in paths} == {"r1", "r3", "r5", "r8"}
    assert all(path.hop_count == 1 for path in paths)


def test_self_loop_never_becomes_a_path() -> None:
    graph, entities = _build()

    paths = traverse_paths(graph, entities["d1"], max_hops=3)

    assert all("r7" not in {step.relation.id for step in path.steps} for path in paths)


def test_two_hops_reach_the_expected_association() -> None:
    graph, entities = _build()

    paths = traverse_paths(graph, entities["d1"], max_hops=2)
    two_hop = {
        tuple(step.relation.id for step in path.steps)
        for path in paths
        if path.hop_count == 2
    }

    # 百日咳 → 琥乙红霉素片 ← 小儿支原体肺炎
    assert ("r1", "r2") in two_hop
    # 百日咳 → 阵发性咳嗽 ← 急性肺炎
    assert ("r3", "r4") in two_hop


def test_paths_never_repeat_a_node() -> None:
    graph, entities = _build()

    paths = traverse_paths(graph, entities["d1"], max_hops=3)

    for path in paths:
        node_ids = [node.id for node in path.nodes]
        assert len(node_ids) == len(set(node_ids))


def test_deeper_hop_levels_are_not_starved_by_shallower_ones() -> None:
    graph, entities = _build()

    paths = traverse_paths(graph, entities["d1"], max_hops=3, limit=2)

    # 逐层限额是每层 2 条；若在末尾统一截断，三跳会被整体丢掉。
    assert {path.hop_count for path in paths} == {1, 2, 3}


def test_fanout_truncation_is_reported_with_real_edge_count() -> None:
    graph, entities = _build()

    path = traverse_paths(graph, entities["d1"], max_hops=1, fanout=2)[0]

    truncation = path.truncations[0]
    assert truncation.entity_id == "d1"
    assert truncation.total_edges == 4
    assert truncation.shown_edges == 2


def test_relation_type_filter_applies_to_every_hop() -> None:
    graph, entities = _build()

    paths = traverse_paths(
        graph, entities["d1"], max_hops=2, relation_types=("RECOMMENDS_DRUG",)
    )

    assert {path.steps[0].relation.type for path in paths} == {"RECOMMENDS_DRUG"}
    assert {path.hop_count for path in paths} == {1, 2}


def test_direction_filter_restricts_expansion() -> None:
    graph, entities = _build()

    outgoing = traverse_paths(
        graph, entities["d1"], max_hops=1, direction=TraversalDirection.OUTGOING
    )
    incoming = traverse_paths(
        graph, entities["d1"], max_hops=1, direction=TraversalDirection.INCOMING
    )

    assert {path.steps[0].relation.id for path in outgoing} == {"r1", "r3", "r5"}
    assert {path.steps[0].relation.id for path in incoming} == {"r8"}


def test_traversal_is_deterministic() -> None:
    graph, entities = _build()

    first = traverse_paths(graph, entities["d1"], max_hops=3)
    second = traverse_paths(graph, entities["d1"], max_hops=3)

    assert first == second


def test_max_hops_out_of_range_is_rejected() -> None:
    graph, entities = _build()

    with pytest.raises(ValueError, match="max_hops"):
        traverse_paths(graph, entities["d1"], max_hops=4)
    with pytest.raises(ValueError, match="max_hops"):
        traverse_paths(graph, entities["d1"], max_hops=0)
