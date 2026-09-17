import hashlib

import pytest

from tracegraph.core.contracts import Entity, Relation
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.retrieval.graph import GraphRetriever
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository


_ENTITIES = (
    Entity("d1", "百日咳", "Disease"),
    Entity("d2", "小儿支原体肺炎", "Disease"),
    Entity("d3", "急性肺炎", "Disease"),
    Entity("m1", "琥乙红霉素片", "Drug"),
    Entity("s1", "阵发性咳嗽", "Symptom"),
)

# 关系 id -> 该关系唯一证据所在文档的正文
_DOCUMENTS = {
    "r1": ("dutmed-百日咳-推荐药物.md", "百日咳的推荐药物包括琥乙红霉素片。"),
    "r2": ("dutmed-小儿支原体肺炎-推荐药物.md", "小儿支原体肺炎的推荐药物包括琥乙红霉素片。"),
    "r3": ("dutmed-百日咳-症状.md", "百日咳的典型症状为阵发性咳嗽。"),
    "r4": ("dutmed-急性肺炎-症状.md", "急性肺炎的典型症状为阵发性咳嗽。"),
}

_EDGES = (
    ("r1", "d1", "m1", "RECOMMENDS_DRUG"),
    ("r2", "d2", "m1", "RECOMMENDS_DRUG"),
    ("r3", "d1", "s1", "HAS_SYMPTOM"),
    ("r4", "d3", "s1", "HAS_SYMPTOM"),
)

_QUERY = "百日咳用什么药"


def _build() -> tuple[InMemoryDocumentRepository, InMemoryGraphRepository, dict[str, str]]:
    """返回 (文档仓储, 图仓储, 关系 id -> 证据 chunk id)。"""
    documents = InMemoryDocumentRepository()
    ingestion = TextIngestionService(documents)
    chunk_ids = {}
    for relation_id, (source_name, content) in _DOCUMENTS.items():
        result = ingestion.ingest_text(source_name, content)
        chunk_ids[relation_id] = result.chunks[0].id

    graph = InMemoryGraphRepository()
    for entity in _ENTITIES:
        graph.upsert_entity(entity)
    for relation_id, source, target, relation_type in _EDGES:
        graph.upsert_relation(
            Relation(relation_id, source, target, relation_type, (chunk_ids[relation_id],))
        )
    return documents, graph, chunk_ids


def _v0_1_evidence_id(relation_id: str, chunk_id: str) -> str:
    """v0.1 的 Evidence.id 生成式，逐字抄来作为回归护栏。"""
    raw = f"graph\x1f{relation_id}\x1f{chunk_id}".encode("utf-8")
    return f"ev-{hashlib.sha256(raw).hexdigest()[:20]}"


def test_single_hop_scores_match_the_v0_1_formula() -> None:
    documents, graph, chunk_ids = _build()

    evidences = GraphRetriever(documents, graph).retrieve(_QUERY, limit=5, max_hops=1)

    scores = {evidence.chunk_id: evidence.retrieval_score for evidence in evidences}
    # 0.45 基分 + 0.35 出边起点 rank0 加成 + 0.2 意图（RECOMMENDS_DRUG）加成
    assert scores[chunk_ids["r1"]] == 1.0
    # 0.45 基分 + 0.35 出边起点 rank0 加成；HAS_SYMPTOM 不在本查询意图内
    assert scores[chunk_ids["r3"]] == 0.8


def test_single_hop_evidence_ids_are_unchanged() -> None:
    documents, graph, chunk_ids = _build()

    evidences = GraphRetriever(documents, graph).retrieve(_QUERY, limit=5, max_hops=1)

    assert {evidence.id for evidence in evidences} == {
        _v0_1_evidence_id(relation_id, chunk_ids[relation_id]) for relation_id in ("r1", "r3")
    }


def test_two_hop_scores_stay_below_every_single_hop_score() -> None:
    documents, graph, _ = _build()

    evidences = GraphRetriever(documents, graph).retrieve(_QUERY, limit=10, max_hops=2)

    single = [e.retrieval_score for e in evidences if e.graph_path.hop_count == 1]
    derived = [e.retrieval_score for e in evidences if e.graph_path.hop_count == 2]
    assert single and derived
    assert max(derived) < min(single)


def test_multi_hop_associations_survive_the_hop_decay() -> None:
    documents, graph, _ = _build()

    # 跳数衰减让 1 跳恒高于多跳；不预留名额时多跳会被挤出 limit。
    evidences = GraphRetriever(documents, graph).retrieve(_QUERY, limit=3, max_hops=2)

    assert {e.graph_path.hop_count for e in evidences} == {1, 2}


def test_single_hop_evidence_is_still_returned_without_multi_hop() -> None:
    documents, graph, _ = _build()

    evidences = GraphRetriever(documents, graph).retrieve(_QUERY, limit=1, max_hops=2)

    # limit=1 时没有多跳名额，不能把唯一的位置让给推导关联。
    assert len(evidences) == 1
    assert evidences[0].graph_path.hop_count == 1


@pytest.mark.parametrize("limit", (1, 2, 3, 5))
def test_limit_contract_is_preserved(limit: int) -> None:
    documents, graph, _ = _build()

    evidences = GraphRetriever(documents, graph).retrieve(_QUERY, limit=limit, max_hops=3)

    assert len(evidences) <= limit


def test_each_chunk_is_used_as_evidence_at_most_once() -> None:
    documents, graph, _build_ids = _build()

    evidences = GraphRetriever(documents, graph).retrieve(_QUERY, limit=10, max_hops=3)

    chunk_ids = [evidence.chunk_id for evidence in evidences]
    assert len(chunk_ids) == len(set(chunk_ids))


def test_retrieval_is_deterministic() -> None:
    documents, graph, _ = _build()
    retriever = GraphRetriever(documents, graph)

    first = retriever.retrieve(_QUERY, limit=5, max_hops=3)
    second = retriever.retrieve(_QUERY, limit=5, max_hops=3)

    assert [e.id for e in first] == [e.id for e in second]


def test_unknown_entity_yields_no_evidence() -> None:
    documents, graph, _ = _build()

    assert GraphRetriever(documents, graph).retrieve("阑尾切除术式", max_hops=2) == ()


@pytest.mark.parametrize("max_hops", (0, 4))
def test_max_hops_out_of_range_is_rejected(max_hops: int) -> None:
    documents, graph, _ = _build()

    with pytest.raises(ValueError, match="max_hops"):
        GraphRetriever(documents, graph).retrieve(_QUERY, max_hops=max_hops)
