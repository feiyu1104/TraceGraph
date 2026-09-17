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

# 既有的、没有 Workspace 概念的库在迁移时全部归入这一个。它由存储层保证
# 存在，因此不可能是"查不到归属"的文档。
DEFAULT_WORKSPACE_ID = "ws-default"
DEFAULT_WORKSPACE_ADAPTER_ID = "medical"


@dataclass(frozen=True, slots=True)
class Workspace:
    """一个知识场景的隔离单位。

    目前只有文档归属这一层含义：adapter_id 只是记录这份数据按哪个领域
    适配器组织，本阶段不做适配器动态加载。
    """

    id: str
    name: str
    adapter_id: str
    created_at: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip():
            raise ValueError("Workspace requires a non-empty id and name")
        if not self.adapter_id.strip():
            raise ValueError("Workspace requires a non-empty adapter_id")
        if not self.created_at.strip():
            raise ValueError("Workspace requires created_at")


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    source_name: str
    media_type: str
    # 每个 Document 都必须归属一个 Workspace：没有「无归属」的文档。
    workspace_id: str
    # 入库与最后一次写入的时间。既有 DUTMed 文档没有留下时间，迁移时只能
    # 留空：宁可空着，也不要凭空造一个时间出来。
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.source_name.strip():
            raise ValueError("Document requires a non-empty id and source_name")
        if not self.workspace_id.strip():
            raise ValueError("Document requires a non-empty workspace_id")


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: str
    document_id: str
    number: int
    # 解析后正文的哈希；原件哈希见 original_sha256，两者不是一回事。
    content_sha256: str
    # 原件信息：只有留下原件的版本才有。既有 DUTMed 数据只有解析结果，
    # 没有原件可追溯，因此这四个字段允许整体缺省，但必须同进同退。
    original_sha256: str | None = None
    original_size: int | None = None
    # 相对存储根目录的路径，不是绝对路径：数据目录搬家后仍然可解析。
    stored_path: str | None = None
    original_filename: str | None = None
    # 与 Document 同理：旧版本的入库时间无从追溯，留空。
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("DocumentVersion number must be positive")
        if len(self.content_sha256) != 64:
            raise ValueError("DocumentVersion requires a SHA-256 content hash")

        original = (
            self.original_sha256,
            self.original_size,
            self.stored_path,
            self.original_filename,
        )
        if all(value is None for value in original):
            return
        if any(value is None for value in original):
            raise ValueError("DocumentVersion 的原件信息必须同时提供或同时缺省")
        if len(self.original_sha256) != 64:
            raise ValueError("original_sha256 必须是原件字节的 SHA-256")
        if self.original_size < 0:
            raise ValueError("original_size 不能为负")
        if not self.stored_path.strip() or not self.original_filename.strip():
            raise ValueError("原件的 stored_path 与 original_filename 不能为空")


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
    """图中的一个实体；归属的 Workspace 是它的一部分，不是外部上下文。

    默认值只服务于 Workspace 概念出现之前的 DUTMed 数据 —— 那批数据全部属于
    默认 Workspace。图仓储的每个读写方法仍然显式要求 workspace_id，因此
    「实体自带归属」不会被用来绕过方法参数上的隔离。
    """

    id: str
    name: str
    type: str
    workspace_id: str = DEFAULT_WORKSPACE_ID

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.name.strip() or not self.type.strip():
            raise ValueError("Entity requires non-empty id, name, and type")
        if not self.workspace_id.strip():
            raise ValueError("Entity requires a non-empty workspace_id")


@dataclass(frozen=True, slots=True)
class Relation:
    id: str
    source_entity_id: str
    target_entity_id: str
    type: str
    evidence_chunk_ids: tuple[str, ...]
    workspace_id: str = DEFAULT_WORKSPACE_ID

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
        if not self.workspace_id.strip():
            raise ValueError("Relation requires a non-empty workspace_id")
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


