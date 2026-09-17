from tracegraph.core.ports import (
    CandidateRepository,
    DocumentRepository,
    GraphRepository,
    OriginalDocumentStore,
)


class DocumentLifecycleService:
    def __init__(
        self,
        documents: DocumentRepository,
        graph: GraphRepository | None = None,
        originals: OriginalDocumentStore | None = None,
        candidates: CandidateRepository | None = None,
    ) -> None:
        self.documents = documents
        self.graph = graph
        self.originals = originals
        self.candidates = candidates

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
        if self.candidates is not None:
            # 必须在 Chunk 之前删：候选的证据关联引用 chunks，反过来会先撞上外键。
            self.candidates.delete_document(document_id)
        deleted_chunk_ids = self.documents.delete_document(document_id)
        if set(deleted_chunk_ids) != set(chunk_ids):
            raise RuntimeError("文档删除结果与预期 Chunk 不一致")
        if self.originals is not None:
            # 落点由 Workspace 与 Document ID 现推，既不读数据库里的 stored_path，
            # 也不接受调用方给的路径，因此递归删除跑不出这个文档自己的目录。
            self.originals.remove_document(
                workspace_id=document.workspace_id,
                document_id=document_id,
            )
        return deleted_chunk_ids
