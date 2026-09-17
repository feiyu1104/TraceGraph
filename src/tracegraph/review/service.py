"""候选审核：审核状态迁移与内容修正，以及两者的合法性与冲突判定。

本服务只碰候选仓储。它没有图仓储的引用，因此「审核通过」永远不会顺手写进
正式图谱 —— 发布是另一个服务、另一个接口、另一次显式调用的事。

状态机是封闭的四个迁移，没有第五个：

    pending  → approved        pending  → rejected
    approved → pending         rejected → pending

「已发布」不是第五个状态，而是与审核状态正交的一列。已发布的候选不能改内容
也不能退回：图里那份数据还在，退回只会让候选表和图后端各说各话。撤销发布是
未来的功能，本批遇到这种请求一律返回冲突。
"""

from dataclasses import replace
from datetime import UTC, datetime

from tracegraph.core.contracts import (
    CandidateEntity,
    CandidateRelation,
    CandidateStatus,
)
from tracegraph.core.identity import normalize_name
from tracegraph.core.ports import (
    CandidateRepository,
    DocumentRepository,
    DomainAdapter,
)
from tracegraph.domains.registry import AdapterRegistry, UnknownAdapterError


class CandidateReviewError(RuntimeError):
    """审核阶段的失败基类；接口层按 status_code 与 error_code 透出。"""

    error_code = "candidate_review_error"
    status_code = 400


class CandidateNotFoundError(CandidateReviewError):
    error_code = "not_found"
    status_code = 404


class CandidateWorkspaceMismatchError(CandidateReviewError):
    """候选不属于请求指定的 Workspace。"""

    error_code = "candidate_workspace_mismatch"
    status_code = 409


class IllegalCandidateTransitionError(CandidateReviewError):
    error_code = "illegal_candidate_transition"
    status_code = 409


class PublishedCandidateConflictError(CandidateReviewError):
    """已发布的候选不能再改内容或退回。"""

    error_code = "candidate_already_published"
    status_code = 409


class CandidateContentConflictError(CandidateReviewError):
    """内容修正撞上了类型白名单、自环、跨文档端点或重复候选。"""

    error_code = "candidate_content_conflict"
    status_code = 409


# 状态机只有这四个迁移。`status -> status` 这种空操作刻意不在里面：批量接口
# 收到一个已经处于目标状态的候选时应当整批失败，而不是悄悄跳过它。
_ALLOWED_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.PENDING: frozenset(
        {CandidateStatus.APPROVED, CandidateStatus.REJECTED}
    ),
    CandidateStatus.APPROVED: frozenset({CandidateStatus.PENDING}),
    CandidateStatus.REJECTED: frozenset({CandidateStatus.PENDING}),
}


