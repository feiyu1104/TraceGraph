from typing import Protocol

from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    AnswerStatus,
    Chunk,
    Document,
    DocumentVersion,
    Entity,
    Evidence,
    Feedback,
    FrontierExpansion,
    GraphStatistics,
    IngestionJob,
    Relation,
    TraversalDirection,
)


class DomainAdapter(Protocol):
    """DUTMed 等领域包需要提供的最小行为。"""

    name: str
    version: str

    def entity_types(self) -> tuple[str, ...]: ...

    def relation_types(self) -> tuple[str, ...]: ...

    def normalize_question(self, question: str) -> str: ...

    def preflight_status(self, question: str) -> AnswerStatus | None: ...

    def status_message(self, status: AnswerStatus, question: str) -> str: ...


class DocumentRepository(Protocol):
    """文档入库服务依赖的最小持久化接口。"""

    def get_document_by_source(self, source_name: str) -> Document | None: ...

    def get_document(self, document_id: str) -> Document | None: ...

    def list_documents(self) -> tuple[Document, ...]: ...

    def save_document(self, document: Document) -> None: ...

    def delete_document(self, document_id: str) -> tuple[str, ...]: ...

    def find_version_by_hash(
        self, document_id: str, content_sha256: str
    ) -> DocumentVersion | None: ...

    def list_versions(self, document_id: str) -> tuple[DocumentVersion, ...]: ...

    def get_version(self, version_id: str) -> DocumentVersion | None: ...

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


class Retriever(Protocol):
    """将问题转换为统一证据列表的最小检索接口。"""

    name: str

    def retrieve(
        self, query: str, limit: int = 5, max_hops: int = DEFAULT_MAX_HOPS
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


class FeedbackRepository(Protocol):
    def save_feedback(self, feedback: Feedback) -> None: ...

    def get_feedback(self, feedback_id: str) -> Feedback | None: ...

    def list_feedback(self) -> tuple[Feedback, ...]: ...
