from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    MAX_HOPS,
    Entity,
    GraphPath,
    PathStep,
    PathTruncation,
    TraversalDirection,
)
from tracegraph.core.ports import GraphRepository


DEFAULT_FANOUT = 32
MAX_PATHS = 200


def traverse_paths(
    graph: GraphRepository,
    start: Entity,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    limit: int = MAX_PATHS,
    fanout: int = DEFAULT_FANOUT,
    relation_types: tuple[str, ...] | None = None,
    direction: TraversalDirection | None = None,
) -> tuple[GraphPath, ...]:
    """从 start 出发逐层扩展，返回确定性排序的图路径。

    遍历语义由本函数统一定义，所有图后端只负责实现 `expand_frontier`
    这一个原语，因此「各后端实现相同的逐层遍历」是结构上成立的。
    返回条数上限是 `max_hops × limit`（每个跳数层级各至多 limit 条）。
    """
    if max_hops < 1 or max_hops > MAX_HOPS:
        raise ValueError(f"max_hops 必须在 1 到 {MAX_HOPS} 之间")
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if fanout < 1:
        raise ValueError("fanout 必须大于 0")

    truncations: dict[str, PathTruncation] = {}
    collected: list[GraphPath] = []
    level: list[tuple[tuple[PathStep, ...], frozenset[str], Entity]] = [
        ((), frozenset({start.id}), start)
    ]

    for _ in range(max_hops):
        if not level:
            break
        expansions = {
            expansion.node_id: expansion
            for expansion in graph.expand_frontier(
                tuple(sorted({tail.id for _, _, tail in level})),
                fanout=fanout,
                relation_types=relation_types,
                direction=direction,
            )
        }
        for node_id, expansion in expansions.items():
            if expansion.total_edges > len(expansion.steps):
                truncations[node_id] = PathTruncation(
                    entity_id=node_id,
                    total_edges=expansion.total_edges,
                    shown_edges=len(expansion.steps),
                )

        candidates: list[tuple[tuple[PathStep, ...], frozenset[str], Entity]] = []
        for steps, visited, tail in level:
            expansion = expansions.get(tail.id)
            if expansion is None:
                continue
            for step in expansion.steps:
                if step.target.id in visited:
                    continue
                candidates.append(((*steps, step), visited | {step.target.id}, step.target))
        candidates.sort(key=lambda candidate: _steps_key(candidate[0]))
        level = candidates[:limit]
        collected.extend(
            GraphPath(
                start=start,
                steps=steps,
                truncations=_path_truncations(start, steps, truncations),
            )
            for steps, _, _ in level
        )

    # 按 (跳数, 各步关系 id, 各节点 id) 升序 —— 1 跳优先，次序确定。
    collected.sort(key=lambda path: (path.hop_count, _steps_key(path.steps)))
    # 每个跳数层级各保留至多 limit 条：若只在末尾统一截断，浅层路径会占满
    # 名额并把最深一层整体挤掉，使 max_hops 形同虚设。各层的 frontier 预算
    # 仍是 limit，路径爆炸的防线没有松动。
    per_level: dict[int, int] = {}
    kept: list[GraphPath] = []
    for path in collected:
        taken = per_level.get(path.hop_count, 0)
        if taken >= limit:
            continue
        per_level[path.hop_count] = taken + 1
        kept.append(path)
    return tuple(kept)


def _steps_key(steps: tuple[PathStep, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(step.relation.id for step in steps),
        tuple(step.target.id for step in steps),
    )


def _path_truncations(
    start: Entity,
    steps: tuple[PathStep, ...],
    truncations: dict[str, PathTruncation],
) -> tuple[PathTruncation, ...]:
    nodes = (start, *(step.target for step in steps))
    return tuple(truncations[node.id] for node in nodes if node.id in truncations)
