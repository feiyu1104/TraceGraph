"""候选发布：幂等、证据复检、跨 Workspace 拒绝，以及图侧的可见性。"""

import pytest

from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    CandidateStatus,
    Entity,
    Relation,
    Workspace,
)
from tracegraph.core.identity import graph_entity_id, graph_relation_id
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.extraction.service import ExtractionService
from tracegraph.generation.models import ModelEntry, ModelRegistry
from tracegraph.generation.providers import ExtractiveAnswerGenerator
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.publication.service import (
    CandidatePublicationService,
    NotApprovedError,
    PublicationConflictError,
    PublicationEvidenceError,
    PublicationTargetNotFoundError,
    PublicationWorkspaceMismatchError,
    PublicationWorkspaceNotFoundError,
)
from tracegraph.retrieval.traversal import traverse_paths
from tracegraph.review.service import CandidateReviewService
from tracegraph.storage.candidates import InMemoryCandidateRepository
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository

DUTMED_MARKDOWN = """# 百日咳

## 简介

百日咳是由百日咳鲍特菌引起的急性呼吸道传染病。

## 症状

咳嗽、低热

## 推荐药物

琥乙红霉素片
"""

OTHER_MARKDOWN = """# 小儿支原体肺炎

## 简介

小儿支原体肺炎是儿童常见的呼吸道感染。

## 推荐药物

琥乙红霉素片
"""

_CREATED_AT = "2026-01-01T00:00:00+00:00"


def _registry() -> ModelRegistry:
    entry = ModelEntry(id="extractive", label="extractive", kind="extractive", model="t")
    return ModelRegistry("extractive", (entry,), {"extractive": ExtractiveAnswerGenerator()})


class _Stack:
    """两个 Workspace 各抽一次，共用同一套仓储与同一个图后端。"""

    OTHER = "ws-b"

    def __init__(self, graph=None) -> None:
        self.workspace_id = DEFAULT_WORKSPACE_ID
        self.documents = InMemoryDocumentRepository()
        self.candidates = InMemoryCandidateRepository()
        self.graph = graph if graph is not None else InMemoryGraphRepository()
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
        self.publication = CandidatePublicationService(
            self.documents, self.candidates, self.graph
        )
        self.run = self._extract(self.workspace_id, "dutmed-百日咳.md", DUTMED_MARKDOWN)
        self.entities = self.candidates.list_entities(self.run.id)
        self.relations = self.candidates.list_relations(self.run.id)
        self.foreign_run = self._extract(
            self.OTHER, "dutmed-支原体肺炎.md", OTHER_MARKDOWN
        )

    def _extract(self, workspace_id: str, source_name: str, content: str) -> object:
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

    def relations_of(self, entity_id: str) -> tuple[object, ...]:
        return tuple(
            relation
            for relation in self.relations
            if entity_id in (relation.source_entity_id, relation.target_entity_id)
        )

    def approve_all(self, run_id: str | None = None) -> None:
        """把一次抽取的全部候选一起批准：实体先行，关系跟上。"""
        run_id = run_id or self.run.id
        entities = self.candidates.list_entities(run_id)
        relations = self.candidates.list_relations(run_id)
        self.review.review_batch(
            self._workspace_of(run_id),
            entity_ids=tuple(item.id for item in entities),
            relation_ids=(),
            status=CandidateStatus.APPROVED,
        )
        self.review.review_batch(
            self._workspace_of(run_id),
            entity_ids=(),
            relation_ids=tuple(item.id for item in relations),
            status=CandidateStatus.APPROVED,
        )

    def _workspace_of(self, run_id: str) -> str:
        return self.candidates.get_run(run_id).workspace_id

    def publish_all(self, **overrides):
        """发布本次抽取的全部已批准候选。"""
        return self.publication.publish(
            self.workspace_id,
            entity_ids=overrides.get(
                "entity_ids", tuple(item.id for item in self.entities)
            ),
            relation_ids=overrides.get(
                "relation_ids", tuple(item.id for item in self.relations)
            ),
        )

    def entities_in_graph(self, workspace_id: str | None = None) -> tuple[Entity, ...]:
        workspace_id = workspace_id or self.workspace_id
        return tuple(
            entity
            for entity in self.graph._entities.values()
            if entity.workspace_id == workspace_id
        )

    def relations_in_graph(self, workspace_id: str | None = None) -> tuple[Relation, ...]:
        workspace_id = workspace_id or self.workspace_id
        return tuple(
            relation
            for relation in self.graph._relations.values()
            if relation.workspace_id == workspace_id
        )


def _approved_stack(**kwargs) -> _Stack:
    stack = _Stack(**kwargs)
    stack.approve_all()
    return stack


def test_publishing_creates_entities_and_relations() -> None:
    stack = _approved_stack()

    outcome = stack.publish_all()

    assert outcome.created_entity_ids
    assert outcome.created_relation_ids
    assert not outcome.reused_entity_ids
    assert len(stack.entities_in_graph()) == len(stack.entities)
    assert len(stack.relations_in_graph()) == len(stack.relations)
    assert outcome.to_counts() == {
        "entities_created": len(stack.entities),
        "entities_reused": 0,
        "entities_skipped": 0,
        "relations_created": len(stack.relations),
        "relations_reused": 0,
        "relations_skipped": 0,
    }


