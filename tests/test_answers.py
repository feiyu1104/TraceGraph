import pytest

from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    DEFAULT_WORKSPACE_ID,
    AnswerStatus,
    Claim,
    Entity,
    Evidence,
    GraphPath,
    PathStep,
    Relation,
    TraversalDirection,
)
from tracegraph.domains.medical.adapter import MedicalDomainAdapter
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.generation.models import (
    ModelEntry,
    ModelRegistry,
    UnknownGeneratorError,
    UnavailableGeneratorError,
)
from tracegraph.generation.providers import (
    ExtractiveAnswerGenerator,
    GeneratedAnswer,
    GenerationNetworkError,
    GenerationResponseError,
)
from tracegraph.generation.service import AnswerService, compose_text
from tracegraph.retrieval.graph import GraphRetriever
from tracegraph.retrieval.hybrid import HybridRetriever
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository


def _ingest(documents: InMemoryDocumentRepository, sources: dict[str, str]) -> dict[str, str]:
    ingestion = TextIngestionService(documents)
    return {
        key: ingestion.ingest_text(name, content).chunks[0].id
        for key, (name, content) in sources.items()
    }


def _conflict_stack(
    second_owner: str = "d1", second_type: str = "SHOULD_NOT_EAT"
):
    """杏仁：d1 同时宜吃/忌吃（默认），或 d1 宜吃而 d2 忌吃。"""
    documents = InMemoryDocumentRepository()
    chunks = _ingest(
        documents,
        {
            "eat": ("dutmed-饮食.md", "杏仁可以作为日常膳食的一部分。"),
            "avoid": ("dutmed-忌口.md", "杏仁不宜在此情况下食用。"),
        },
    )
    graph = InMemoryGraphRepository()
    graph.upsert_entity(Entity("d1", "绝经与心血管疾病", "Disease"))
    graph.upsert_entity(Entity("d2", "高血压", "Disease"))
    graph.upsert_entity(Entity("f1", "杏仁", "Food"))
    graph.upsert_relation(Relation("p1", "d1", "f1", "SHOULD_EAT", (chunks["eat"],)))
    graph.upsert_relation(
        Relation("n1", second_owner, "f1", second_type, (chunks["avoid"],))
    )
    retriever = GraphRetriever(documents, graph)
    return retriever, graph


