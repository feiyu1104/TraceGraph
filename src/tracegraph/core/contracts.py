from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    OUT_OF_SCOPE = "out_of_scope"
    EMERGENCY_ESCALATION = "emergency_escalation"
    SYSTEM_ERROR = "system_error"


class IngestionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class FeedbackKind(StrEnum):
    HELPFUL = "helpful"
    INCORRECT = "incorrect"
    UNSUPPORTED = "unsupported"
    WRONG_CITATION = "wrong_citation"
    OTHER = "other"


class TraversalDirection(StrEnum):
    """沿一条关系行进的方向。"""

    OUTGOING = "outgoing"
    INCOMING = "incoming"


MAX_HOPS = 3
DEFAULT_MAX_HOPS = 2


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    source_name: str
    media_type: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.source_name.strip():
            raise ValueError("Document requires a non-empty id and source_name")


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: str
    document_id: str
    number: int
    content_sha256: str

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("DocumentVersion number must be positive")
        if len(self.content_sha256) != 64:
            raise ValueError("DocumentVersion requires a SHA-256 content hash")


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str
    document_id: str
    document_version_id: str
    index: int
    content: str
    locator: str

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("Chunk index cannot be negative")
        if not self.content.strip():
            raise ValueError("Chunk requires non-empty content")


@dataclass(frozen=True, slots=True)
class IngestionJob:
    id: str
    document_id: str
    status: IngestionStatus
    processed_chunks: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        if self.processed_chunks < 0:
            raise ValueError("processed_chunks cannot be negative")
        if self.status is IngestionStatus.FAILED and not (self.error or "").strip():
            raise ValueError("failed ingestion job requires an error message")


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    name: str
    type: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip() or not self.type.strip():
            raise ValueError("Entity requires non-empty id, name, and type")


@dataclass(frozen=True, slots=True)
class Relation:
    id: str
    source_entity_id: str
    target_entity_id: str
    type: str
    evidence_chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.id,
                self.source_entity_id,
                self.target_entity_id,
                self.type,
            )
        ):
            raise ValueError("Relation requires non-empty identity fields")
        if not self.evidence_chunk_ids:
            raise ValueError("Relation requires at least one evidence Chunk")


@dataclass(frozen=True, slots=True)
class PathStep:
    """路径中的一跳：一条关系，以及走完这一跳后到达的实体。"""

    relation: Relation
    direction: TraversalDirection
    target: Entity

    def __post_init__(self) -> None:
        if self.direction is TraversalDirection.OUTGOING:
            arrival = self.relation.target_entity_id
        else:
            arrival = self.relation.source_entity_id
        if arrival != self.target.id:
            raise ValueError("PathStep 的方向与目标实体不一致")

    @property
    def origin_entity_id(self) -> str:
        if self.direction is TraversalDirection.OUTGOING:
            return self.relation.source_entity_id
        return self.relation.target_entity_id


@dataclass(frozen=True, slots=True)
class PathTruncation:
    """某一跳的邻居数超过 fanout 上限时记录，使截断对使用者可见。"""

    entity_id: str
    total_edges: int
    shown_edges: int

    def __post_init__(self) -> None:
        if self.total_edges <= self.shown_edges:
            raise ValueError("PathTruncation 只在确实发生截断时记录")


