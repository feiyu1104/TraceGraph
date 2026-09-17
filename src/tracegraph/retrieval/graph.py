import hashlib

from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    MAX_HOPS,
    Evidence,
    GraphPath,
    PathStep,
    TraversalDirection,
)
from tracegraph.core.ports import DocumentRepository, GraphRepository
from tracegraph.retrieval.traversal import MAX_PATHS, traverse_paths


_RELATION_HINTS = {
    "BELONGS_TO": ("分类", "属于"),
    "HAS_SYMPTOM": ("症状", "表现", "征兆"),
    "ACCOMPANIES": ("并发", "伴随"),
    "TREATED_BY": ("科室", "挂号", "看什么科", "什么科"),
    "USES_TREATMENT": ("治疗", "疗法"),
    "REQUIRES_CHECK": ("检查", "检验"),
    "RECOMMENDS_DRUG": ("药", "用药", "药物"),
    "COMMONLY_USES_DRUG": ("常用药", "药物"),
    "SHOULD_EAT": ("宜吃", "吃什么", "饮食"),
    "SHOULD_NOT_EAT": ("忌口", "不能吃", "不宜吃"),
    "RECOMMENDS_RECIPE": ("食谱", "菜谱"),
}

# 跳数衰减：路径分数 = 各步分数均值 × _HOP_DECAY ** (跳数 - 1)。
# 1 跳时指数为 0，因此单跳分数与多跳改造前完全一致；
# 2 跳最高为 0.725 × 0.6 = 0.435，稳定低于任何 1 跳。
_HOP_DECAY = 0.6

_BASE_STEP_SCORE = 0.45
_OUTGOING_RANK_BONUS = 0.35
_INCOMING_RANK_BONUS = 0.2
_INTENT_BONUS = 0.2


class GraphRetriever:
    name = "graph"

    def __init__(
        self,
        document_repository: DocumentRepository,
        graph_repository: GraphRepository,
    ) -> None:
        self.documents = document_repository
        self.graph = graph_repository

    def retrieve(
        self, query: str, limit: int = 5, max_hops: int = DEFAULT_MAX_HOPS
    ) -> tuple[Evidence, ...]:
        if not query.strip():
            raise ValueError("query 不能为空")
        if limit < 1:
            raise ValueError("limit 必须大于 0")
        if max_hops < 1 or max_hops > MAX_HOPS:
            raise ValueError(f"max_hops 必须在 1 到 {MAX_HOPS} 之间")
        entities = self.graph.search_entities(query, limit=5)
        if not entities:
            return ()
        # 起点只取 top-1，与单跳版本一致；只有起点带 rank 加成。
        entity_ranks = {entities[0].id: 0}
        intent_types = _intent_relation_types(query.casefold())
        paths = traverse_paths(
            self.graph, entities[0], max_hops=max_hops, limit=MAX_PATHS
        )
        ranked = sorted(
            paths,
            key=lambda path: (
                -_path_score(path, entity_ranks, intent_types),
                _path_order(path),
            ),
        )
        # 按跳数分桶收集：若只用一个「多跳」桶，二跳会凭更高的分数占满它，
        # 三跳永远进不来。分桶后每个跳数层级各自保有候选。
        buckets: dict[int, list[Evidence]] = {}
        seen_chunks: set[str] = set()
        for path in ranked:
            # 只取最后一步的关系：证据落在「本次新到达的那一跳」上，
            # 1 跳时即唯一那步，与单跳版本产出的 Evidence 完全相同。
            bucket = buckets.setdefault(path.hop_count, [])
            if len(bucket) >= limit:
                continue
            relation = path.steps[-1].relation
            score = _path_score(path, entity_ranks, intent_types)
            for chunk_id in relation.evidence_chunk_ids:
                if chunk_id in seen_chunks:
                    continue
                chunk = self.documents.get_chunk(chunk_id)
                if chunk is None:
                    continue
                document = self.documents.get_document(chunk.document_id)
                version = self.documents.get_version(chunk.document_version_id)
                if document is None or version is None:
                    continue
                bucket.append(
                    Evidence(
                        id=_stable_evidence_id(relation.id, chunk.id),
                        content=chunk.content,
                        document_id=document.id,
                        document_version=version.id,
                        source_name=document.source_name,
                        locator=chunk.locator,
                        chunk_id=chunk.id,
                        retrieval_method="graph",
                        retrieval_score=score,
                        graph_path=path,
                    )
                )
                seen_chunks.add(chunk_id)
        return _merge_with_hop_quota(buckets.pop(1, []), buckets, limit)


def _merge_with_hop_quota(
    facts: list[Evidence], derived_by_hop: dict[int, list[Evidence]], limit: int
) -> tuple[Evidence, ...]:
    """把一跳事实与多跳关联合并到同一个 limit 内，并给多跳保留名额。

    跳数衰减保证一跳分数恒高于任何多跳，不预留名额时多跳永远进不了 top-k，
    网页上的「二跳/三跳」就等于没有效果；名额再按跳数层级均分，否则二跳会
    凭更高的分数把三跳全部挤掉。max_hops=1 时没有多跳，退化为取前 limit 条。
    """
    budget = limit // 2
    if not derived_by_hop or budget < 1:
        return tuple(facts[:limit])
    levels = sorted(derived_by_hop)
    share = max(1, budget // len(levels))
    picked = [evidence for hop in levels for evidence in derived_by_hop[hop][:share]]
    if len(picked) < budget:
        chosen = {evidence.id for evidence in picked}
        picked.extend(
            evidence
            for hop in levels
            for evidence in derived_by_hop[hop]
            if evidence.id not in chosen
        )
    picked = picked[:budget]
    head = facts[: limit - len(picked)]
    return (*head, *picked)


def _path_score(
    path: GraphPath, entity_ranks: dict[str, int], intent_types: set[str]
) -> float:
    step_scores = [_step_score(step, entity_ranks, intent_types) for step in path.steps]
    mean_score = sum(step_scores) / len(step_scores)
    return round(min(mean_score * _HOP_DECAY ** (path.hop_count - 1), 1.0), 6)


def _step_score(
    step: PathStep, entity_ranks: dict[str, int], intent_types: set[str]
) -> float:
    """单跳打分；起点为 top-1 时与单跳版本的 _relation_score 逐位等价。"""
    score = _BASE_STEP_SCORE
    rank = entity_ranks.get(step.origin_entity_id)
    if rank is not None:
        bonus = (
            _OUTGOING_RANK_BONUS
            if step.direction is TraversalDirection.OUTGOING
            else _INCOMING_RANK_BONUS
        )
        score += bonus / (rank + 1)
    if step.relation.type in intent_types:
        score += _INTENT_BONUS
    return round(min(score, 1.0), 6)


def _path_order(path: GraphPath) -> tuple[tuple[str, ...], int]:
    """同分时的确定性次序；1 跳下退化为「按关系 id」，与单跳版本一致。"""
    return (tuple(step.relation.id for step in path.steps), path.hop_count)


def _intent_relation_types(query: str) -> set[str]:
    if any(hint in query for hint in ("不宜吃", "不能吃", "忌口", "避免吃")):
        return {"SHOULD_NOT_EAT"}
    return {
        relation_type
        for relation_type, hints in _RELATION_HINTS.items()
        if any(hint in query for hint in hints)
    }


def _stable_evidence_id(relation_id: str, chunk_id: str) -> str:
    raw = f"graph\x1f{relation_id}\x1f{chunk_id}".encode("utf-8")
    return f"ev-{hashlib.sha256(raw).hexdigest()[:20]}"