class CandidateReviewService:
    def __init__(
        self,
        documents: DocumentRepository,
        candidates: CandidateRepository,
        adapters: AdapterRegistry,
    ) -> None:
        self.documents = documents
        self.candidates = candidates
        self.adapters = adapters

    def review_entity(
        self,
        workspace_id: str,
        candidate_id: str,
        *,
        status: CandidateStatus | None = None,
        name: str | None = None,
        entity_type: str | None = None,
    ) -> CandidateEntity:
        """单条候选实体的审核与内容修正；什么都不给则原样返回。

        单条只是「这批只有一条」：判定与写入都走批量那条路径。
        """
        if status is not None or name is not None or entity_type is not None:
            self._review(
                workspace_id,
                entity_ids=(candidate_id,),
                relation_ids=(),
                status=status,
                content={candidate_id: {"name": name, "entity_type": entity_type}},
            )
        return self._entity(workspace_id, candidate_id)

    def review_relation(
        self,
        workspace_id: str,
        candidate_id: str,
        *,
        status: CandidateStatus | None = None,
        source_entity_id: str | None = None,
        target_entity_id: str | None = None,
        relation_type: str | None = None,
    ) -> CandidateRelation:
        if (
            status is not None
            or source_entity_id is not None
            or target_entity_id is not None
            or relation_type is not None
        ):
            self._review(
                workspace_id,
                entity_ids=(),
                relation_ids=(candidate_id,),
                status=status,
                content={
                    candidate_id: {
                        "source_entity_id": source_entity_id,
                        "target_entity_id": target_entity_id,
                        "relation_type": relation_type,
                    }
                },
            )
        return self._relation(workspace_id, candidate_id)

    def review_batch(
        self,
        workspace_id: str,
        *,
        entity_ids: tuple[str, ...],
        relation_ids: tuple[str, ...],
        status: CandidateStatus,
    ) -> tuple[tuple[CandidateEntity, ...], tuple[CandidateRelation, ...]]:
        """把一批候选改成同一个审核状态。

        整批要么全改、要么一条都不改：任何一个候选不存在、不属于这个
        Workspace、状态迁移非法，或者改完会在图侧留下悬空关系 / 批准一条
        两端未批准的关系，都在写之前抛错。
        """
        self._review(
            workspace_id,
            entity_ids=entity_ids,
            relation_ids=relation_ids,
            status=status,
            content={},
        )
        return (
            tuple(self._entity(workspace_id, item) for item in entity_ids),
            tuple(self._relation(workspace_id, item) for item in relation_ids),
        )

    def _review(
        self,
        workspace_id: str,
        *,
        entity_ids: tuple[str, ...],
        relation_ids: tuple[str, ...],
        status: CandidateStatus | None,
        content: dict[str, dict[str, str | None]],
    ) -> None:
        """审核与修正的唯一实现：先整体判定，再交给仓储一次写完。"""
        entities = {item: self._entity(workspace_id, item) for item in entity_ids}
        relations = {item: self._relation(workspace_id, item) for item in relation_ids}

        if status is not None:
            for candidate in (*entities.values(), *relations.values()):
                self._require_transition(candidate, status)

        # 只改状态时请求里也带着内容字段（全是 None），那些不算修正：把它们
        # 当成修正会让「批准后再退回 pending」这种纯状态操作撞上「只有待审核
        # 的候选能改内容」。
        edits = {
            candidate_id: fields
            for candidate_id, fields in content.items()
            if any(value is not None for value in fields.values())
        }
        # 内容修正得到的是「改完但还是原状态」的对象；状态变更由下一步单独写。
        edited_entities = dict(entities)
        edited_relations = dict(relations)
        if edits:
            adapter = self._adapter(workspace_id)
            for candidate_id, fields in edits.items():
                if candidate_id in entities:
                    edited_entities[candidate_id] = self._edit_entity(
                        entities[candidate_id], fields, adapter
                    )
                elif candidate_id in relations:
                    edited_relations[candidate_id] = self._edit_relation(
                        workspace_id, relations[candidate_id], fields, adapter
                    )
                else:
                    raise CandidateNotFoundError(f"候选不存在：{candidate_id}")

        self._require_consistent(
            workspace_id,
            batch_entities=edited_entities,
            batch_relations=edited_relations,
            target_status=status,
        )

        # 先写内容、后写状态：内容写入带的是候选原来的状态，必须在状态写入
        # 之前完成，否则会把刚批准的那一列覆盖回 pending。
        for candidate_id in edits:
            if candidate_id in edited_entities and candidate_id in entities:
                self.candidates.save_entity(edited_entities[candidate_id])
            elif candidate_id in edited_relations and candidate_id in relations:
                self.candidates.save_relation(edited_relations[candidate_id])
        if status is not None:
            self.candidates.apply_review(
                workspace_id,
                entity_ids=entity_ids,
                relation_ids=relation_ids,
                status=status,
                updated_at=_timestamp(),
            )

    def _require_transition(
        self, candidate: CandidateEntity | CandidateRelation, status: CandidateStatus
    ) -> None:
        if candidate.is_published:
            raise PublishedCandidateConflictError(
                f"候选 {candidate.id} 已经发布到图谱，撤销发布尚未实现，"
                "因此不能改状态。"
            )
        if status not in _ALLOWED_TRANSITIONS[candidate.status]:
            raise IllegalCandidateTransitionError(
                f"候选 {candidate.id} 不能从 {candidate.status.value} "
                f"变成 {status.value}。"
            )

    def _require_consistent(
        self,
        workspace_id: str,
        *,
        batch_entities: dict[str, CandidateEntity],
        batch_relations: dict[str, CandidateRelation],
        target_status: CandidateStatus | None,
    ) -> None:
        """审核结束后不允许出现悬空关系，也不允许批准两端未批准的关系。

        判定的是「改完之后会变成什么样」，因此先把本批的目标状态叠加到现状
        上，再逐条关系检查。检查覆盖该 Workspace 的全部候选关系，而不只是
        本批的这些：拒绝一条实体时，受害的往往是早就存在的那几条关系。

        已发布的关系不在检查范围内 —— 它们已经进了图谱，本批不提供撤销发布
        的路径。
        """
        def entity_status(candidate_id: str) -> CandidateStatus:
            """端点实体的终态：本批给了目标状态就按目标状态算。"""
            if candidate_id in batch_entities:
                if target_status is not None:
                    return target_status
                return batch_entities[candidate_id].status
            # 不在本批里的端点：它的归属照样要检查，因此仍走 _entity。
            return self._entity(workspace_id, candidate_id).status

        stored = {
            relation.id: relation
            for relation in self.candidates.list_workspace_relations(workspace_id)
        }
        # 本批改过的关系覆盖掉库里的旧版本，下面按「终态」判定。
        stored.update(batch_relations)
        for relation in stored.values():
            if relation.is_published:
                continue
            # 一条关系的终态：本批的目标状态优先，否则就是 stored 里那份。
            final = (
                target_status
                if relation.id in batch_relations and target_status is not None
                else relation.status
            )
            if final is CandidateStatus.REJECTED:
                continue
            endpoints = (relation.source_entity_id, relation.target_entity_id)
            if final is CandidateStatus.APPROVED:
                for endpoint_id in endpoints:
                    if entity_status(endpoint_id) is not CandidateStatus.APPROVED:
                        raise CandidateContentConflictError(
                            f"关系 {relation.id} 获得批准前，两端实体必须已经批准"
                            "或者包含在同一批批准操作里。"
                        )
            else:
                for endpoint_id in endpoints:
                    if entity_status(endpoint_id) is CandidateStatus.REJECTED:
                        raise CandidateContentConflictError(
                            f"关系 {relation.id} 的端点 {endpoint_id} 已被拒绝，"
                            "不能留下指向它的候选关系。"
                        )

    def _edit_entity(
        self,
        entity: CandidateEntity,
        fields: dict[str, str | None],
        adapter: DomainAdapter,
    ) -> CandidateEntity:
        """按请求修正一条候选实体；只有 name 与 type 可写。"""
        self._require_editable(entity)
        name = fields.get("name")
        entity_type = fields.get("entity_type")
        new_name = entity.name if name is None else name.strip()
        new_type = entity.type if entity_type is None else entity_type.strip()
        if not new_name:
            raise CandidateContentConflictError("候选实体的名称不能为空。")
        if new_type not in set(adapter.entity_types()):
            raise CandidateContentConflictError(
                f"实体类型 {new_type} 不在当前 Workspace 适配器允许的范围内。"
            )
        normalized = normalize_name(new_name)
        # 修正之后重新做一次重复校验：改名改类型都可能撞上同一次抽取里的另一条。
        for other in self.candidates.list_entities(entity.extraction_run_id):
            if other.id != entity.id and (other.normalized_name, other.type) == (
                normalized,
                new_type,
            ):
                raise CandidateContentConflictError(
                    f"同一次抽取里已经有「{other.name}」（{other.type}）。"
                )
        return replace(
            entity,
            name=new_name,
            normalized_name=normalized,
            type=new_type,
            updated_at=_timestamp(),
        )

    def _edit_relation(
        self,
        workspace_id: str,
        relation: CandidateRelation,
        fields: dict[str, str | None],
        adapter: DomainAdapter,
    ) -> CandidateRelation:
        """按请求修正一条候选关系；只有两端与 type 可写。"""
        self._require_editable(relation)
        source_id = fields.get("source_entity_id") or relation.source_entity_id
        target_id = fields.get("target_entity_id") or relation.target_entity_id
        relation_type = fields.get("relation_type") or relation.type
        if relation_type not in set(adapter.relation_types()):
            raise CandidateContentConflictError(
                f"关系类型 {relation_type} 不在当前 Workspace 适配器允许的范围内。"
            )
        if source_id == target_id:
            raise CandidateContentConflictError("候选关系不能指向自己。")
        source = self._endpoint(workspace_id, relation, source_id)
        target = self._endpoint(workspace_id, relation, target_id)
        for other in self.candidates.list_relations(relation.extraction_run_id):
            if other.id != relation.id and (
                other.source_entity_id,
                other.type,
                other.target_entity_id,
            ) == (source.id, relation_type, target.id):
                raise CandidateContentConflictError(
                    f"同一次抽取里已经有同样的关系：{relation_type}。"
                )
        return replace(
            relation,
            source_entity_id=source.id,
            target_entity_id=target.id,
            type=relation_type,
            updated_at=_timestamp(),
        )

    def _endpoint(
        self, workspace_id: str, relation: CandidateRelation, endpoint_id: str
    ) -> CandidateEntity:
        """关系端点必须是同一次抽取、同一个文档版本下的实体。

        两者都属于同一个 Workspace 由 `_entity` 保证：端点读得出来就说明它
        确实归这个 Workspace。
        """
        entity = self._entity(workspace_id, endpoint_id)
        if (
            entity.extraction_run_id != relation.extraction_run_id
            or entity.document_version_id != relation.document_version_id
        ):
            raise CandidateContentConflictError(
                f"候选实体 {endpoint_id} 与关系 {relation.id} 不属于同一次抽取的"
                "同一个文档版本。"
            )
        return entity

    def _require_editable(self, candidate: CandidateEntity | CandidateRelation) -> None:
        if candidate.is_published:
            raise PublishedCandidateConflictError(
                f"候选 {candidate.id} 已经发布到图谱，不能修改内容。"
            )
        if candidate.status is not CandidateStatus.PENDING:
            raise IllegalCandidateTransitionError(
                f"只有待审核的候选可以修改内容，{candidate.id} 当前是 "
                f"{candidate.status.value}。"
            )

    def _entity(self, workspace_id: str, candidate_id: str) -> CandidateEntity:
        entity = self.candidates.get_entity(candidate_id)
        if entity is None:
            raise CandidateNotFoundError(f"候选实体不存在：{candidate_id}")
        _require_owner(entity.workspace_id, workspace_id, candidate_id)
        return entity

    def _relation(self, workspace_id: str, candidate_id: str) -> CandidateRelation:
        relation = self.candidates.get_relation(candidate_id)
        if relation is None:
            raise CandidateNotFoundError(f"候选关系不存在：{candidate_id}")
        _require_owner(relation.workspace_id, workspace_id, candidate_id)
        return relation

    def _adapter(self, workspace_id: str) -> DomainAdapter:
        """当前 Workspace 的适配器：类型白名单的唯一来源。"""
        workspace = self.documents.get_workspace(workspace_id)
        if workspace is None:
            raise CandidateNotFoundError(f"未找到 Workspace：{workspace_id}")
        try:
            return self.adapters.resolve(workspace.adapter_id)
        except UnknownAdapterError as error:
            raise CandidateReviewError(str(error)) from error


def _require_owner(owner: str, workspace_id: str, candidate_id: str) -> None:
    if owner != workspace_id:
        raise CandidateWorkspaceMismatchError(
            f"候选 {candidate_id} 不属于 Workspace {workspace_id}。"
        )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
