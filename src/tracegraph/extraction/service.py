"""抽取服务：把一个文档版本的真实 Chunk 变成可追溯的候选知识。

这里没有任何通往图后端的依赖 —— 候选只能落到候选表，抽取即发布在装配层面
就不可能发生。领域差异也全部来自适配器：服务本身不认识任何具体领域词。
"""

from dataclasses import replace
from datetime import UTC, datetime
import hashlib

from tracegraph.core.contracts import (
    CandidateEntity,
    CandidateRelation,
    DocumentVersion,
    ExtractionRun,
    ExtractionStatus,
)
from tracegraph.core.ports import (
    CandidateRepository,
    DocumentRepository,
    DomainAdapter,
)
from tracegraph.domains.registry import AdapterRegistry, UnknownAdapterError
from tracegraph.extraction.providers import (
    ExtractionDraft,
    ExtractionError,
    ExtractiveCandidateExtractor,
    ModelCandidateExtractor,
    normalize_name,
)
from tracegraph.generation.models import (
    KIND_EXTRACTIVE,
    KIND_OPENAI,
    ModelRegistry,
    RegistrySource,
)
from tracegraph.generation.providers import ChatCompleter


class ExtractionWorkspaceNotFoundError(ExtractionError):
    error_code = "workspace_not_found"
    status_code = 404

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"未找到 Workspace：{workspace_id}")


class ExtractionAdapterUnavailableError(ExtractionError):
    error_code = "workspace_adapter_unavailable"
    status_code = 409


class UnknownExtractionTargetError(ExtractionError):
    """要抽取的文档或版本不存在，或不属于指定的 Workspace。"""

    error_code = "not_found"
    status_code = 404


class UnsupportedExtractionModelError(ExtractionError):
    """选中的模型不能用于抽取。"""

    error_code = "extraction_model_unsupported"
    status_code = 400


class ExtractionFailedError(ExtractionError):
    """抽取本身失败；失败的抽取任务已经落库，可以按 run_id 查询。"""

    error_code = "extraction_failed"
    status_code = 502

    def __init__(self, run: ExtractionRun) -> None:
        super().__init__(f"抽取任务 {run.id} 失败：{run.error}")
        self.run = run


