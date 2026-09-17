"""候选审核的状态机、内容修正与冲突判定。"""

import pytest

from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    CandidateStatus,
    Workspace,
)
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.extraction.service import ExtractionService
from tracegraph.generation.models import ModelEntry, ModelRegistry
from tracegraph.generation.providers import ExtractiveAnswerGenerator
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.review.service import (
    CandidateContentConflictError,
    CandidateNotFoundError,
    CandidateReviewService,
    CandidateWorkspaceMismatchError,
    IllegalCandidateTransitionError,
)
from tracegraph.storage.candidates import InMemoryCandidateRepository
from tracegraph.storage.memory import InMemoryDocumentRepository

DUTMED_MARKDOWN = """# 百日咳

## 简介

百日咳是由百日咳鲍特菌引起的急性呼吸道传染病。

## 症状

咳嗽、低热

## 推荐药物

琥乙红霉素片
"""

# 一份普通资料：没有可确定抽取的章节，因此抽不出任何候选。
PLAIN_MARKDOWN = "# 会议记录\n\n下周三讨论排期。\n"

_CREATED_AT = "2026-01-01T00:00:00+00:00"


def _registry() -> ModelRegistry:
    entry = ModelEntry(id="extractive", label="extractive", kind="extractive", model="t")
    return ModelRegistry("extractive", (entry,), {"extractive": ExtractiveAnswerGenerator()})


class _Stack:
    """两个 Workspace + 各一份文档 + 各一次抽取，审核用例的公共起点。

    两个 Workspace 共用同一套仓储：跨 Workspace 的拒绝必须在「候选确实存在、
    只是不归你」的条件下验证，各自开一套仓储只会撞上「查无此候选」。
    """

    OTHER = "ws-b"

    def __init__(self) -> None:
        self.workspace_id = DEFAULT_WORKSPACE_ID
        self.documents = InMemoryDocumentRepository()
        self.candidates = InMemoryCandidateRepository()
        self.documents.save_workspace(
            Workspace(
                id=self.OTHER,
                name=self.OTHER,
                adapter_id="medical",
                created_at=_CREATED_AT,
            )
        )
        self.review = CandidateReviewService(
            self.documents, self.candidates, build_default_adapter_registry()
        )
        self.run = self.extract(self.workspace_id)
        self.entities = self.candidates.list_entities(self.run.id)
        self.relations = self.candidates.list_relations(self.run.id)
        self.other_run = self.extract(self.workspace_id, source_name="dutmed-百日咳-b.md")
        self.foreign_run = self.extract(self.OTHER)

    def extract(
        self,
        workspace_id: str,
        source_name: str = "dutmed-百日咳.md",
        content: str = DUTMED_MARKDOWN,
    ) -> object:
        result = TextIngestionService(self.documents).ingest_text(
            source_name, content, workspace_id
        )
        return ExtractionService(
            self.documents,
            build_default_adapter_registry(),
            self.candidates,
            _registry(),
        ).start(
            workspace_id,
            document_id=result.document.id,
            model_id="extractive",
        )

    def entity(self, name: str) -> object:
        return next(item for item in self.entities if item.name == name)

    def foreign_entity(self, name: str) -> object:
        return next(
            item
            for item in self.candidates.list_entities(self.foreign_run.id)
            if item.name == name
        )

    def other_run_entity(self, name: str) -> object:
        """同一个 Workspace 里另一次抽取产出的同名实体。"""
        return next(
            item
            for item in self.candidates.list_entities(self.other_run.id)
            if item.name == name
        )

    def relation(self, index: int = 0) -> object:
        return self.relations[index]

    def relations_of(self, entity_id: str) -> tuple[object, ...]:
        return tuple(
            relation
            for relation in self.relations
            if entity_id in (relation.source_entity_id, relation.target_entity_id)
        )

    def status_of(self, candidate_id: str) -> CandidateStatus:
        stored = self.candidates.get_entity(candidate_id) or self.candidates.get_relation(
            candidate_id
        )
        assert stored is not None
        return stored.status