def test_publishing_is_idempotent() -> None:
    stack = _approved_stack()
    first = stack.publish_all()
    graph_entities = stack.entities_in_graph()
    graph_relations = stack.relations_in_graph()

    second = stack.publish_all()

    # 第二次一个节点、一条边都不新增，全部落在「跳过」里。
    assert second.created_entity_ids == ()
    assert second.created_relation_ids == ()
    assert not second.reused_entity_ids
    assert not second.reused_relation_ids
    assert set(second.skipped_entity_ids) == set(first.created_entity_ids)
    assert set(second.skipped_relation_ids) == set(first.created_relation_ids)
    assert stack.entities_in_graph() == graph_entities
    assert stack.relations_in_graph() == graph_relations


def test_publishing_records_the_graph_id_on_the_candidate() -> None:
    stack = _approved_stack()
    stack.publish_all()

    entity = stack.candidates.get_entity(stack.entity("百日咳").id)
    assert entity.published_at is not None
    assert entity.graph_id is not None
    # 发布状态与审核状态正交：发布之后仍然是 approved。
    assert entity.status is CandidateStatus.APPROVED
    assert entity.is_published


def test_evidence_union_grows_without_duplicating_edges() -> None:
    stack = _approved_stack()
    stack.publish_all()
    before = stack.relations_in_graph()

    # 同一个 Workspace 里再抽一份同样的内容：稳定 ID 撞上已有的节点与边，
    # 证据并进去，而不是多出一份图谱。
    run = stack._extract(
        stack.workspace_id, "dutmed-百日咳-副本.md", DUTMED_MARKDOWN
    )
    stack.approve_all(run.id)
    stack.publication.publish_run(stack.workspace_id, run.id)

    after = stack.relations_in_graph()
    assert {relation.id for relation in after} == {relation.id for relation in before}
    # 稳定 ID 相同 -> 同一个节点、同一条边，证据取并集后变多。
    assert sum(len(relation.evidence_chunk_ids) for relation in after) > sum(
        len(relation.evidence_chunk_ids) for relation in before
    )
    # 证据里没有重复项。
    for relation in after:
        assert len(set(relation.evidence_chunk_ids)) == len(relation.evidence_chunk_ids)


def test_pending_candidates_cannot_be_published() -> None:
    stack = _Stack()

    with pytest.raises(NotApprovedError):
        stack.publish_all()

    assert stack.entities_in_graph() == ()


def test_rejected_candidates_cannot_be_published() -> None:
    stack = _Stack()
    entity = stack.entity("百日咳")
    # 拒绝一个实体要连它的关系一起带走，否则会留下悬空关系。
    stack.review.review_batch(
        stack.workspace_id,
        entity_ids=(entity.id,),
        relation_ids=tuple(item.id for item in stack.relations_of(entity.id)),
        status=CandidateStatus.REJECTED,
    )

    with pytest.raises(NotApprovedError):
        stack.publication.publish(
            stack.workspace_id, entity_ids=(entity.id,), relation_ids=()
        )

    assert stack.entities_in_graph() == ()


def test_cross_workspace_publication_is_rejected() -> None:
    stack = _approved_stack()
    foreign = stack.foreign_entity("琥乙红霉素片")

    # 拿别的 Workspace 的候选到自己这里发。
    with pytest.raises(PublicationWorkspaceMismatchError):
        stack.publication.publish(
            stack.workspace_id, entity_ids=(foreign.id,), relation_ids=()
        )
    # 拿自己的候选去别的 Workspace 发。
    with pytest.raises(PublicationWorkspaceMismatchError):
        stack.publication.publish(
            stack.OTHER, entity_ids=(stack.entity("百日咳").id,), relation_ids=()
        )

    assert stack.entities_in_graph() == ()
    assert stack.entities_in_graph(stack.OTHER) == ()


def test_unknown_workspace_is_reported() -> None:
    stack = _approved_stack()

    with pytest.raises(PublicationWorkspaceNotFoundError):
        stack.publication.publish(
            "ws-nowhere",
            entity_ids=(stack.entity("百日咳").id,),
            relation_ids=(),
        )


def test_unknown_candidate_is_reported() -> None:
    stack = _approved_stack()

    with pytest.raises(PublicationTargetNotFoundError):
        stack.publication.publish(
            stack.workspace_id, entity_ids=("cand-missing",), relation_ids=()
        )


def test_a_relation_whose_endpoint_is_not_published_is_a_conflict() -> None:
    stack = _approved_stack()
    relation = stack.relations[0]

    with pytest.raises(PublicationConflictError):
        stack.publication.publish(
            stack.workspace_id, entity_ids=(), relation_ids=(relation.id,)
        )

    assert stack.relations_in_graph() == ()