def _association_stack():
    """百日咳 → 琥乙红霉素片 ← 小儿支原体肺炎。"""
    documents = InMemoryDocumentRepository()
    chunks = _ingest(
        documents,
        {
            "r1": ("dutmed-百日咳-推荐药物.md", "百日咳的推荐药物包括琥乙红霉素片。"),
            "r2": ("dutmed-小儿支原体肺炎-推荐药物.md", "小儿支原体肺炎的推荐药物包括琥乙红霉素片。"),
        },
    )
    graph = InMemoryGraphRepository()
    graph.upsert_entity(Entity("d1", "百日咳", "Disease"))
    graph.upsert_entity(Entity("d2", "小儿支原体肺炎", "Disease"))
    graph.upsert_entity(Entity("m1", "琥乙红霉素片", "Drug"))
    graph.upsert_relation(Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", (chunks["r1"],)))
    graph.upsert_relation(Relation("r2", "d2", "m1", "RECOMMENDS_DRUG", (chunks["r2"],)))
    return GraphRetriever(documents, graph)


def _derived_only_evidence() -> Evidence:
    """一条孤立的二跳证据，没有任何一跳原文事实。"""
    start = Entity("d1", "百日咳", "Disease")
    drug = Entity("m1", "琥乙红霉素片", "Drug")
    other = Entity("d2", "小儿支原体肺炎", "Disease")
    path = GraphPath(
        start=start,
        steps=(
            PathStep(
                relation=Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", ("c1",)),
                direction=TraversalDirection.OUTGOING,
                target=drug,
            ),
            PathStep(
                relation=Relation("r2", "d2", "m1", "RECOMMENDS_DRUG", ("c2",)),
                direction=TraversalDirection.INCOMING,
                target=other,
            ),
        ),
    )
    return Evidence(
        id="ev-derived",
        content="小儿支原体肺炎的推荐药物包括琥乙红霉素片。",
        document_id="doc-1",
        document_version="ver-1",
        source_name="dutmed-小儿支原体肺炎-推荐药物.md",
        locator="推荐药物",
        chunk_id="c2",
        retrieval_method="graph",
        retrieval_score=0.495,
        graph_path=path,
    )


class _FixedRetriever:
    """返回预先构造的证据，用于制造真实检索难以稳定复现的边界。"""

    name = "fixed"

    def __init__(self, evidences: tuple[Evidence, ...]) -> None:
        self.evidences = evidences

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        max_hops: int = DEFAULT_MAX_HOPS,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> tuple[Evidence, ...]:
        return self.evidences


def test_opposing_relations_from_one_subject_are_conflicting() -> None:
    retriever, graph = _conflict_stack()
    service = AnswerService(retriever, MedicalDomainAdapter(), graph_repository=graph)

    answer = service.answer("绝经与心血管疾病宜吃杏仁吗", max_hops=1)

    assert answer.status is AnswerStatus.CONFLICTING_EVIDENCE
    assert answer.claims == ()
    assert answer.evidences


def test_opposing_relations_across_subjects_are_not_conflicting() -> None:
    # 同一个食物在不同疾病下建议相反是正常的，不构成冲突。
    retriever, graph = _conflict_stack(second_owner="d2")
    service = AnswerService(retriever, MedicalDomainAdapter(), graph_repository=graph)

    answer = service.answer("绝经与心血管疾病宜吃杏仁吗", max_hops=1)

    assert answer.status is AnswerStatus.ANSWERED


def test_conflict_is_detected_without_a_graph_repository_dependency() -> None:
    # 未注入图仓储时无法查冲突，退化为正常作答而不是报错。
    retriever, _ = _conflict_stack()
    service = AnswerService(retriever, MedicalDomainAdapter())

    assert service.answer("绝经与心血管疾病宜吃杏仁吗", max_hops=1).status is (
        AnswerStatus.ANSWERED
    )


def test_derived_associations_never_become_claims() -> None:
    retriever = _association_stack()
    service = AnswerService(retriever, MedicalDomainAdapter())

    answer = service.answer("百日咳用什么药", limit=5, max_hops=2)

    assert answer.status is AnswerStatus.ANSWERED
    assert len(answer.derived_associations) == 1
    assert answer.metrics["derived_association_count"] == 1
    assert answer.metrics["max_hops"] == 2

    claimed_ids = {
        evidence_id for claim in answer.claims for evidence_id in claim.evidence_ids
    }
    derived_ids = {
        evidence_id
        for association in answer.derived_associations
        for evidence_id in association.evidence_ids
    }
    assert claimed_ids.isdisjoint(derived_ids)
    assert {claim.text for claim in answer.claims}.isdisjoint(
        {association.text for association in answer.derived_associations}
    )
    # 推导区块的文案必须点明来源不是本实体。
    assert "小儿支原体肺炎" in answer.derived_associations[0].text
    assert "百日咳" in answer.derived_associations[0].text


def test_no_single_hop_facts_means_insufficient_evidence() -> None:
    service = AnswerService(
        _FixedRetriever((_derived_only_evidence(),)), MedicalDomainAdapter()
    )

    answer = service.answer("百日咳用什么药", max_hops=2)

    # 只有多跳关联时不做跨节点推测。
    assert answer.status is AnswerStatus.INSUFFICIENT_EVIDENCE
    assert answer.claims == ()
    assert answer.derived_associations == ()
    assert answer.evidences == ()


class _StaticGenerator:
    """固定产出一条主张的替身模型，用来验证「选了谁」这件事本身。

    `name` 刻意与注册表 ID 不同（和真实的 OpenAI-compatible 生成器一样：
    类名恒为 `openai-compatible`，ID 却由配置决定），这样 metrics 报的是
    ID 而不是类名这件事才会被真正测到。
    """

    name = "openai-compatible"

    def __init__(self, label: str, model: str) -> None:
        self.label = label
        self.model = model

    def generate(self, question: str, evidences: tuple[Evidence, ...]):
        return GeneratedAnswer(
            claims=(Claim(text=f"{self.label} 的主张", evidence_ids=(evidences[0].id,)),)
        )


def _two_model_registry() -> ModelRegistry:
    return ModelRegistry(
        "extractive",
        (
            ModelEntry(id="extractive", label="离线摘录", kind="extractive"),
            ModelEntry(id="main", label="主模型", kind="openai-compatible", model="m-1"),
            ModelEntry(
                id="broken",
                label="坏掉的模型",
                kind="openai-compatible",
                available=False,
                reason="环境变量 MISSING_KEY 未设置",
            ),
        ),
        {
            "extractive": ExtractiveAnswerGenerator(),
            "main": _StaticGenerator("main", "m-1"),
        },
    )


class _FailingGenerator:
    """模拟模型不可用，用来验证「不静默降级」与「显式降级」。"""

    name = "openai-compatible"
    model = "failing-model"

    def __init__(self, error: Exception) -> None:
        self.error = error

    def generate(self, question: str, evidences: tuple[Evidence, ...]):
        raise self.error


def test_successful_generation_is_not_marked_degraded() -> None:
    service = AnswerService(_association_stack(), MedicalDomainAdapter())

    answer = service.answer("百日咳用什么药", max_hops=2)

    assert answer.metrics["generator"] == "extractive"
    assert answer.metrics["generation_degraded"] is False
    assert not any("降级" in warning for warning in answer.warnings)


def test_model_failure_without_fallback_is_an_explicit_error() -> None:
    service = AnswerService(
        _association_stack(),
        MedicalDomainAdapter(),
        _FailingGenerator(GenerationNetworkError("连不上")),
    )

    answer = service.answer("百日咳用什么药", max_hops=1)

    assert answer.status is AnswerStatus.SYSTEM_ERROR
    assert answer.error_code == "generation_network_error"
    assert answer.claims == ()
    assert answer.evidences
    # 失败响应的 metrics 与成功响应同形，前端不必为失败态单独分支。
    assert answer.metrics["generator"] == "openai-compatible"
    assert answer.metrics["generation_degraded"] is False
    # 上游细节只以 error_code 暴露，不进入正文。
    assert "连不上" not in (answer.text or "")


def test_response_failure_keeps_its_own_error_code() -> None:
    service = AnswerService(
        _association_stack(),
        MedicalDomainAdapter(),
        _FailingGenerator(GenerationResponseError("不是 JSON")),
    )

    answer = service.answer("百日咳用什么药", max_hops=1)

    assert answer.status is AnswerStatus.SYSTEM_ERROR
    assert answer.error_code == "generation_response_error"


def test_failed_request_reports_the_model_it_tried() -> None:
    registry = ModelRegistry(
        "extractive",
        (
            ModelEntry(id="extractive", label="离线摘录", kind="extractive"),
            ModelEntry(id="main", label="主模型", kind="openai-compatible", model="m-1"),
        ),
        {
            "extractive": ExtractiveAnswerGenerator(),
            "main": _FailingGenerator(GenerationNetworkError("连不上")),
        },
    )
    service = AnswerService(
        _association_stack(), MedicalDomainAdapter(), registry=registry
    )

    answer = service.answer("百日咳用什么药", max_hops=1, generator_id="main")

    assert answer.error_code == "generation_network_error"
    # 失败态也要报出「本来要用哪一个」，且同样用注册表 ID。
    assert answer.metrics["generator"] == "main"
    assert answer.metrics["model"] == "failing-model"


def test_unexpected_generator_failure_is_contained() -> None:
    service = AnswerService(
        _association_stack(),
        MedicalDomainAdapter(),
        _FailingGenerator(RuntimeError("内部炸了")),
    )

    answer = service.answer("百日咳用什么药", max_hops=1)

    assert answer.status is AnswerStatus.SYSTEM_ERROR
    assert answer.error_code == "internal_error"
    assert "内部炸了" not in (answer.text or "")


def test_explicit_fallback_degrades_visibly() -> None:
    service = AnswerService(
        _association_stack(),
        MedicalDomainAdapter(),
        _FailingGenerator(GenerationNetworkError("连不上")),
        fallback_generator=ExtractiveAnswerGenerator(),
    )

    answer = service.answer("百日咳用什么药", max_hops=2)

    assert answer.status is AnswerStatus.ANSWERED
    assert answer.metrics["generator"] == "extractive"
    # 降级不篡改 requested_generator：它记录的仍是这次请求本来要用的模型。
    assert answer.metrics["requested_generator"] == "openai-compatible"
    assert answer.metrics["generation_degraded"] is True
    assert answer.claims
    # 降级必须写在最显眼的警告位置上。
    assert "降级" in answer.warnings[0]
    assert "generation_network_error" in answer.warnings[0]


def test_keyword_only_evidence_survives_the_evidence_floor() -> None:
    """下限必须是相对的：融合分里「只被关键词命中」最高也只有 0.2。

    绝对下限会把这类证据整条挡掉，而通过网页上传的普通文档只走关键词
    通路 —— 那正是「上传后立刻能提问」这条承诺失效的地方。
    """
    documents = InMemoryDocumentRepository()
    _ingest(documents, {"notice": ("科室须知.txt", "呼吸内科负责处理咳嗽与呼吸困难。")})
    retriever = HybridRetriever(
        (KeywordRetriever(documents), GraphRetriever(documents, InMemoryGraphRepository()))
    )
    service = AnswerService(retriever, MedicalDomainAdapter())

    answer = service.answer("呼吸内科负责处理什么", max_hops=1)

    assert answer.status is AnswerStatus.ANSWERED
    assert answer.claims
    # 融合分确实低于曾经的绝对下限 0.25，这个用例才有意义。
    assert answer.evidences[0].retrieval_score < 0.25
    assert answer.evidences[0].graph_path is None


def test_answer_text_comes_only_from_claims() -> None:
    service = AnswerService(_association_stack(), MedicalDomainAdapter())

    answer = service.answer("百日咳用什么药", limit=5, max_hops=2)

    assert answer.claims
    assert answer.derived_associations
    # 正文逐行等于 claims 的渲染结果 —— 推导关联没有第二次出场的机会。
    assert answer.text == compose_text(answer.claims)
    for association in answer.derived_associations:
        assert association.text not in answer.text


def test_each_request_picks_its_own_generator() -> None:
    service = AnswerService(
        _association_stack(), MedicalDomainAdapter(), registry=_two_model_registry()
    )

    chosen = service.answer("百日咳用什么药", max_hops=1, generator_id="main")
    defaulted = service.answer("百日咳用什么药", max_hops=1)

    # 报的是注册表 ID，不是生成器类的 name —— 界面按 ID 反查显示名称。
    assert chosen.metrics["generator"] == "main"
    assert chosen.metrics["requested_generator"] == "main"
    assert chosen.metrics["model"] == "m-1"
    assert "main 的主张" in chosen.text

    assert defaulted.metrics["generator"] == "extractive"
    # 未显式选择时，requested_generator 记录的是注册表的默认模型 ID，不是空串。
    assert defaulted.metrics["requested_generator"] == "extractive"
    assert defaulted.metrics["model"] == ""


def test_selecting_a_model_leaves_no_trace_on_the_next_request() -> None:
    # 模型选择必须是每次请求的事：不存在会被并发请求互相覆盖的全局状态。
    service = AnswerService(
        _association_stack(), MedicalDomainAdapter(), registry=_two_model_registry()
    )

    service.answer("百日咳用什么药", max_hops=1, generator_id="main")

    assert service.registry.default_id == "extractive"
    assert service.generator.name == "extractive"
    assert (
        service.answer("百日咳用什么药", max_hops=1).metrics["generator"]
        == "extractive"
    )


def test_unknown_generator_id_is_rejected() -> None:
    service = AnswerService(_association_stack(), MedicalDomainAdapter())

    with pytest.raises(UnknownGeneratorError):
        service.answer("百日咳用什么药", max_hops=1, generator_id="nope")


def test_unavailable_generator_is_rejected_without_falling_back() -> None:
    service = AnswerService(
        _association_stack(), MedicalDomainAdapter(), registry=_two_model_registry()
    )

    with pytest.raises(UnavailableGeneratorError) as error:
        service.answer("百日咳用什么药", max_hops=1, generator_id="broken")

    assert error.value.error_code == "generator_unavailable"


def test_unknown_generator_id_fails_before_any_retrieval() -> None:
    seen: list[str] = []

    class _RecordingRetriever:
        name = "recording"

        def retrieve(self, query, limit=5, max_hops=DEFAULT_MAX_HOPS, workspace_id=None):
            seen.append(query)
            return ()

    service = AnswerService(_RecordingRetriever(), MedicalDomainAdapter())

    # 未知模型要立刻失败，不该先白跑一遍检索。
    with pytest.raises(UnknownGeneratorError):
        service.answer("百日咳用什么药", max_hops=3, generator_id="nope")

    assert seen == []


def test_max_hops_is_forwarded_to_the_retriever() -> None:
    seen = []

    class _RecordingRetriever:
        name = "recording"

        def retrieve(self, query, limit=5, max_hops=DEFAULT_MAX_HOPS, workspace_id=None):
            seen.append(max_hops)
            return ()

    AnswerService(_RecordingRetriever(), MedicalDomainAdapter()).answer(
        "百日咳用什么药", max_hops=3
    )

    assert seen == [3]