# ---------------------------------------------------------------------------
# 候选知识：抽取阶段产出的、尚未经过人工审核的实体与关系。
#
# 候选与正式图谱是两套东西：候选全部带着证据来源躺在候选表里，只有经过
# 审核之后才可能被发布。因此这里的每一条记录都强制要求至少一个真实 Chunk
# 作为证据 —— 没有证据的候选进不了契约，也就没有渠道被写进数据库。
# ---------------------------------------------------------------------------


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CandidateStatus(StrEnum):
    """候选知识的审核状态；抽取阶段只产生 pending。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CandidateKind(StrEnum):
    """一条证据关联指向的是候选实体还是候选关系。"""

    ENTITY = "entity"
    RELATION = "relation"


@dataclass(frozen=True, slots=True)
class ExtractionVocabulary:
    """适配器为「不调用在线模型」的确定性抽取给出的章节词汇表。

    它把文档自身的章节结构接到本领域声明的类型上：`subject_type` 是文档标题
    （分块定位符的第一层）指向的实体类型，`sections` 把章节标题映射到
    （实体类型, 关系类型）。领域词汇只出现在适配器里，抽取服务与提示词都不
    认识任何一个具体章节名。

    适配器不给词汇表（返回 None）表示该领域没有可确定抽取的章节约定，此时
    摘录式抽取如实产出 0 条候选，而不是猜一个类型。
    """

    subject_type: str
    sections: Mapping[str, tuple[str, str]]
    # 一个章节里并列写多个条目时的分隔符。它和章节标题一样属于「这份领域的
    # 文档长什么样」，因此由适配器给出，抽取服务不认识任何具体的分隔符。
    separator: str = "、"

    def __post_init__(self) -> None:
        if not self.subject_type.strip():
            raise ValueError("抽取词汇表必须给出 subject_type")
        if not self.separator:
            raise ValueError("抽取词汇表必须给出一个非空的分隔符")
        object.__setattr__(self, "sections", MappingProxyType(dict(self.sections)))


@dataclass(frozen=True, slots=True)
class ExtractionRun:
    """一次抽取任务；候选知识全部挂在这条记录下。"""

    id: str
    workspace_id: str
    document_id: str
    document_version_id: str
    adapter_id: str
    model_id: str
    status: ExtractionStatus
    created_at: str
    updated_at: str
    entity_count: int = 0
    relation_count: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        required = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "adapter_id": self.adapter_id,
            "model_id": self.model_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(
                f"ExtractionRun requires non-empty fields: {', '.join(missing)}"
            )
        if min(self.entity_count, self.relation_count) < 0:
            raise ValueError("候选计数不能为负")
        if self.status is ExtractionStatus.FAILED and not (self.error or "").strip():
            raise ValueError("失败的抽取任务必须给出原因")


def _require_candidate_scope(
    *,
    candidate_id: str,
    extraction_run_id: str,
    workspace_id: str,
    document_id: str,
    document_version_id: str,
    adapter_id: str,
    candidate_type: str,
    evidence_chunk_ids: tuple[str, ...],
    created_at: str,
    updated_at: str,
) -> None:
    """候选实体与候选关系共用的必填项与证据校验。

    证据非空在这里是硬约束，而不是靠调用方自觉：契约层就拒绝了没有来源的
    候选知识，抽取服务与存储层因此都不可能把它保存下来。
    """
    required = {
        "id": candidate_id,
        "extraction_run_id": extraction_run_id,
        "workspace_id": workspace_id,
        "document_id": document_id,
        "document_version_id": document_version_id,
        "adapter_id": adapter_id,
        "type": candidate_type,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise ValueError(f"候选知识 requires non-empty fields: {', '.join(missing)}")
    if not evidence_chunk_ids:
        raise ValueError("候选知识必须至少关联一个真实 Chunk 作为证据")
    if not all(chunk_id.strip() for chunk_id in evidence_chunk_ids):
        raise ValueError("候选知识的证据 Chunk ID 不能为空")
    if len(set(evidence_chunk_ids)) != len(evidence_chunk_ids):
        raise ValueError("候选知识的证据 Chunk 不能重复")


def _require_publication_pair(
    published_at: str | None, graph_id: str | None, status: CandidateStatus
) -> None:
    """发布状态的唯一判据是 `published_at`，它与图落点必须同进同退。

    只有已批准的候选能被发布，所以「已发布但状态不是 approved」是矛盾状态：
    它只可能来自绕过审核服务直接标记发布的写入。
    """
    if (published_at is None) != (graph_id is None):
        raise ValueError("候选的 published_at 与 graph_id 必须同时存在或同时为空")
    if published_at is None:
        return
    if not published_at.strip() or not graph_id.strip():
        raise ValueError("候选的 published_at 与 graph_id 不能为空")
    if status is not CandidateStatus.APPROVED:
        raise ValueError("只有已批准的候选才能处于已发布状态")


@dataclass(frozen=True, slots=True)
class CandidateEntity:
    id: str
    extraction_run_id: str
    workspace_id: str
    document_id: str
    document_version_id: str
    adapter_id: str
    name: str
    # 去重用的规范化名称（折叠空白、去首尾标点、大小写归一）。
    normalized_name: str
    type: str
    evidence_chunk_ids: tuple[str, ...]
    created_at: str
    updated_at: str
    status: CandidateStatus = CandidateStatus.PENDING
    # 发布状态与审核状态是两个概念，因此不挤进 `status`：已批准的候选可以
    # 还没发布，发布过的候选也不会因此变成一个"新的审核状态"。两者同时
    # 为空表示还没发布；`graph_id` 是它在图后端的落点。
    published_at: str | None = None
    graph_id: str | None = None

    def __post_init__(self) -> None:
        _require_candidate_scope(
            candidate_id=self.id,
            extraction_run_id=self.extraction_run_id,
            workspace_id=self.workspace_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            adapter_id=self.adapter_id,
            candidate_type=self.type,
            evidence_chunk_ids=self.evidence_chunk_ids,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
        _require_publication_pair(self.published_at, self.graph_id, self.status)
        if not self.name.strip() or not self.normalized_name.strip():
            raise ValueError("候选实体必须有非空名称")

    @property
    def is_published(self) -> bool:
        return self.published_at is not None


@dataclass(frozen=True, slots=True)
class CandidateRelation:
    id: str
    extraction_run_id: str
    workspace_id: str
    document_id: str
    document_version_id: str
    adapter_id: str
    # 两端都是同一次抽取里的候选实体 ID：关系的端点不可能是别的东西。
    source_entity_id: str
    target_entity_id: str
    type: str
    evidence_chunk_ids: tuple[str, ...]
    created_at: str
    updated_at: str
    status: CandidateStatus = CandidateStatus.PENDING
    published_at: str | None = None
    graph_id: str | None = None

    def __post_init__(self) -> None:
        _require_candidate_scope(
            candidate_id=self.id,
            extraction_run_id=self.extraction_run_id,
            workspace_id=self.workspace_id,
            document_id=self.document_id,
            document_version_id=self.document_version_id,
            adapter_id=self.adapter_id,
            candidate_type=self.type,
            evidence_chunk_ids=self.evidence_chunk_ids,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
        _require_publication_pair(self.published_at, self.graph_id, self.status)
        if not self.source_entity_id.strip() or not self.target_entity_id.strip():
            raise ValueError("候选关系必须给出两端的候选实体")
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("候选关系不允许自环")

    @property
    def is_published(self) -> bool:
        return self.published_at is not None


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """一条「候选 ← 真实 Chunk」的证据关联。

    实体证据与关系证据共用这一种形状，分别存在两张表里。`chunk_id` 指向
    `chunks` 表，因此数据库层面也拒绝关联到不存在的 Chunk。
    """

    candidate_id: str
    candidate_kind: CandidateKind
    chunk_id: str

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.chunk_id.strip():
            raise ValueError("证据关联必须给出候选 ID 与 Chunk ID")


@dataclass(frozen=True, slots=True)
class CandidateTally:
    """一个文档名下的候选概览。

    审核状态三选一，因此前三项相加是候选总数；`published` 与它们正交 ——
    已发布的候选同时仍然是已批准的那一条，不会被算成第四种审核状态。
    """

    pending: int = 0
    approved: int = 0
    rejected: int = 0
    published: int = 0

    def __post_init__(self) -> None:
        if min(self.pending, self.approved, self.rejected, self.published) < 0:
            raise ValueError("候选计数不能为负")
        if self.published > self.approved:
            raise ValueError("已发布的候选数不能超过已批准数")

    @property
    def total(self) -> int:
        return self.pending + self.approved + self.rejected


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    """一次发布的结果：新建、复用与幂等跳过的图对象各有多少。"""

    created_entity_ids: tuple[str, ...] = ()
    reused_entity_ids: tuple[str, ...] = ()
    skipped_entity_ids: tuple[str, ...] = ()
    created_relation_ids: tuple[str, ...] = ()
    reused_relation_ids: tuple[str, ...] = ()
    skipped_relation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for group in (
            self.created_entity_ids,
            self.reused_entity_ids,
            self.skipped_entity_ids,
            self.created_relation_ids,
            self.reused_relation_ids,
            self.skipped_relation_ids,
        ):
            if len(set(group)) != len(group):
                raise ValueError("发布结果里的图对象 ID 不能重复")

    def to_counts(self) -> dict[str, int]:
        return {
            "entities_created": len(self.created_entity_ids),
            "entities_reused": len(self.reused_entity_ids),
            "entities_skipped": len(self.skipped_entity_ids),
            "relations_created": len(self.created_relation_ids),
            "relations_reused": len(self.reused_relation_ids),
            "relations_skipped": len(self.skipped_relation_ids),
        }
