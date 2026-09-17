from typing import Protocol

from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    DEFAULT_WORKSPACE_ID,
    AnswerStatus,
    CandidateEntity,
    CandidateEvidence,
    CandidateRelation,
    CandidateStatus,
    Chunk,
    Document,
    DocumentVersion,
    Entity,
    Evidence,
    ExtractionRun,
    ExtractionVocabulary,
    Feedback,
    FrontierExpansion,
    GraphStatistics,
    IngestionJob,
    Relation,
    TraversalDirection,
    Workspace,
)


class DomainAdapter(Protocol):
    """DUTMed 等领域包需要提供的最小行为。"""

    name: str
    version: str

    def entity_types(self) -> tuple[str, ...]: ...

    def relation_types(self) -> tuple[str, ...]: ...

    def extraction_vocabulary(self) -> ExtractionVocabulary | None:
        """确定性摘录式抽取的章节词汇表。

        这是「不调用任何在线模型时怎么抽取」的领域差异所在：章节标题到类型
        的对应关系属于领域知识，因此只能由适配器给出，抽取服务里没有任何
        一个具体领域的词。返回 None 表示这个领域没有可确定抽取的章节约定。
        """
        ...

    def normalize_question(self, question: str) -> str: ...

    def preflight_status(self, question: str) -> AnswerStatus | None: ...

    def status_message(self, status: AnswerStatus, question: str) -> str: ...


class DocumentRepository(Protocol):
    """文档入库服务依赖的最小持久化接口。"""

    def save_workspace(self, workspace: Workspace) -> None: ...

    def get_workspace(self, workspace_id: str) -> Workspace | None: ...

    def list_workspaces(self) -> tuple[Workspace, ...]: ...

    def get_document_by_source(
        self, source_name: str, workspace_id: str
    ) -> Document | None: ...

    def get_document(self, document_id: str) -> Document | None: ...

    def list_documents(self, workspace_id: str | None = None) -> tuple[Document, ...]: ...

    def save_document(self, document: Document) -> None: ...

    def delete_document(self, document_id: str) -> tuple[str, ...]: ...

    def find_version_by_hash(
        self, document_id: str, content_sha256: str
    ) -> DocumentVersion | None: ...

    def list_versions(self, document_id: str) -> tuple[DocumentVersion, ...]: ...

    def get_version(self, version_id: str) -> DocumentVersion | None: ...

    def attach_original(
        self,
        version_id: str,
        *,
        original_sha256: str,
        original_size: int,
        stored_path: str,
        original_filename: str,
    ) -> DocumentVersion:
        """给一个还没有原件的版本补上原件元信息，返回更新后的版本。

        四个字段一次性写入，不存在只补一半的中间状态。条件判断在存储层
        完成：已有原件的版本不允许被覆盖，此时抛 ValueError。
        """
        ...

    def list_chunks(self, document_version_id: str) -> tuple[Chunk, ...]: ...

    def get_chunk(self, chunk_id: str) -> Chunk | None: ...

    def get_ingestion_job(self, job_id: str) -> IngestionJob | None: ...

    def save_ingestion_job(self, job: IngestionJob) -> None: ...

    def save_ingestion(
        self,
        version: DocumentVersion,
        chunks: tuple[Chunk, ...],
        job: IngestionJob,
    ) -> None: ...


class OriginalDocumentStore(Protocol):
    """上传原件的落盘接口。

    方法只接受服务端生成的 ID，不接受调用方给的路径：落点由存储实现自己
    拼出来，因此删除时不可能被一个伪造的路径带到目录外面去。
    """

    def save(
        self,
        *,
        workspace_id: str,
        document_id: str,
        version_id: str,
        suffix: str,
        raw: bytes,
    ) -> str:
        """写入一份原件，返回相对存储根目录的路径。"""
        ...

    def read(self, stored_path: str) -> bytes | None:
        """按相对路径读回原件；不存在时返回 None。"""
        ...

    def exists(self, stored_path: str) -> bool: ...

    def remove_version(
        self, *, workspace_id: str, document_id: str, version_id: str
    ) -> None:
        """删除单个版本的原件目录；目录不存在时什么也不做。"""
        ...

    def remove_document(self, *, workspace_id: str, document_id: str) -> None:
        """删除该文档名下的全部原件。"""
        ...


