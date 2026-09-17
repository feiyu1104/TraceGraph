"""候选发布：把已批准的候选幂等地写进当前 Workspace 的图后端。

这个服务是候选表与图后端之间唯一的通路：审核服务碰不到图，抽取服务也碰不到
图，只有这里在候选确实是 approved、证据确实还指得着真实 Chunk 之后才动图。

写进图里的是标识与必要元数据（实体 ID、名称、类型、关系 ID、证据 Chunk ID），
原文继续留在 SQLite 的 chunks 表里按需取 —— Neo4j 不保存整段正文的副本。

幂等靠「先查后写」：实体按「Workspace + 类型 + 规范名称」查已有节点，关系按
「Workspace + 两端 + 类型」查已有边，命中就复用它的 ID 并把新证据并进去。
因此重复发布同一批候选只会得到同一批节点和边，不会多出一份。
"""

from datetime import UTC, datetime

from tracegraph.core.contracts import (
    CandidateEntity,
    CandidateRelation,
    CandidateStatus,
    Entity,
    PublicationOutcome,
    Relation,
)
from tracegraph.core.identity import graph_entity_id, graph_relation_id
from tracegraph.core.ports import (
    CandidateRepository,
    DocumentRepository,
    GraphRepository,
)


class PublicationError(RuntimeError):
    """发布阶段的失败基类；接口层按 status_code 与 error_code 透出。"""

    error_code = "publication_error"
    status_code = 400


class PublicationWorkspaceNotFoundError(PublicationError):
    error_code = "workspace_not_found"
    status_code = 404


class PublicationTargetNotFoundError(PublicationError):
    error_code = "not_found"
    status_code = 404


class PublicationWorkspaceMismatchError(PublicationError):
    """候选不属于 URL 指定的 Workspace。"""

    error_code = "candidate_workspace_mismatch"
    status_code = 409


class NotApprovedError(PublicationError):
    """只有已批准的候选可以发布。"""

    error_code = "candidate_not_approved"
    status_code = 409


class PublicationEvidenceError(PublicationError):
    """候选的证据已经指不到真实的文档版本或 Chunk。"""

    error_code = "candidate_evidence_missing"
    status_code = 409


class PublicationConflictError(PublicationError):
    """关系两端还没进图，或者没有被包含在本次发布里。"""

    error_code = "publication_conflict"
    status_code = 409