class ExtractionService:
    """文档切块 → 候选抽取 → 候选持久化的编排。

    抽取按 model_id 分派：离线摘录用确定性的摘录实现，在线模型通过注册表
    取到的生成器完成一次对话补全。两条路径产出同一种草稿，之后走同一套
    证据过滤与去重。
    """

    def __init__(
        self,
        documents: DocumentRepository,
        adapters: AdapterRegistry,
        candidates: CandidateRepository,
        models: RegistrySource,
    ) -> None:
        self.documents = documents
        self.adapters = adapters
        self.candidates = candidates
        self.models = models

    def start(
        self,
        workspace_id: str,
        *,
        document_id: str | None = None,
        document_version_id: str | None = None,
        model_id: str | None = None,
    ) -> ExtractionRun:
        workspace = self.documents.get_workspace(workspace_id)
        if workspace is None:
            raise ExtractionWorkspaceNotFoundError(workspace_id)
        try:
            adapter = self.adapters.resolve(workspace.adapter_id)
        except UnknownAdapterError as error:
            raise ExtractionAdapterUnavailableError(str(error)) from error

        version = self._resolve_version(
            document_id, document_version_id, workspace_id=workspace.id
        )
        # 注册表可能在这次抽取期间被运行时替换，因此先固定一份快照：模型 ID、
        # 生成器与条目类型必须出自同一份表，否则一次并发更新就可能让这次抽取
        # 拿着新表的类型去决定旧表的生成器怎么用。
        snapshot = self.models.current
        # 未知与不可用由注册表分别报出，接口层按既有约定映射成 400 与 503。
        resolved_model, generator = snapshot.resolve_selection(model_id)
        extractor = self._extractor_for(snapshot, resolved_model, generator)

        now = _timestamp()
        run = ExtractionRun(
            id=f"run-{hashlib.sha256(f'{workspace.id}\x1f{version.id}\x1f{now}'.encode('utf-8')).hexdigest()[:20]}",
            workspace_id=workspace.id,
            document_id=version.document_id,
            document_version_id=version.id,
            adapter_id=adapter.name,
            model_id=resolved_model,
            status=ExtractionStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self.candidates.save_run(run)
        self.candidates.save_run(replace(run, status=ExtractionStatus.RUNNING))

        chunks = self.documents.list_chunks(version.id)
        try:
            draft = extractor.extract(chunks, adapter)
            entities, relations = self._build_candidates(
                run,
                draft,
                adapter=adapter,
                known_chunk_ids=frozenset(chunk.id for chunk in chunks),
                now=now,
            )
        except Exception as error:
            # 失败也要如实落库：否则用户只能看到一条停在 running 的记录。
            failed = replace(
                run,
                status=ExtractionStatus.FAILED,
                error=str(error) or type(error).__name__,
                updated_at=_timestamp(),
            )
            self.candidates.save_run(failed)
            raise ExtractionFailedError(failed) from error

        succeeded = replace(
            run,
            status=ExtractionStatus.SUCCEEDED,
            entity_count=len(entities),
            relation_count=len(relations),
            updated_at=_timestamp(),
        )
        self.candidates.save_extraction(succeeded, entities, relations)
        return succeeded

    def _resolve_version(
        self,
        document_id: str | None,
        document_version_id: str | None,
        *,
        workspace_id: str,
    ) -> DocumentVersion:
        """定位要抽取的版本，并确认它确实属于这个 Workspace。

        归属是硬条件：拿别的 Workspace 的 document_id 过来只会得到 404，不会
        抽到那份文档的任何内容。
        """
        if document_version_id is not None:
            version = self.documents.get_version(document_version_id)
            if version is None:
                raise UnknownExtractionTargetError(
                    f"文档版本不存在：{document_version_id}"
                )
            if document_id is not None and version.document_id != document_id:
                raise UnknownExtractionTargetError("文档版本不属于指定的文档")
        elif document_id is not None:
            versions = self.documents.list_versions(document_id)
            if not versions:
                raise UnknownExtractionTargetError(f"文档没有可抽取的版本：{document_id}")
            version = max(versions, key=lambda item: item.number)
        else:
            raise ExtractionError("必须给出 document_id 或 document_version_id")

        document = self.documents.get_document(version.document_id)
        if document is None or document.workspace_id != workspace_id:
            raise UnknownExtractionTargetError("文档不属于指定的 Workspace")
        return version

    def _extractor_for(
        self, models: ModelRegistry, model_id: str, generator: object
    ) -> ExtractiveCandidateExtractor | ModelCandidateExtractor:
        entry = models.entry(model_id)
        kind = entry.kind if entry is not None else ""
        if kind == KIND_EXTRACTIVE:
            return ExtractiveCandidateExtractor()
        if kind == KIND_OPENAI and isinstance(generator, ChatCompleter):
            return ModelCandidateExtractor(generator)
        raise UnsupportedExtractionModelError(f"模型 {model_id} 不能用于候选抽取。")

    def _build_candidates(
        self,
        run: ExtractionRun,
        draft: ExtractionDraft,
        *,
        adapter: DomainAdapter,
        known_chunk_ids: frozenset[str],
        now: str,
    ) -> tuple[tuple[CandidateEntity, ...], tuple[CandidateRelation, ...]]:
        """把草稿变成候选契约对象。

        两条抽取路径共用这一道关卡：类型必须在适配器声明的范围内，证据必须
        是本次抽取真实读到的 Chunk，去重后没有证据的一律丢弃。
        """
        allowed_entity_types = set(adapter.entity_types())
        allowed_relation_types = set(adapter.relation_types())

        entities: dict[tuple[str, str], CandidateEntity] = {}
        for item in draft.entities:
            if item.type not in allowed_entity_types:
                continue
            evidence = _known_evidence(item.evidence_chunk_ids, known_chunk_ids)
            key = (normalize_name(item.name), item.type)
            if not evidence or not key[0]:
                continue
            existing = entities.get(key)
            if existing is None:
                entities[key] = _entity(run, key, item.name, item.type, evidence, now)
            elif not set(evidence) <= set(existing.evidence_chunk_ids):
                # 同一实体在多段里出现：合并证据，而不是留下两条。
                entities[key] = replace(
                    existing,
                    evidence_chunk_ids=_union(existing.evidence_chunk_ids, evidence),
                )

        by_name, ambiguous = _index_by_name(entities)
        relations: dict[tuple[str, str, str], CandidateRelation] = {}
        for item in draft.relations:
            if item.type not in allowed_relation_types:
                continue
            source = _endpoint(entities, by_name, ambiguous, item.source_name)
            target = _endpoint(entities, by_name, ambiguous, item.target_name)
            evidence = _known_evidence(item.evidence_chunk_ids, known_chunk_ids)
            if source is None or target is None or not evidence:
                # 端点不在本批实体里时整条丢弃：指向不存在实体的关系没有意义。
                continue
            if source.id == target.id:
                # 契约层会拒绝自环，这里先一步挡掉，免得整批保存失败。
                continue
            key = (source.id, item.type, target.id)
            existing = relations.get(key)
            if existing is None:
                relations[key] = _relation(
                    run, key, source.id, target.id, item.type, evidence, now
                )
            elif not set(evidence) <= set(existing.evidence_chunk_ids):
                relations[key] = replace(
                    existing,
                    evidence_chunk_ids=_union(existing.evidence_chunk_ids, evidence),
                )
        return tuple(entities.values()), tuple(relations.values())


def _index_by_name(
    entities: dict[tuple[str, str], CandidateEntity],
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """按规范化名称索引实体；同名不同类型的名称记为有歧义，不参与解析。"""
    by_name: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    for key in entities:
        if key[0] in by_name:
            ambiguous.add(key[0])
        else:
            by_name[key[0]] = key
    return by_name, ambiguous


def _endpoint(
    entities: dict[tuple[str, str], CandidateEntity],
    by_name: dict[str, tuple[str, str]],
    ambiguous: set[str],
    name: str,
) -> CandidateEntity | None:
    normalized = normalize_name(name)
    if normalized in ambiguous:
        return None
    key = by_name.get(normalized)
    return None if key is None else entities[key]


def _known_evidence(
    evidence_chunk_ids: tuple[str, ...], known_chunk_ids: frozenset[str]
) -> tuple[str, ...]:
    """只保留本次真的读到的 Chunk：模型编出来的 ID 在这里被丢掉。"""
    return tuple(
        chunk_id for chunk_id in evidence_chunk_ids if chunk_id in known_chunk_ids
    )


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return (*first, *(chunk_id for chunk_id in second if chunk_id not in first))


def _entity(
    run: ExtractionRun,
    key: tuple[str, str],
    name: str,
    entity_type: str,
    evidence: tuple[str, ...],
    now: str,
) -> CandidateEntity:
    return CandidateEntity(
        # ID 由「任务 + 规范化名称 + 类型」推出：同一次抽取里同名同类型只有
        # 一条记录，重跑同一份文档也不会得到两套互不相干的 ID。
        id=_candidate_id("cent", run.id, key[0], entity_type),
        extraction_run_id=run.id,
        workspace_id=run.workspace_id,
        document_id=run.document_id,
        document_version_id=run.document_version_id,
        adapter_id=run.adapter_id,
        name=name,
        normalized_name=key[0],
        type=entity_type,
        evidence_chunk_ids=evidence,
        created_at=now,
        updated_at=now,
    )


def _relation(
    run: ExtractionRun,
    key: tuple[str, str, str],
    source_id: str,
    target_id: str,
    relation_type: str,
    evidence: tuple[str, ...],
    now: str,
) -> CandidateRelation:
    return CandidateRelation(
        id=_candidate_id("crel", run.id, *key),
        extraction_run_id=run.id,
        workspace_id=run.workspace_id,
        document_id=run.document_id,
        document_version_id=run.document_version_id,
        adapter_id=run.adapter_id,
        source_entity_id=source_id,
        target_entity_id=target_id,
        type=relation_type,
        evidence_chunk_ids=evidence,
        created_at=now,
        updated_at=now,
    )


def _candidate_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:20]}"


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