def _stack() -> _Stack:
    return _Stack()


def test_pending_candidate_can_be_approved_then_returned_to_pending() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")

    approved = stack.review.review_entity(
        stack.workspace_id, entity.id, status=CandidateStatus.APPROVED
    )
    assert approved.status is CandidateStatus.APPROVED
    assert stack.status_of(entity.id) is CandidateStatus.APPROVED

    # approved → pending 是合法的回退。
    back = stack.review.review_entity(
        stack.workspace_id, entity.id, status=CandidateStatus.PENDING
    )
    assert back.status is CandidateStatus.PENDING
    assert stack.status_of(entity.id) is CandidateStatus.PENDING


def test_pending_candidate_can_be_rejected_then_returned_to_pending() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")
    relations = stack.relations_of(entity.id)

    def set_status(status: CandidateStatus) -> None:
        stack.review.review_batch(
            stack.workspace_id,
            entity_ids=(entity.id,),
            relation_ids=tuple(relation.id for relation in relations),
            status=status,
        )

    set_status(CandidateStatus.REJECTED)
    assert stack.status_of(entity.id) is CandidateStatus.REJECTED

    set_status(CandidateStatus.PENDING)
    assert stack.status_of(entity.id) is CandidateStatus.PENDING

    # rejected 不能直接跳到 approved，必须先回 pending。
    set_status(CandidateStatus.REJECTED)
    with pytest.raises(IllegalCandidateTransitionError):
        stack.review.review_entity(
            stack.workspace_id, entity.id, status=CandidateStatus.APPROVED
        )


def test_illegal_transition_leaves_the_candidate_untouched() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")
    stack.review.review_entity(
        stack.workspace_id, entity.id, status=CandidateStatus.APPROVED
    )

    with pytest.raises(IllegalCandidateTransitionError):
        stack.review.review_entity(
            stack.workspace_id, entity.id, status=CandidateStatus.REJECTED
        )

    assert stack.status_of(entity.id) is CandidateStatus.APPROVED


def test_review_rejects_a_candidate_from_another_workspace() -> None:
    stack = _stack()
    own = stack.entity("咳嗽")
    foreign = stack.foreign_entity("咳嗽")

    # 拿自己 Workspace 的 ID 去别的 Workspace 里审。
    with pytest.raises(CandidateWorkspaceMismatchError):
        stack.review.review_entity(
            stack.OTHER, own.id, status=CandidateStatus.REJECTED
        )
    # 拿别的 Workspace 的 ID 到自己这里审。
    with pytest.raises(CandidateWorkspaceMismatchError):
        stack.review.review_entity(
            stack.workspace_id, foreign.id, status=CandidateStatus.REJECTED
        )

    # 两次都必须在写之前失败。
    assert stack.status_of(own.id) is CandidateStatus.PENDING
    assert stack.status_of(foreign.id) is CandidateStatus.PENDING


def test_review_reports_a_missing_candidate() -> None:
    stack = _stack()
    with pytest.raises(CandidateNotFoundError):
        stack.review.review_entity(
            stack.workspace_id, "cand-missing", status=CandidateStatus.APPROVED
        )


def test_pending_entity_content_can_be_corrected() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")

    edited = stack.review.review_entity(
        stack.workspace_id, entity.id, name=" 百日咳（疫咳）。 ", entity_type="Disease"
    )

    assert edited.name == "百日咳（疫咳）。"
    # 规范化跟着重算：去掉首尾标点后折叠大小写，收尾的「）」与「。」都在
    # 标点集合里，所以一起被剥掉。
    assert edited.normalized_name == "百日咳（疫咳"
    assert stack.status_of(entity.id) is CandidateStatus.PENDING