class CandidatePublicationService:
    def __init__(
        self,
        documents: DocumentRepository,
        candidates: CandidateRepository,
        graph: GraphRepository,
    ) -> None:
        self.documents = documents
        self.candidates = candidates
        self.graph = graph

    def publish(
        self,
        workspace_id: str,
        *,
        entity_ids: tuple[str, ...],
        relation_ids: tuple[str, ...],
    ) -> PublicationOutcome:
        """发布一批已批准的候选；重复调用不会多出节点、边或证据。"""
        if self.documents.get_workspace(workspace_id) is None:
            raise PublicationWorkspaceNotFoundError(f"未找到 Workspace：{workspace_id}")

        entities = {item: self._approved_entity(workspace_id, item) for item in entity_ids}
        relations = {
            item: self._approved_relation(workspace_id, item) for item in relation_ids
        }
        self._require_evidence(workspace_id, (*entities.values(), *relations.values()))

        # 关系的两端可能不在本次发布的实体里（先发布了实体、之后才批准关系），
        # 但那样的端点必须已经在图中，否则这条边就指向了不存在的东西。
        resolved: dict[str, str] = {}
        groups: dict[str, list[tuple[CandidateEntity, bool]]] = {}
        for candidate in entities.values():
            graph_id, existing = self._resolve_entity(workspace_id, candidate)
            resolved[candidate.id] = graph_id
            groups.setdefault(graph_id, []).append((candidate, existing is not None))
        for relation in relations.values():
            for endpoint_id in (
                relation.source_entity_id,
                relation.target_entity_id,
            ):
                if endpoint_id in resolved:
                    continue
                endpoint = self._approved_entity(workspace_id, endpoint_id)
                graph_id, existing = self._resolve_entity(workspace_id, endpoint)
                if existing is None:
                    raise PublicationConflictError(
                        f"关系 {relation.id} 的端点 {endpoint_id} 还没有发布到图谱，"
                        "必须先把实体包含在本次发布里。"
                    )
                resolved[endpoint_id] = graph_id

        created_entities, reused_entities, skipped_entities = self._write_entities(
            workspace_id, groups
        )
        (
            created_relations,
            reused_relations,
            skipped_relations,
            relation_graph_ids,
        ) = self._write_relations(workspace_id, relations.values(), resolved)

        # 图写完才记发布结果：中途失败时不会留下「标记成已发布、图里却没有」
        # 的候选，重试时已经写进去的部分会被认成复用，接着往下写即可。
        self.candidates.mark_published(
            workspace_id,
            entities=tuple((item.id, resolved[item.id]) for item in entities.values()),
            relations=tuple(
                (item, relation_graph_ids[item]) for item in relations
            ),
            published_at=_timestamp(),
        )
        return PublicationOutcome(
            created_entity_ids=created_entities,
            reused_entity_ids=reused_entities,
            skipped_entity_ids=skipped_entities,
            created_relation_ids=created_relations,
            reused_relation_ids=reused_relations,
            skipped_relation_ids=skipped_relations,
        )

    def publish_run(self, workspace_id: str, run_id: str) -> PublicationOutcome:
        """发布一次抽取里全部已批准的候选。

        没有获批的候选就发布一个空批次：空批次不是错误，它只是什么也不做。
        """
        run = self.candidates.get_run(run_id)
        if run is None:
            raise PublicationTargetNotFoundError(f"未找到抽取任务：{run_id}")
        if run.workspace_id != workspace_id:
            raise PublicationWorkspaceMismatchError(
                f"抽取任务 {run_id} 不属于 Workspace {workspace_id}。"
            )
        return self.publish(
            workspace_id,
            entity_ids=tuple(
                entity.id
                for entity in self.candidates.list_entities(run_id)
                if entity.status is CandidateStatus.APPROVED
            ),
            relation_ids=tuple(
                relation.id
                for relation in self.candidates.list_relations(run_id)
                if relation.status is CandidateStatus.APPROVED
            ),
        )

    def _write_entities(
        self,
        workspace_id: str,
        groups: dict[str, list[tuple[CandidateEntity, bool]]],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """按图 ID 分组写入实体，返回 (新建, 复用, 跳过) 三组图 ID。

        同一个图 ID 下的多条候选是「同一个实体」：只要节点本来就在图里，
        这个实体就算复用 —— 哪怕同批里还有一条候选从没发布过。
        """
        created: list[str] = []
        reused: list[str] = []
        skipped: list[str] = []
        for graph_id, members in sorted(groups.items()):
            candidates = [candidate for candidate, _ in members]
            if any(existed for _, existed in members):
                # 已经在图里的节点不重写：它的显示名可能来自既有数据（例如
                # 导入的 DUTMed 图谱），不该被一条新候选的写法覆盖掉。
                bucket = (
                    skipped
                    if all(candidate.graph_id == graph_id for candidate in candidates)
                    else reused
                )
            else:
                bucket = created
                self.graph.upsert_entity(
                    Entity(
                        id=graph_id,
                        name=min(
                            candidates, key=lambda item: (item.name, item.id)
                        ).name,
                        type=candidates[0].type,
                        workspace_id=workspace_id,
                    )
                )
            bucket.append(graph_id)
        return tuple(created), tuple(reused), tuple(skipped)

    def _write_relations(
        self,
        workspace_id: str,
        relations: tuple[CandidateRelation, ...],
        resolved: dict[str, str],
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        dict[str, str],
    ]:
        """写入关系，返回 (新建, 复用, 跳过, 候选 ID → 图关系 ID)。"""
        grouped: dict[str, list[CandidateRelation]] = {}
        existing_by_graph_id: dict[str, Relation | None] = {}
        assigned: dict[str, str] = {}
        for relation in relations:
            graph_id, existing = self._resolve_relation(workspace_id, relation, resolved)
            if graph_id not in grouped:
                grouped[graph_id] = []
                existing_by_graph_id[graph_id] = existing
            grouped[graph_id].append(relation)
            assigned[relation.id] = graph_id

        created: list[str] = []
        reused: list[str] = []
        skipped: list[str] = []
        for graph_id, members in sorted(grouped.items()):
            existing = existing_by_graph_id[graph_id]
            if existing is not None and all(
                member.graph_id == graph_id for member in members
            ):
                # 这条边已经由这些候选发布过：再写一遍只会是同样的内容。
                skipped.append(graph_id)
                continue
            evidence = existing.evidence_chunk_ids if existing is not None else ()
            for member in members:
                # 并集：同一条边被多份文档支持时证据累积，同一段证据不会重复。
                evidence = _union(evidence, member.evidence_chunk_ids)
            self.graph.upsert_relation(
                Relation(
                    id=graph_id,
                    source_entity_id=resolved[members[0].source_entity_id],
                    target_entity_id=resolved[members[0].target_entity_id],
                    type=members[0].type,
                    evidence_chunk_ids=evidence,
                    workspace_id=workspace_id,
                )
            )
            (reused if existing is not None else created).append(graph_id)
        return tuple(created), tuple(reused), tuple(skipped), assigned

    def _resolve_entity(
        self, workspace_id: str, candidate: CandidateEntity
    ) -> tuple[str, Entity | None]:
        """定位候选实体在图里的落点，返回 (图 ID, 已经在图里的那个节点或 None)。

        先按稳定 ID 直接取 —— 发布服务写进去的节点都能这样找到；没找到再按
        「Workspace + 类型 + 规范名称」查一次，那是给 Workspace 之前就存在的
        旧图谱（DUTMed）留的路：它们的 ID 不是这套规则生成的，但同名同类型的
        实体应该合并成同一个节点，而不是裂成两个。
        """
        derived = graph_entity_id(
            workspace_id, candidate.type, candidate.normalized_name
        )
        existing = self.graph.get_entity(derived, workspace_id)
        if existing is None:
            existing = self.graph.find_entity_by_key(
                workspace_id, candidate.type, candidate.normalized_name
            )
        return (existing.id if existing else derived), existing

    def _resolve_relation(
        self,
        workspace_id: str,
        candidate: CandidateRelation,
        resolved: dict[str, str],
    ) -> tuple[str, Relation | None]:
        """定位候选关系在图里的落点，返回 (图 ID, 已经在图里的那条边或 None)。"""
        source_id = resolved[candidate.source_entity_id]
        target_id = resolved[candidate.target_entity_id]
        derived = graph_relation_id(workspace_id, source_id, candidate.type, target_id)
        existing = self.graph.get_relation(derived, workspace_id)
        if existing is None:
            existing = self.graph.find_relation_by_key(
                workspace_id, source_id, candidate.type, target_id
            )
        return (existing.id if existing else derived), existing

    def _require_evidence(
        self,
        workspace_id: str,
        candidates: tuple[CandidateEntity | CandidateRelation, ...],
    ) -> None:
        """候选的证据必须仍然指得着这个 Workspace 里的真实 Chunk。

        抽取到发布之间文档可能已经被删掉或换过版本，那样证据就断链了；带上
        断链的证据发到图里，等于在图谱里留下一段永远点不开的来源。
        """
        documents: dict[str, bool] = {}
        for candidate in candidates:
            version = self.documents.get_version(candidate.document_version_id)
            if version is None or version.document_id != candidate.document_id:
                raise PublicationEvidenceError(
                    f"候选 {candidate.id} 对应的文档版本已经不存在。"
                )
            if candidate.document_id not in documents:
                document = self.documents.get_document(candidate.document_id)
                documents[candidate.document_id] = (
                    document is not None and document.workspace_id == workspace_id
                )
            if not documents[candidate.document_id]:
                raise PublicationEvidenceError(
                    f"候选 {candidate.id} 的文档不属于 Workspace {workspace_id}。"
                )
            for chunk_id in candidate.evidence_chunk_ids:
                chunk = self.documents.get_chunk(chunk_id)
                if chunk is None:
                    raise PublicationEvidenceError(
                        f"候选 {candidate.id} 的证据 Chunk {chunk_id} 已经不存在。"
                    )
                if chunk.document_version_id != candidate.document_version_id:
                    raise PublicationEvidenceError(
                        f"候选 {candidate.id} 的证据 Chunk {chunk_id} 属于另一个"
                        "文档版本。"
                    )

    def _approved_entity(
        self, workspace_id: str, candidate_id: str
    ) -> CandidateEntity:
        entity = self.candidates.get_entity(candidate_id)
        if entity is None:
            raise PublicationTargetNotFoundError(f"候选实体不存在：{candidate_id}")
        _require_owner(entity.workspace_id, workspace_id, candidate_id)
        _require_approved(entity)
        return entity

    def _approved_relation(
        self, workspace_id: str, candidate_id: str
    ) -> CandidateRelation:
        relation = self.candidates.get_relation(candidate_id)
        if relation is None:
            raise PublicationTargetNotFoundError(f"候选关系不存在：{candidate_id}")
        _require_owner(relation.workspace_id, workspace_id, candidate_id)
        _require_approved(relation)
        return relation


def _require_owner(owner: str, workspace_id: str, candidate_id: str) -> None:
    if owner != workspace_id:
        raise PublicationWorkspaceMismatchError(
            f"候选 {candidate_id} 不属于 Workspace {workspace_id}。"
        )


def _require_approved(candidate: CandidateEntity | CandidateRelation) -> None:
    if candidate.status is not CandidateStatus.APPROVED:
        raise NotApprovedError(
            f"候选 {candidate.id} 当前是 {candidate.status.value}，只有已批准的"
            "候选可以发布。"
        )


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return (*first, *(chunk_id for chunk_id in second if chunk_id not in first))


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
