from tracegraph.core.ports import DocumentRepository, GraphRepository


class DocumentLifecycleService:
    def __init__(
        self,
        documents: DocumentRepository,
        graph: GraphRepository | None = None,
    ) -> None:
        self.documents = documents
        self.graph = graph

    def delete(self, document_id: str) -> tuple[str, ...]:
        document = self.documents.get_document(document_id)
        if document is None:
            raise KeyError(document_id)
        versions = self.documents.list_versions(document_id)
        chunk_ids = tuple(
            chunk.id
            for version in versions
            for chunk in self.documents.list_chunks(version.id)
        )
        if self.graph is not None:
            self.graph.remove_evidence(chunk_ids)
        deleted_chunk_ids = self.documents.delete_document(document_id)
        if set(deleted_chunk_ids) != set(chunk_ids):
            raise RuntimeError("文档删除结果与预期 Chunk 不一致")
        return deleted_chunk_ids