class Retriever(Protocol):
    """将问题转换为统一证据列表的最小检索接口。

    `workspace_id` 是「这次检索在哪个 Workspace 里进行」，由调用方按请求
    传入，不存放在检索器实例上：同一个检索器会被并发请求共用，把它变成
    实例状态就会出现两个请求互相看到对方 Workspace 的窗口。
    """

    name: str

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        max_hops: int = DEFAULT_MAX_HOPS,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> tuple[Evidence, ...]: ...


class GraphRepository(Protocol):
    """实体关系存储的最小接口。"""

    name: str

    def upsert_entity(self, entity: Entity) -> None: ...

    def upsert_relation(self, relation: Relation) -> None: ...

    def replace_outgoing_graph(
        self,
        source: Entity,
        targets: tuple[Entity, ...],
        relations: tuple[Relation, ...],
    ) -> None: ...

    def get_entity(self, entity_id: str) -> Entity | None: ...

    def search_entities(self, query: str, limit: int = 5) -> tuple[Entity, ...]: ...

    def list_relations(
        self, entity_ids: tuple[str, ...], limit: int = 20
    ) -> tuple[Relation, ...]: ...

    def get_relation(self, relation_id: str) -> Relation | None: ...

    def statistics(self) -> GraphStatistics: ...

    def find_opposing_relations(
        self, entity_id: str, relation_types: tuple[str, str]
    ) -> tuple[Relation, ...]: ...

    def expand_frontier(
        self,
        node_ids: tuple[str, ...],
        *,
        fanout: int,
        relation_types: tuple[str, ...] | None = None,
        direction: TraversalDirection | None = None,
    ) -> tuple[FrontierExpansion, ...]: ...

    def remove_evidence(self, chunk_ids: tuple[str, ...]) -> None: ...

    def delete_outgoing_relations(self, entity_id: str) -> None: ...


class CandidateRepository(Protocol):
    """候选知识的最小持久化接口。

    候选只落在候选表里，本接口也没有任何通往图后端的路径：未经审核的候选
    不允许进入正式图谱，因此「抽取即发布」在装配层面就不可能发生。
    """

    def save_run(self, run: ExtractionRun) -> None:
        """写入或更新一条抽取任务（状态、计数与失败原因）。"""
        ...

    def save_extraction(
        self,
        run: ExtractionRun,
        entities: tuple[CandidateEntity, ...],
        relations: tuple[CandidateRelation, ...],
    ) -> None:
        """把一次抽取的全部产物作为一个事务写入。

        候选、证据关联与任务终态同进同退：失败时整批回滚，不会留下
        「候选已入库、状态还停在 running」的半批数据。
        """
        ...

    def get_run(self, run_id: str) -> ExtractionRun | None: ...

    def list_entities(self, run_id: str) -> tuple[CandidateEntity, ...]: ...

    def list_relations(self, run_id: str) -> tuple[CandidateRelation, ...]: ...

    def list_evidence(self, run_id: str) -> tuple[CandidateEvidence, ...]: ...

    def list_workspace_entities(
        self,
        workspace_id: str,
        *,
        document_id: str | None = None,
        status: CandidateStatus | None = None,
        entity_type: str | None = None,
    ) -> tuple[CandidateEntity, ...]: ...

    def list_workspace_relations(
        self,
        workspace_id: str,
        *,
        document_id: str | None = None,
        status: CandidateStatus | None = None,
        relation_type: str | None = None,
    ) -> tuple[CandidateRelation, ...]: ...

    def delete_document(self, document_id: str) -> None:
        """删除该文档名下的抽取任务与全部候选数据。"""
        ...


class FeedbackRepository(Protocol):
    def save_feedback(self, feedback: Feedback) -> None: ...

    def get_feedback(self, feedback_id: str) -> Feedback | None: ...

    def list_feedback(self) -> tuple[Feedback, ...]: ...