def test_content_edit_is_rejected_on_a_non_pending_candidate() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")
    stack.review.review_entity(
        stack.workspace_id, entity.id, status=CandidateStatus.APPROVED
    )

    with pytest.raises(IllegalCandidateTransitionError):
        stack.review.review_entity(stack.workspace_id, entity.id, name="改个名")

    assert stack.entity("百日咳").name == "百日咳"


def test_entity_type_outside_the_adapter_vocabulary_is_rejected() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")

    with pytest.raises(CandidateContentConflictError):
        stack.review.review_entity(
            stack.workspace_id, entity.id, entity_type="不存在的类型"
        )

    assert stack.status_of(entity.id) is CandidateStatus.PENDING


def test_renaming_onto_an_existing_candidate_is_rejected() -> None:
    stack = _stack()
    target = stack.entity("低热")

    # 咳嗽与低热都是 Symptom：改名之后就是同一次抽取里的重复候选。
    with pytest.raises(CandidateContentConflictError):
        stack.review.review_entity(stack.workspace_id, target.id, name="咳嗽")

    assert stack.entity("低热").name == "低热"


def test_relation_endpoints_must_stay_in_the_same_run() -> None:
    stack = _stack()
    relation = stack.relation()
    other_run = stack.other_run_entity("咳嗽")

    with pytest.raises(CandidateContentConflictError):
        stack.review.review_relation(
            stack.workspace_id, relation.id, target_entity_id=other_run.id
        )

    assert stack.candidates.get_relation(relation.id).target_entity_id == (
        relation.target_entity_id
    )


def test_relation_endpoint_from_another_workspace_is_rejected() -> None:
    stack = _stack()
    relation = stack.relation()
    foreign = stack.foreign_entity("咳嗽")

    with pytest.raises(CandidateWorkspaceMismatchError):
        stack.review.review_relation(
            stack.workspace_id, relation.id, target_entity_id=foreign.id
        )

    assert stack.candidates.get_relation(relation.id).target_entity_id == (
        relation.target_entity_id
    )


def test_relation_cannot_point_at_itself() -> None:
    stack = _stack()
    relation = stack.relation()

    with pytest.raises(CandidateContentConflictError):
        stack.review.review_relation(
            stack.workspace_id,
            relation.id,
            source_entity_id=relation.target_entity_id,
            target_entity_id=relation.target_entity_id,
        )


def test_relation_type_outside_the_adapter_vocabulary_is_rejected() -> None:
    stack = _stack()
    relation = stack.relation()

    with pytest.raises(CandidateContentConflictError):
        stack.review.review_relation(
            stack.workspace_id, relation.id, relation_type="不存在的类型"
        )


def test_relation_cannot_be_approved_before_its_endpoints() -> None:
    stack = _stack()
    relation = stack.relation()

    with pytest.raises(CandidateContentConflictError):
        stack.review.review_batch(
            stack.workspace_id,
            entity_ids=(),
            relation_ids=(relation.id,),
            status=CandidateStatus.APPROVED,
        )

    assert stack.status_of(relation.id) is CandidateStatus.PENDING


def test_relation_and_endpoints_approved_in_one_batch() -> None:
    stack = _stack()
    relation = stack.relation()

    entities, relations = stack.review.review_batch(
        stack.workspace_id,
        entity_ids=(relation.source_entity_id, relation.target_entity_id),
        relation_ids=(relation.id,),
        status=CandidateStatus.APPROVED,
    )

    assert {item.status for item in entities} == {CandidateStatus.APPROVED}
    assert relations[0].status is CandidateStatus.APPROVED


def test_approving_a_relation_whose_endpoint_is_already_approved() -> None:
    stack = _stack()
    relation = stack.relation()
    stack.review.review_batch(
        stack.workspace_id,
        entity_ids=(relation.source_entity_id, relation.target_entity_id),
        relation_ids=(),
        status=CandidateStatus.APPROVED,
    )

    stack.review.review_batch(
        stack.workspace_id,
        entity_ids=(),
        relation_ids=(relation.id,),
        status=CandidateStatus.APPROVED,
    )

    assert stack.status_of(relation.id) is CandidateStatus.APPROVED