def test_a_relation_can_be_published_after_its_endpoints_were() -> None:
    stack = _approved_stack()
    relation = stack.relations[0]

    stack.publication.publish(
        stack.workspace_id,
        entity_ids=(relation.source_entity_id, relation.target_entity_id),
        relation_ids=(),
    )
    outcome = stack.publication.publish(
        stack.workspace_id, entity_ids=(), relation_ids=(relation.id,)
    )

    assert len(outcome.created_relation_ids) == 1


def test_broken_evidence_blocks_publication() -> None:
    stack = _approved_stack()
    target = stack.entities[0]
    # 文档被删掉之后，候选手里的 Chunk 就指不到任何真实内容了。
    stack.documents.delete_document(target.document_id)

    with pytest.raises(PublicationEvidenceError):
        stack.publication.publish(
            stack.workspace_id, entity_ids=(target.id,), relation_ids=()
        )

    assert stack.entities_in_graph() == ()
    assert stack.candidates.get_entity(target.id).published_at is None


def test_publishing_a_run_publishes_only_its_approved_candidates() -> None:
    stack = _Stack()
    # 只批准「百日咳」这一个实体，关系与其余实体都不动。
    stack.review.review_entity(
        stack.workspace_id, stack.entity("百日咳").id, status=CandidateStatus.APPROVED
    )

    outcome = stack.publication.publish_run(stack.workspace_id, stack.run.id)

    assert len(outcome.created_entity_ids) == 1
    assert outcome.created_relation_ids == ()
    assert len(stack.entities_in_graph()) == 1


def test_publishing_an_unknown_run_is_reported() -> None:
    stack = _approved_stack()

    with pytest.raises(PublicationTargetNotFoundError):
        stack.publication.publish_run(stack.workspace_id, "run-missing")


def test_a_run_from_another_workspace_cannot_be_published() -> None:
    stack = _Stack()
    stack.approve_all(stack.foreign_run.id)

    with pytest.raises(PublicationWorkspaceMismatchError):
        stack.publication.publish_run(stack.workspace_id, stack.foreign_run.id)


def test_published_graph_is_readable_through_the_workspace_scoped_api() -> None:
    stack = _approved_stack()
    stack.publish_all()

    entity = stack.entity("百日咳")
    graph_id = stack.candidates.get_entity(entity.id).graph_id
    assert graph_id == graph_entity_id(
        stack.workspace_id, entity.type, entity.normalized_name
    )

    found = stack.graph.find_entity_by_key(
        stack.workspace_id, entity.type, entity.normalized_name
    )
    assert found is not None and found.id == graph_id

    paths = traverse_paths(
        stack.graph,
        found,
        workspace_id=stack.workspace_id,
        max_hops=1,
    )
    assert paths
    # 路径上的每一条关系都带着证据 Chunk ID，能回到 SQLite 取原文。
    assert all(path.steps[0].relation.evidence_chunk_ids for path in paths)


def test_relation_graph_id_is_stable_across_repeated_publication() -> None:
    stack = _approved_stack()
    stack.publish_all()
    relation = stack.relations[0]
    graph_id = stack.candidates.get_relation(relation.id).graph_id

    stack.publish_all()

    assert stack.candidates.get_relation(relation.id).graph_id == graph_id
    assert graph_id == graph_relation_id(
        stack.workspace_id,
        stack.candidates.get_entity(relation.source_entity_id).graph_id,
        relation.type,
        stack.candidates.get_entity(relation.target_entity_id).graph_id,
    )


def test_publication_reuses_an_entity_that_is_already_in_the_graph() -> None:
    stack = _approved_stack()
    entity = stack.entity("百日咳")
    # 图谱里已经有一个同名同类型的节点，但 ID 不是这套规则生成的（旧数据）。
    stack.graph.upsert_entity(
        Entity("ent-legacy", entity.name, entity.type, stack.workspace_id)
    )

    outcome = stack.publish_all()

    assert graph_entity_id(
        stack.workspace_id, entity.type, entity.normalized_name
    ) not in outcome.created_entity_ids
    assert "ent-legacy" in outcome.reused_entity_ids
    assert stack.candidates.get_entity(entity.id).graph_id == "ent-legacy"
    # 旧节点没有被重写、也没有被复制出一个新的。
    assert sum(1 for item in stack.entities_in_graph() if item.name == entity.name) == 1


def test_two_workspaces_publish_same_named_entities_without_merging() -> None:
    stack = _approved_stack()
    stack.publish_all()
    stack.approve_all(stack.foreign_run.id)
    stack.publication.publish_run(stack.OTHER, stack.foreign_run.id)

    own = stack.candidates.get_entity(stack.entity("琥乙红霉素片").id)
    foreign = stack.candidates.get_entity(stack.foreign_entity("琥乙红霉素片").id)

    assert own.graph_id != foreign.graph_id
    assert stack.graph.get_entity(own.graph_id, stack.OTHER) is None
    assert stack.graph.get_entity(foreign.graph_id, stack.workspace_id) is None
    assert stack.graph.get_entity(own.graph_id, stack.workspace_id) is not None
    assert stack.graph.get_entity(foreign.graph_id, stack.OTHER) is not None
