import pytest

from tracegraph.core.contracts import (
    Entity,
    GraphPath,
    PathStep,
    PathTruncation,
    Relation,
    TraversalDirection,
)


def _entity(entity_id: str, name: str) -> Entity:
    return Entity(id=entity_id, name=name, type="Disease")


def _relation(relation_id: str, source: str, target: str, relation_type: str) -> Relation:
    return Relation(
        id=relation_id,
        source_entity_id=source,
        target_entity_id=target,
        type=relation_type,
        evidence_chunk_ids=(f"chunk-{relation_id}",),
    )


def test_nodes_are_derived_from_start_and_steps() -> None:
    start = _entity("d1", "百日咳")
    middle = _entity("m1", "琥乙红霉素片")
    end = _entity("d2", "小儿支原体肺炎")
    path = GraphPath(
        start=start,
        steps=(
            PathStep(
                relation=_relation("r1", "d1", "m1", "RECOMMENDS_DRUG"),
                direction=TraversalDirection.OUTGOING,
                target=middle,
            ),
            PathStep(
                relation=_relation("r2", "d2", "m1", "RECOMMENDS_DRUG"),
                direction=TraversalDirection.INCOMING,
                target=end,
            ),
        ),
    )

    assert path.nodes == (start, middle, end)
    assert path.hop_count == 2


def test_path_rejects_repeated_node() -> None:
    start = _entity("d1", "百日咳")
    with pytest.raises(ValueError, match="重复节点"):
        GraphPath(
            start=start,
            steps=(
                PathStep(
                    relation=_relation("r1", "d1", "d2", "ACCOMPANIES"),
                    direction=TraversalDirection.OUTGOING,
                    target=_entity("d2", "急性肺炎"),
                ),
                PathStep(
                    relation=_relation("r2", "d2", "d1", "ACCOMPANIES"),
                    direction=TraversalDirection.OUTGOING,
                    target=start,
                ),
            ),
        )


def test_step_rejects_direction_inconsistent_with_target() -> None:
    start = _entity("d1", "百日咳")
    other = _entity("d2", "急性肺炎")
    # r1 是 d1 -> d2，沿 OUTGOING 走到的应当是 d2，这里却写成 d1。
    with pytest.raises(ValueError, match="方向与目标实体不一致"):
        PathStep(
            relation=_relation("r1", "d1", "d2", "ACCOMPANIES"),
            direction=TraversalDirection.OUTGOING,
            target=start,
        )

    with pytest.raises(ValueError, match="方向与目标实体不一致"):
        PathStep(
            relation=_relation("r1", "d1", "d2", "ACCOMPANIES"),
            direction=TraversalDirection.INCOMING,
            target=other,
        )


def test_path_rejects_disconnected_steps() -> None:
    with pytest.raises(ValueError, match="首尾不相接"):
        GraphPath(
            start=_entity("d1", "百日咳"),
            steps=(
                PathStep(
                    relation=_relation("r1", "d1", "d2", "ACCOMPANIES"),
                    direction=TraversalDirection.OUTGOING,
                    target=_entity("d2", "急性肺炎"),
                ),
                PathStep(
                    # 从 d2 出发却接了一条 d3 -> d4 的关系。
                    relation=_relation("r2", "d3", "d4", "ACCOMPANIES"),
                    direction=TraversalDirection.OUTGOING,
                    target=_entity("d4", "肺炎"),
                ),
            ),
        )


def test_empty_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="至少需要一跳"):
        GraphPath(start=_entity("d1", "百日咳"), steps=())


def test_label_marks_traversal_direction() -> None:
    path = GraphPath(
        start=_entity("d1", "百日咳"),
        steps=(
            PathStep(
                relation=_relation("r1", "d1", "m1", "RECOMMENDS_DRUG"),
                direction=TraversalDirection.OUTGOING,
                target=_entity("m1", "琥乙红霉素片"),
            ),
            PathStep(
                relation=_relation("r2", "d2", "m1", "RECOMMENDS_DRUG"),
                direction=TraversalDirection.INCOMING,
                target=_entity("d2", "小儿支原体肺炎"),
            ),
        ),
    )

    assert path.label() == "百日咳 → RECOMMENDS_DRUG → 琥乙红霉素片 ← RECOMMENDS_DRUG ← 小儿支原体肺炎"


def test_truncation_only_records_actual_truncation() -> None:
    assert PathTruncation(entity_id="k1", total_edges=795, shown_edges=32).total_edges == 795
    with pytest.raises(ValueError, match="确实发生截断"):
        PathTruncation(entity_id="k1", total_edges=32, shown_edges=32)