def test_rejecting_an_entity_with_a_live_relation_is_a_conflict() -> None:
    stack = _stack()
    relation = stack.relation()

    with pytest.raises(CandidateContentConflictError):
        stack.review.review_entity(
            stack.workspace_id,
            relation.source_entity_id,
            status=CandidateStatus.REJECTED,
        )

    assert stack.status_of(relation.source_entity_id) is CandidateStatus.PENDING


def test_rejecting_an_entity_and_its_relations_in_one_batch() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")
    relations = stack.relations_of(entity.id)
    assert relations

    stack.review.review_batch(
        stack.workspace_id,
        entity_ids=(entity.id,),
        relation_ids=tuple(relation.id for relation in relations),
        status=CandidateStatus.REJECTED,
    )

    assert stack.status_of(entity.id) is CandidateStatus.REJECTED
    assert {
        stack.status_of(relation.id) for relation in relations
    } == {CandidateStatus.REJECTED}


def test_rejecting_an_entity_with_only_some_relations_rejected_is_a_conflict() -> None:
    stack = _stack()
    entity = stack.entity("百日咳")
    relations = stack.relations_of(entity.id)

    # 只带走其中一条关系，剩下的仍然指向被拒的端点 —— 整批不许落地。
    with pytest.raises(CandidateContentConflictError):
        stack.review.review_batch(
            stack.workspace_id,
            entity_ids=(entity.id,),
            relation_ids=(relations[0].id,),
            status=CandidateStatus.REJECTED,
        )

    assert stack.status_of(entity.id) is CandidateStatus.PENDING
    assert stack.status_of(relations[0].id) is CandidateStatus.PENDING


def test_batch_rolls_back_entirely_on_one_bad_candidate() -> None:
    stack = _stack()
    relation = stack.relation()
    good = stack.entity("百日咳")
    bad = stack.entity("咳嗽")
    # 先把它推到 approved，让它在批里构成非法迁移。
    stack.review.review_entity(
        stack.workspace_id, bad.id, status=CandidateStatus.APPROVED
    )

    with pytest.raises(IllegalCandidateTransitionError):
        stack.review.review_batch(
            stack.workspace_id,
            entity_ids=(good.id, bad.id),
            relation_ids=(),
            status=CandidateStatus.APPROVED,
        )

    # 整批回滚：连本来合法的那一条也不能变。
    assert stack.status_of(good.id) is CandidateStatus.PENDING
    assert stack.status_of(bad.id) is CandidateStatus.APPROVED
    assert stack.status_of(relation.id) is CandidateStatus.PENDING


def test_batch_rolls_back_when_a_relation_loses_its_endpoint() -> None:
    stack = _stack()
    relation = stack.relation()

    with pytest.raises(CandidateContentConflictError):
        stack.review.review_batch(
            stack.workspace_id,
            entity_ids=(relation.source_entity_id,),
            relation_ids=(relation.id,),
            status=CandidateStatus.REJECTED,
        )

    assert stack.status_of(relation.source_entity_id) is CandidateStatus.PENDING


def test_review_against_an_unknown_workspace_is_rejected() -> None:
    """不存在的 Workspace 名下不会有候选，因此任何审核都会撞上归属不符。"""
    stack = _stack()

    with pytest.raises(CandidateWorkspaceMismatchError):
        stack.review.review_entity(
            "ws-nowhere", stack.entity("百日咳").id, name="改个名"
        )

    assert stack.entity("百日咳").name == "百日咳"


def test_a_document_without_extractable_sections_yields_no_candidates() -> None:
    stack = _Stack()
    run = stack.extract(DEFAULT_WORKSPACE_ID, "notes.md", PLAIN_MARKDOWN)

    assert stack.candidates.list_entities(run.id) == ()
    assert stack.candidates.list_relations(run.id) == ()