@dataclass(frozen=True, slots=True)
class GraphPath:
    """从 start 出发的一条图路径；节点序列由 start 与各步 target 派生。"""

    start: Entity
    steps: tuple[PathStep, ...]
    truncations: tuple[PathTruncation, ...] = ()

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("GraphPath 至少需要一跳")
        seen = {self.start.id}
        current_id = self.start.id
        for step in self.steps:
            if step.origin_entity_id != current_id:
                raise ValueError("GraphPath 的相邻步首尾不相接")
            if step.target.id in seen:
                raise ValueError("GraphPath 不允许重复节点")
            seen.add(step.target.id)
            current_id = step.target.id

    @property
    def nodes(self) -> tuple[Entity, ...]:
        return (self.start, *(step.target for step in self.steps))

    @property
    def hop_count(self) -> int:
        return len(self.steps)

    def label(self) -> str:
        parts = [self.start.name]
        for step in self.steps:
            arrow = "→" if step.direction is TraversalDirection.OUTGOING else "←"
            parts.append(f"{arrow} {step.relation.type} {arrow}")
            parts.append(step.target.name)
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class FrontierExpansion:
    """一个节点在给定过滤条件下的邻居扩展结果。"""

    node_id: str
    steps: tuple[PathStep, ...]
    total_edges: int

    def __post_init__(self) -> None:
        if self.total_edges < len(self.steps):
            raise ValueError("FrontierExpansion 的真实边数不能小于返回的步数")


@dataclass(frozen=True, slots=True)
class GraphStatistics:
    """图后端的规模概览，供对角比较与一致性检查使用。"""

    entities: int
    relations: int
    orphan_entities: int
    entity_types: tuple[tuple[str, int], ...] = ()
    relation_types: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if min(self.entities, self.relations, self.orphan_entities) < 0:
            raise ValueError("GraphStatistics 的计数不能为负")
        if self.orphan_entities > self.entities:
            raise ValueError("孤立实体数不能超过实体总数")


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    content: str
    document_id: str
    document_version: str
    source_name: str
    locator: str
    chunk_id: str
    retrieval_method: str
    retrieval_score: float
    graph_path: GraphPath | None = None

    def __post_init__(self) -> None:
        required = {
            "id": self.id,
            "content": self.content,
            "document_id": self.document_id,
            "source_name": self.source_name,
            "chunk_id": self.chunk_id,
            "retrieval_method": self.retrieval_method,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Evidence requires non-empty fields: {', '.join(missing)}")
        if self.graph_path is not None and not any(
            self.chunk_id in step.relation.evidence_chunk_ids
            for step in self.graph_path.steps
        ):
            raise ValueError("Evidence 的 Chunk 必须出现在图路径某一步的关系证据中")


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Claim requires non-empty text")


@dataclass(frozen=True, slots=True)
class Answer:
    status: AnswerStatus
    text: str | None = None
    claims: tuple[Claim, ...] = ()
    derived_associations: tuple[Claim, ...] = ()
    evidences: tuple[Evidence, ...] = ()
    warnings: tuple[str, ...] = ()
    # 数值指标之外还要记录生效的生成器名称与是否降级，因此不限定为 float。
    metrics: Mapping[str, object] = field(default_factory=dict)
    # 失败时给出的机器可读原因；成功与业务性拒答（证据不足、证据冲突）留空。
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is AnswerStatus.ANSWERED and not (self.text or "").strip():
            raise ValueError("answered status requires non-empty text")

        evidence_ids = [evidence.id for evidence in self.evidences]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Answer contains duplicate evidence ids")

        known_evidence = set(evidence_ids)
        unknown = {
            evidence_id
            for claim in (*self.claims, *self.derived_associations)
            for evidence_id in claim.evidence_ids
            if evidence_id not in known_evidence
        }
        if unknown:
            raise ValueError(f"Claim references unknown evidence: {sorted(unknown)}")

        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    question: str
    expected_status: AnswerStatus
    expected_entities: tuple[str, ...] = ()
    expected_relations: tuple[str, ...] = ()
    required_evidence_ids: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source: str = "curated"

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.question.strip():
            raise ValueError("EvaluationCase requires a non-empty id and question")


@dataclass(frozen=True, slots=True)
class Feedback:
    id: str
    question: str
    answer_status: AnswerStatus
    kind: FeedbackKind
    evidence_ids: tuple[str, ...] = ()
    comment: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.question.strip() or not self.created_at.strip():
            raise ValueError("Feedback requires id, question, and created_at")
