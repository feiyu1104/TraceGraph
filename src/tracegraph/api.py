import base64
import binascii
from collections.abc import Mapping
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
from time import perf_counter
import uuid

from fastapi import FastAPI, Query as QueryParam, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tracegraph import __version__
from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    DEFAULT_WORKSPACE_ID,
    MAX_HOPS,
    Answer,
    AnswerStatus,
    CandidateEntity,
    CandidateRelation,
    CandidateStatus,
    CandidateTally,
    Document,
    EvaluationCase,
    Evidence,
    Entity,
    ExtractionRun,
    Feedback,
    FeedbackKind,
    GraphPath,
    IngestionJob,
    Relation,
    Workspace,
)
from tracegraph.core.ports import (
    CandidateRepository,
    DocumentRepository,
    DomainAdapter,
    FeedbackRepository,
    GraphRepository,
    OriginalDocumentStore,
    Retriever,
)
from tracegraph.domains.medical.adapter import MedicalDomainAdapter
from tracegraph.domains.registry import (
    AdapterRegistry,
    UnknownAdapterError,
    build_default_adapter_registry,
)
from tracegraph.extraction.providers import ExtractionError
from tracegraph.extraction.service import ExtractionService
from tracegraph.feedback.service import FeedbackService
from tracegraph.feedback.storage import InMemoryFeedbackRepository
from tracegraph.generation.models import (
    ModelRegistry,
    UnknownGeneratorError,
    UnavailableGeneratorError,
    single_generator_registry,
)
from tracegraph.generation.providers import (
    AnswerGenerator,
    ExtractiveAnswerGenerator,
    relation_label,
)
from tracegraph.generation.service import AnswerService
from tracegraph.ingestion.service import (
    IngestionResult,
    OriginalConflictError,
    TextIngestionService,
)
from tracegraph.ingestion.lifecycle import (
    DocumentLifecycleService,
    PublishedGraphConflictError,
)
from tracegraph.ingestion.text import UnsupportedDocumentError
from tracegraph.observability import RequestMetrics
from tracegraph.publication.service import CandidatePublicationService, PublicationError
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.review.service import CandidateReviewError, CandidateReviewService
from tracegraph.storage.candidates import InMemoryCandidateRepository
from tracegraph.storage.memory import InMemoryDocumentRepository


# 未显式传入生成配置时应用按离线摘录运行，`/system` 也必须如实这么报，
# 否则「当前有没有接大模型」在默认构造路径上会变成空白。
_OFFLINE_GENERATION_STATUS = {
    "llm_configured": "false",
    "llm_model": "",
    "llm_fallback": "none",
}

# 上传体积上限，单位 MiB；可用 TRACEGRAPH_MAX_UPLOAD_MB 调整。
MAX_UPLOAD_ENV = "TRACEGRAPH_MAX_UPLOAD_MB"
DEFAULT_MAX_UPLOAD_MB = 10

# Content-Length 的余量：JSON 里除文件内容外还有文件名字段。
_UPLOAD_HEADER_SLACK = 65536


class ApiError(Exception):
    """带机器可读错误码的接口错误。

    `detail` 保持为字符串：既有调用方一直按字符串读它，改成对象会让它们失效。
    错误码作为同级字段追加，只增不改。
    """

    def __init__(self, status_code: int, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail


class TextIngestionRequest(BaseModel):
    source_name: str
    content: str
    # 不传即归入 default Workspace；服务端仍会复检它确实存在。
    workspace_id: str = DEFAULT_WORKSPACE_ID


class FileIngestionRequest(BaseModel):
    filename: str
    content_base64: str
    workspace_id: str = DEFAULT_WORKSPACE_ID


class RetrievalRequest(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=50)
    # 刻意不加 ge/le：越界的 max_hops 要返回 400 而不是 pydantic 的 422。
    max_hops: int = DEFAULT_MAX_HOPS
    # 不传即查默认 Workspace，既有前端因此不用改。
    workspace_id: str = DEFAULT_WORKSPACE_ID


class QueryRequest(BaseModel):
    question: str
    limit: int = Field(default=5, ge=1, le=20)
    max_hops: int = DEFAULT_MAX_HOPS
    # 不传表示用服务端默认模型；离线摘录固定为 "extractive"。
    generator_id: str | None = None
    # 适配器由服务端按这个 Workspace 的记录解析，请求里不接受 adapter_id。
    workspace_id: str = DEFAULT_WORKSPACE_ID


class WorkspaceCreateRequest(BaseModel):
    name: str
    # 必填：适配器决定这个 Workspace 按哪个领域组织知识。给一个「恰好是
    # 医疗」的默认值会让调用方以为这项选择是可选的，因此不给默认值。
    adapter_id: str


class FeedbackRequest(BaseModel):
    question: str
    answer_status: AnswerStatus
    kind: FeedbackKind
    evidence_ids: list[str] = Field(default_factory=list)
    comment: str | None = None


class PromoteFeedbackRequest(BaseModel):
    expected_status: AnswerStatus


class EntityReviewRequest(BaseModel):
    """单条候选实体的审核或内容修正；两者都不给就是一次纯粹的读取。

    `name` 与 `type` 只能改待审核的候选，`status` 只能走状态机里那四个迁移。
    """

    workspace_id: str = DEFAULT_WORKSPACE_ID
    status: CandidateStatus | None = None
    name: str | None = None
    type: str | None = None


class RelationReviewRequest(BaseModel):
    """单条候选关系的审核或内容修正；两端与 type 只能改待审核的候选。"""

    workspace_id: str = DEFAULT_WORKSPACE_ID
    status: CandidateStatus | None = None
    source_entity_id: str | None = None
    target_entity_id: str | None = None
    type: str | None = None


class BatchReviewRequest(BaseModel):
    """把一批候选改成同一个审核状态；整批要么全改、要么一条都不改。"""

    workspace_id: str = DEFAULT_WORKSPACE_ID
    status: CandidateStatus
    entity_ids: list[str] = Field(default_factory=list)
    relation_ids: list[str] = Field(default_factory=list)


class GraphPublicationRequest(BaseModel):
    """发布一批已批准的候选，或按 extraction_run_id 发布整次抽取。

    两种指法都支持：给了 run_id 就发布那次抽取里全部已批准的候选，
    否则发布显式列出的候选 ID。
    """

    candidate_entity_ids: list[str] = Field(default_factory=list)
    candidate_relation_ids: list[str] = Field(default_factory=list)
    extraction_run_id: str | None = None


class ExtractionRequest(BaseModel):
    """启动一次抽取。

    文档按 ID 或版本 ID 指定（给版本 ID 时不会顺带抽到这个文档的其他版本）；
    两者都不给则报 400。`model_id` 不传表示用服务端默认模型。
    """

    workspace_id: str = DEFAULT_WORKSPACE_ID
    document_id: str | None = None
    document_version_id: str | None = None
    model_id: str | None = None


def create_app(
    repository: DocumentRepository | None = None,
    retriever: Retriever | None = None,
    domain: DomainAdapter | None = None,
    feedback_repository: FeedbackRepository | None = None,
    graph_repository: GraphRepository | None = None,
    answer_generator: AnswerGenerator | None = None,
    fallback_answer_generator: AnswerGenerator | None = None,
    graph_status: Mapping[str, str] | None = None,
    generation_status: Mapping[str, str] | None = None,
    model_registry: ModelRegistry | None = None,
    original_store: OriginalDocumentStore | None = None,
    adapter_registry: AdapterRegistry | None = None,
    candidate_repository: CandidateRepository | None = None,
) -> FastAPI:
    document_repository = (
        repository if repository is not None else InMemoryDocumentRepository()
    )
    # 只有显式装配了原件存储，上传的原件才会落盘；测试与内存开发实例默认不留原件。
    ingestion_service = TextIngestionService(
        document_repository, original_store=original_store
    )
    active_retriever = retriever or KeywordRetriever(document_repository)
    active_domain = domain or MedicalDomainAdapter()
    # 适配器 ID 的合法值只有一个来源：注册表。API 层不再自己维护一份清单。
    known_adapters = adapter_registry or build_default_adapter_registry()
    answer_service = AnswerService(
        active_retriever,
        active_domain,
        answer_generator,
        graph_repository,
        fallback_answer_generator,
        model_registry,
    )
    max_upload_bytes = load_max_upload_bytes()
    active_feedback_repository = feedback_repository or InMemoryFeedbackRepository()
    feedback_service = FeedbackService(active_feedback_repository)
    # 没有模型注册表时退回「当前生成器就是唯一可选项」的退化形态：候选抽取
    # 因此不会因为缺少模型配置而整个不可用。
    active_models = model_registry or single_generator_registry(
        answer_generator or ExtractiveAnswerGenerator()
    )
    active_candidates = candidate_repository or InMemoryCandidateRepository()
    # 抽取服务只认识文档仓储、适配器注册表、候选仓储与模型注册表。它拿不到
    # 图仓储 —— 候选没有任何路径可以写进正式图谱。
    extraction_service = ExtractionService(
        document_repository, known_adapters, active_candidates, active_models
    )
    # 删除文档时一并清掉它的抽取任务与候选：候选的证据关联引用 Chunk，先删
    # 候选才轮得到 Chunk。
    lifecycle_service = DocumentLifecycleService(
        document_repository, graph_repository, original_store, active_candidates
    )
    # 审核服务拿不到图仓储：审核通过本身不会写图，发布是另一次显式调用。
    review_service = CandidateReviewService(
        document_repository, active_candidates, known_adapters
    )
    # 发布服务是候选表与图后端之间唯一的通路；没有图后端时整条路径不可用。
    publication_service = (
        CandidatePublicationService(
            document_repository, active_candidates, graph_repository
        )
        if graph_repository is not None
        else None
    )
    request_metrics = RequestMetrics()
    application = FastAPI(
        title="TraceGraph",
        summary="Evidence-first GraphRAG application framework",
        version=__version__,
    )

    @application.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.detail, "error_code": error.error_code},
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": jsonable_encoder(error.errors()),
                "error_code": "invalid_request",
            },
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request, error: Exception
    ) -> JSONResponse:
        # 只回一句固定文案：真实异常与堆栈留在服务端，不进网页。
        print(f"未处理异常 {request.method} {request.url.path}: {error!r}", file=sys.stderr)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务内部错误，请查看服务端日志。", "error_code": "internal_error"},
        )

    @application.middleware("http")
    async def observe_request(request: Request, call_next):
        request_id = f"req-{uuid.uuid4().hex}"
        started = perf_counter()
        oversized = _oversized_upload(request, max_upload_bytes)
        if oversized is not None:
            # 第一道闸门只按 Content-Length 判断，因此刻意设得宽松；
            # 精确判定在 handler 里按解码后的真实字节数复检。
            request_metrics.record(
                request.method, request.url.path, 413, (perf_counter() - started) * 1000
            )
            oversized.headers["X-Request-ID"] = request_id
            return oversized
        try:
            response = await call_next(request)
        except Exception:
            request_metrics.record(
                request.method,
                request.url.path,
                500,
                (perf_counter() - started) * 1000,
            )
            raise
        request_metrics.record(
            request.method,
            request.url.path,
            response.status_code,
            (perf_counter() - started) * 1000,
        )
        response.headers["X-Request-ID"] = request_id
        return response

    @application.get("/")
    def root() -> dict[str, str]:
        return {
            "name": "TraceGraph",
            "application": "/app",
            "documentation": "/docs",
            "health": "/healthz",
            "system": "/system",
            "graph_search": "/graph/entities?query=苯中毒",
        }

    # 前端构建产物由 FastAPI 直接托管，Python 侧不掺任何构建逻辑。
    # /app/assets 走静态文件，其余 /app/* 一律回 index.html，这样前端路由刷新不会 404。
    frontend_dist = _frontend_dist()
    assets = frontend_dist / "assets"
    if assets.is_dir():
        application.mount("/app/assets", StaticFiles(directory=assets), name="assets")

    @application.get("/app", response_class=HTMLResponse)
    @application.get("/app/{path:path}", response_class=HTMLResponse)
    def web_app(path: str = "") -> HTMLResponse:
        index = frontend_dist / "index.html"
        if not index.is_file():
            raise ApiError(
                503,
                "frontend_unavailable",
                "前端尚未构建：请先执行 npm --prefix frontend install "
                "与 npm --prefix frontend run build。",
            )
        return HTMLResponse(index.read_text(encoding="utf-8"))

    @application.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "TraceGraph", "version": __version__}

    @application.get("/system")
    def system() -> dict[str, object]:
        payload: dict[str, object] = {
            "version": __version__,
            "document_backend": type(document_repository).__name__,
            "graph_backend": getattr(graph_repository, "name", "disabled"),
            "retriever": active_retriever.name,
            "domain": active_domain.name,
            # 只透出已生效的字节数上限本身，不透出 TRACEGRAPH_MAX_UPLOAD_MB
            # 的取值形式；前端据此做上传前预检，服务端仍按真实字节数复检。
            "max_upload_bytes": max_upload_bytes,
        }
        # 降级事实必须能被看到：请求的后端与实际生效的后端在这里同时呈现。
        payload.update(graph_status or {})
        # 生成配置只透出不敏感的字段，base_url 与 api_key 一概不出现在这里。
        payload.update(generation_status or _OFFLINE_GENERATION_STATUS)
        # 实际装配了哪个生成器由运行时对象决定，配置值不覆盖它。这里报的是
        # 注册表 ID 而不是生成器类名：`/models` 与 `metrics.generator` 都用 ID，
        # 三处必须是同一个取值域，否则前端按 ID 反查显示名称会落空。
        payload["generator"] = answer_service.registry.id_of(answer_service.generator)
        return payload

    @application.get("/models")
    def models() -> dict[str, object]:
        """可选生成器清单。刻意不含 base_url：它可能带凭证。"""
        return answer_service.registry.describe()

    @application.get("/adapters")
    def adapters() -> dict[str, object]:
        """服务端内置的领域适配器清单；浏览器只能读，不能注册或修改。"""
        return known_adapters.describe()

    @application.post("/workspaces")
    def create_workspace(request: WorkspaceCreateRequest) -> dict[str, str]:
        name = request.name.strip()
        if not name:
            raise ApiError(400, "invalid_request", "name 不能为空。")
        adapter_id = request.adapter_id.strip()
        if not adapter_id:
            raise ApiError(400, "invalid_adapter", "adapter_id 不能为空。")
        try:
            # 建库时就解析一次：注册表里没有的 ID 不允许被写进 Workspace，
            # 否则查询时会卡在一个永远解析不出来的归属上。
            known_adapters.resolve(adapter_id)
        except UnknownAdapterError as error:
            raise ApiError(400, "invalid_adapter", str(error)) from error
        workspace = Workspace(
            id=f"ws-{uuid.uuid4().hex}",
            name=name,
            adapter_id=adapter_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        document_repository.save_workspace(workspace)
        return _workspace_response(workspace)

    @application.get("/workspaces")
    def list_workspaces() -> dict[str, object]:
        return {
            "workspaces": [
                _workspace_response(workspace)
                for workspace in document_repository.list_workspaces()
            ]
        }

    @application.get("/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str) -> dict[str, str]:
        workspace = document_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ApiError(404, "not_found", f"未找到 Workspace：{workspace_id}")
        return _workspace_response(workspace)

    @application.get("/metrics")
    def metrics() -> dict[str, object]:
        return request_metrics.snapshot()

    @application.post("/ingestions")
    def ingest_text(request: TextIngestionRequest) -> dict[str, object]:
        source_name = _source_name(request.source_name, "source_name")
        try:
            result = ingestion_service.ingest_text(
                source_name, request.content, request.workspace_id
            )
        except UnsupportedDocumentError as error:
            raise ApiError(400, "unsupported_document", str(error)) from error
        except OriginalConflictError as error:
            raise ApiError(409, "original_conflict", str(error)) from error
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return _ingestion_result_response(result)

    @application.post("/ingestions/file")
    def ingest_file(request: FileIngestionRequest) -> dict[str, object]:
        source_name = _source_name(request.filename, "filename")
        try:
            raw = base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ApiError(
                400, "invalid_request", "content_base64 不是合法的 Base64。"
            ) from error
        # 前端会先检查一次，但请求头与前端都不可信，这里必须按真实字节数复检。
        if len(raw) > max_upload_bytes:
            raise ApiError(413, "file_too_large", _too_large_detail(len(raw), max_upload_bytes))
        try:
            result = ingestion_service.ingest_bytes(
                source_name, raw, request.workspace_id
            )
        except UnsupportedDocumentError as error:
            raise ApiError(400, "unsupported_document", str(error)) from error
        except OriginalConflictError as error:
            raise ApiError(409, "original_conflict", str(error)) from error
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return _ingestion_result_response(result)

    @application.get("/documents/{document_id}/versions/{version_id}/original")
    def get_document_original(
        document_id: str, version_id: str
    ) -> dict[str, object]:
        """原件元信息。本批只报「保存在哪、多大、什么哈希」，不做浏览器下载。"""
        version = document_repository.get_version(version_id)
        if version is None or version.document_id != document_id:
            raise ApiError(404, "not_found", "文档版本不存在")
        if version.stored_path is None:
            raise ApiError(404, "original_not_stored", "该版本没有保存原件")
        return {
            "document_id": document_id,
            "version_id": version_id,
            "original_filename": version.original_filename,
            "original_sha256": version.original_sha256,
            "original_size": version.original_size,
            # 相对存储根目录的路径；绝对路径不出现在响应里。
            "stored_path": version.stored_path,
            "available": (
                original_store is not None and original_store.exists(version.stored_path)
            ),
        }

    @application.get("/ingestion-jobs/{job_id}")
    def get_ingestion_job(job_id: str) -> dict[str, object]:
        job = document_repository.get_ingestion_job(job_id)
        if job is None:
            raise ApiError(404, "not_found", "入库任务不存在")
        return _job_response(job)

    @application.delete("/documents/{document_id}")
    def delete_document(document_id: str) -> dict[str, object]:
        try:
            deleted_chunk_ids = lifecycle_service.delete(document_id)
        except KeyError as error:
            raise ApiError(404, "not_found", "文档不存在") from error
        except PublishedGraphConflictError as error:
            # 已经发布到图谱的文档不静默删除：图里的节点与边可能还被别的文档
            # 引用着，删掉证据会把它们变成半截知识。撤销发布是未来的功能。
            raise ApiError(409, error.error_code, str(error)) from error
        return {
            "document_id": document_id,
            "deleted_chunk_ids": list(deleted_chunk_ids),
        }

    @application.post("/retrieval/search")
    def retrieve(request: RetrievalRequest) -> dict[str, object]:
        _require_valid_max_hops(request.max_hops)
        workspace = _require_workspace(document_repository, request.workspace_id)
        adapter = _require_adapter(known_adapters, workspace)
        retriever, _ = _retrieval_route(
            active_retriever, document_repository, graph_repository
        )
        try:
            evidences = retriever.retrieve(
                request.query, request.limit, request.max_hops, workspace.id
            )
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return {
            "query": request.query,
            "workspace_id": workspace.id,
            "adapter_id": adapter.name,
            "retriever": retriever.name,
            "max_hops": request.max_hops,
            "evidences": [_evidence_response(evidence) for evidence in evidences],
        }

    @application.get("/graph/entities")
    def search_graph_entities(
        workspace_id: str,
        query: str,
        limit: int = QueryParam(default=10, ge=1, le=50),
    ) -> dict[str, object]:
        """在指定 Workspace 的图里按名称找实体。

        `workspace_id` 是必填的查询参数：缺失时 FastAPI 直接返回 422，不存在
        时返回 404 —— 两种情况都不会退回默认 Workspace 的数据。
        """
        _require_workspace(document_repository, workspace_id)
        graph = _require_graph(graph_repository)
        try:
            entities = graph.search_entities(query, workspace_id, limit)
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return {
            "query": query,
            "workspace_id": workspace_id,
            "entities": [_entity_response(entity) for entity in entities],
        }

    @application.get("/graph/entities/{entity_id}/relations")
    def graph_entity_relations(
        entity_id: str,
        workspace_id: str,
        limit: int = QueryParam(default=30, ge=1, le=100),
    ) -> dict[str, object]:
        _require_workspace(document_repository, workspace_id)
        graph = _require_graph(graph_repository)
        entity = graph.get_entity(entity_id, workspace_id)
        if entity is None:
            raise ApiError(404, "not_found", "图实体不存在")
        relations = graph.list_relations((entity_id,), workspace_id, limit)
        return {
            "workspace_id": workspace_id,
            "entity": _entity_response(entity),
            "relations": [
                _relation_response(
                    relation,
                    entity_id,
                    workspace_id,
                    graph,
                    document_repository,
                )
                for relation in relations
            ],
        }

    @application.get("/graph/relations/{relation_id}/evidence")
    def graph_relation_evidence(
        relation_id: str, workspace_id: str
    ) -> dict[str, object]:
        """按需返回某条关系溯源到的原文片段，供界面点击路径中的关系时懒加载。

        原文取自 SQLite 的 chunks：图里只存了 Chunk ID，正文从来没有第二份
        副本。
        """
        _require_workspace(document_repository, workspace_id)
        graph = _require_graph(graph_repository)
        relation = graph.get_relation(relation_id, workspace_id)
        if relation is None:
            raise ApiError(404, "not_found", "图关系不存在")
        source = graph.get_entity(relation.source_entity_id, workspace_id)
        target = graph.get_entity(relation.target_entity_id, workspace_id)
        return {
            "workspace_id": workspace_id,
            "relation": {
                "id": relation.id,
                "type": relation.type,
                "source": _entity_response(source) if source else None,
                "target": _entity_response(target) if target else None,
            },
            "evidence": _chunk_evidence(document_repository, relation, workspace_id),
            "sources": [
                _published_relation_source(source)
                for source in active_candidates.list_published_relations(
                    workspace_id, relation.id
                )
            ],
        }

    @application.post("/query")
    def query(request: QueryRequest) -> dict[str, object]:
        _require_valid_max_hops(request.max_hops)
        workspace = _require_workspace(document_repository, request.workspace_id)
        adapter = _require_adapter(known_adapters, workspace)
        retriever, request_graph = _retrieval_route(
            active_retriever, document_repository, graph_repository
        )
        # 装配时那个 AnswerService 从头到尾不改：本次请求的适配器与检索器装在
        # 一个只活在这次调用里的实例上，并发请求之间没有可互相覆盖的状态。
        service = answer_service.for_workspace(
            domain=adapter,
            retriever=retriever,
            graph_repository=request_graph,
            workspace_id=workspace.id,
        )
        try:
            answer = service.answer(
                request.question,
                request.limit,
                request.max_hops,
                request.generator_id,
            )
        except UnknownGeneratorError as error:
            raise ApiError(400, "invalid_generator", str(error)) from error
        except UnavailableGeneratorError as error:
            # 不可用绝不静默改用另一个在线模型，必须让请求方知道。
            raise ApiError(503, "generator_unavailable", str(error)) from error
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return _answer_response(
            answer,
            workspace_id=workspace.id,
            adapter_id=adapter.name,
            retriever=retriever.name,
        )

    @application.post("/extractions")
    def start_extraction(request: ExtractionRequest) -> dict[str, object]:
        """启动一次候选抽取。本批只产出候选，不写正式图谱。"""
        try:
            run = extraction_service.start(
                request.workspace_id,
                document_id=request.document_id,
                document_version_id=request.document_version_id,
                model_id=request.model_id,
            )
        except UnknownGeneratorError as error:
            raise ApiError(400, "invalid_generator", str(error)) from error
        except UnavailableGeneratorError as error:
            raise ApiError(503, "generator_unavailable", str(error)) from error
        except ExtractionError as error:
            raise ApiError(error.status_code, error.error_code, str(error)) from error
        return _run_response(run)

    @application.get("/extractions/{run_id}")
    def get_extraction(run_id: str) -> dict[str, object]:
        run = active_candidates.get_run(run_id)
        if run is None:
            raise ApiError(404, "not_found", f"未找到抽取任务：{run_id}")
        return _run_response(run)

    @application.get("/extractions/{run_id}/candidates")
    def get_extraction_candidates(run_id: str) -> dict[str, object]:
        """候选实体与候选关系，各自带上证据 Chunk 的原文片段。"""
        run = active_candidates.get_run(run_id)
        if run is None:
            raise ApiError(404, "not_found", f"未找到抽取任务：{run_id}")
        return _candidates_payload(
            run,
            active_candidates.list_entities(run_id),
            active_candidates.list_relations(run_id),
            document_repository,
        )

    @application.get("/workspaces/{workspace_id}/candidates")
    def list_workspace_candidates(
        workspace_id: str,
        document_id: str | None = QueryParam(default=None),
        status: CandidateStatus | None = QueryParam(default=None),
        entity_type: str | None = QueryParam(default=None),
        relation_type: str | None = QueryParam(default=None),
    ) -> dict[str, object]:
        """按文档、状态与类型筛选候选。

        Workspace 是硬条件：查询只在这个 Workspace 的候选里进行，别的
        Workspace 的候选一条也读不到。
        """
        workspace = _require_workspace(document_repository, workspace_id)
        return {
            "workspace_id": workspace.id,
            "entities": [
                _candidate_entity_response(entity, document_repository)
                for entity in active_candidates.list_workspace_entities(
                    workspace.id,
                    document_id=document_id,
                    status=status,
                    entity_type=entity_type,
                )
            ],
            "relations": [
                _candidate_relation_response(relation, document_repository)
                for relation in active_candidates.list_workspace_relations(
                    workspace.id,
                    document_id=document_id,
                    status=status,
                    relation_type=relation_type,
                )
            ],
        }

    @application.patch("/candidate-entities/{candidate_id}")
    def review_candidate_entity(
        candidate_id: str, request: EntityReviewRequest
    ) -> dict[str, object]:
        """审核或修正一条候选实体；两者都不给就是一次纯粹的读取。

        审核状态与发布状态是两回事：这里的 status 只在 pending / approved /
        rejected 之间走，已发布的候选既不能改内容也不能退回。
        """
        try:
            entity = review_service.review_entity(
                request.workspace_id,
                candidate_id,
                status=request.status,
                name=request.name,
                entity_type=request.type,
            )
        except CandidateReviewError as error:
            raise ApiError(error.status_code, error.error_code, str(error)) from error
        return _candidate_entity_response(entity, document_repository)

    @application.patch("/candidate-relations/{candidate_id}")
    def review_candidate_relation(
        candidate_id: str, request: RelationReviewRequest
    ) -> dict[str, object]:
        try:
            relation = review_service.review_relation(
                request.workspace_id,
                candidate_id,
                status=request.status,
                source_entity_id=request.source_entity_id,
                target_entity_id=request.target_entity_id,
                relation_type=request.type,
            )
        except CandidateReviewError as error:
            raise ApiError(error.status_code, error.error_code, str(error)) from error
        return _candidate_relation_response(relation, document_repository)

    @application.post("/candidates/batch-review")
    def batch_review_candidates(request: BatchReviewRequest) -> dict[str, object]:
        """批量审核：任一候选不存在、跨 Workspace 或状态非法时整批回滚。"""
        try:
            entities, relations = review_service.review_batch(
                request.workspace_id,
                entity_ids=tuple(request.entity_ids),
                relation_ids=tuple(request.relation_ids),
                status=request.status,
            )
        except CandidateReviewError as error:
            raise ApiError(error.status_code, error.error_code, str(error)) from error
        return {
            "workspace_id": request.workspace_id,
            "status": request.status.value,
            "entities": [
                _candidate_entity_response(entity, document_repository)
                for entity in entities
            ],
            "relations": [
                _candidate_relation_response(relation, document_repository)
                for relation in relations
            ],
        }

    @application.post("/workspaces/{workspace_id}/graph-publications")
    def publish_graph(
        workspace_id: str, request: GraphPublicationRequest
    ) -> dict[str, object]:
        """把已批准的候选发布到当前 Workspace 的图后端。

        图后端的类型由服务端配置决定：Neo4j 就写 Neo4j，SQLite 就写 SQLite
        图仓储 —— 两种后端在这里是同一条代码路径，语义因此不会分叉。
        """
        if publication_service is None:
            raise ApiError(503, "graph_unavailable", "图存储未启用，无法发布。")
        try:
            outcome = (
                publication_service.publish_run(workspace_id, request.extraction_run_id)
                if request.extraction_run_id is not None
                else publication_service.publish(
                    workspace_id,
                    entity_ids=tuple(request.candidate_entity_ids),
                    relation_ids=tuple(request.candidate_relation_ids),
                )
            )
        except PublicationError as error:
            raise ApiError(error.status_code, error.error_code, str(error)) from error
        return {
            "workspace_id": workspace_id,
            "backend": getattr(graph_repository, "name", "unknown"),
            "counts": outcome.to_counts(),
            "created_entity_ids": list(outcome.created_entity_ids),
            "reused_entity_ids": list(outcome.reused_entity_ids),
            "skipped_entity_ids": list(outcome.skipped_entity_ids),
            "created_relation_ids": list(outcome.created_relation_ids),
            "reused_relation_ids": list(outcome.reused_relation_ids),
            "skipped_relation_ids": list(outcome.skipped_relation_ids),
        }

    @application.get("/workspaces/{workspace_id}/documents")
    def list_workspace_documents(workspace_id: str) -> dict[str, object]:
        """该 Workspace 的文档清单，含最新版本、Chunk 数与候选计数。

        只列出这个 Workspace 的文档；候选计数一次取全，避免前端按文档逐个查。
        """
        workspace = _require_workspace(document_repository, workspace_id)
        documents = document_repository.list_documents(workspace.id)
        document_ids = tuple(document.id for document in documents)
        tallies = active_candidates.tally_documents(workspace.id, document_ids)
        with_runs = active_candidates.documents_with_runs(workspace.id, document_ids)
        return {
            "workspace_id": workspace.id,
            "documents": [
                _document_summary(document, document_repository, tallies, with_runs)
                for document in documents
            ],
        }

    @application.post("/feedback")
    def submit_feedback(request: FeedbackRequest) -> dict[str, object]:
        try:
            feedback = feedback_service.submit(
                question=request.question,
                answer_status=request.answer_status,
                kind=request.kind,
                evidence_ids=tuple(request.evidence_ids),
                comment=request.comment,
            )
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return _feedback_response(feedback)

    @application.get("/feedback")
    def list_feedback() -> list[dict[str, object]]:
        return [
            _feedback_response(feedback)
            for feedback in active_feedback_repository.list_feedback()
        ]

    @application.post("/feedback/{feedback_id}/promote")
    def promote_feedback(
        feedback_id: str, request: PromoteFeedbackRequest
    ) -> dict[str, object]:
        try:
            case = feedback_service.promote(feedback_id, request.expected_status)
        except KeyError as error:
            raise ApiError(404, "not_found", "反馈不存在") from error
        return _evaluation_case_response(case)

    return application


def _frontend_dist() -> Path:
    """前端构建产物目录；打包外置时可用环境变量覆盖。"""
    override = (os.getenv("TRACEGRAPH_FRONTEND_DIST") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def load_max_upload_bytes(environ: Mapping[str, str] | None = None) -> int:
    """上传体积上限；取值非法时启动即失败，不用一个悄悄生效的默认值糊过去。"""
    env = os.environ if environ is None else environ
    raw = (env.get(MAX_UPLOAD_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_UPLOAD_MB * 1024 * 1024
    try:
        megabytes = float(raw)
    except ValueError as error:
        raise ValueError(f"{MAX_UPLOAD_ENV} 必须是数字（单位 MiB）。") from error
    if megabytes <= 0:
        raise ValueError(f"{MAX_UPLOAD_ENV} 必须大于 0。")
    return int(megabytes * 1024 * 1024)


def _require_graph(graph: GraphRepository | None) -> GraphRepository:
    if graph is None:
        raise ApiError(503, "graph_unavailable", "图存储未启用")
    return graph


def _document_summary(
    document: Document,
    document_repository: DocumentRepository,
    tallies: dict[str, CandidateTally],
    documents_with_runs: frozenset[str],
) -> dict[str, object]:
    """文档列表的一行：元信息 + 最新版本 + Chunk 数 + 候选计数。"""
    versions = document_repository.list_versions(document.id)
    latest = max(versions, key=lambda version: version.number) if versions else None
    tally = tallies.get(document.id, CandidateTally())
    return {
        "id": document.id,
        "source_name": document.source_name,
        "media_type": document.media_type,
        # 时间对既有 DUTMed 文档是空的（迁移时无从追溯），空串统一报成 null。
        "created_at": document.created_at or None,
        "updated_at": document.updated_at or None,
        "latest_version_id": latest.id if latest else None,
        "latest_version_created_at": (latest.created_at or None) if latest else None,
        "chunk_count": (
            len(document_repository.list_chunks(latest.id)) if latest else 0
        ),
        # 有过抽取任务就算「存在」，哪怕它失败了或者一条候选都没产出 ——
        # 只报成功过的任务会让「抽过但没抽出东西」看起来像从没抽过。
        "has_extraction_runs": document.id in documents_with_runs,
        "candidates": {
            "pending": tally.pending,
            "approved": tally.approved,
            "rejected": tally.rejected,
            "published": tally.published,
            "total": tally.total,
        },
    }


def _require_workspace(
    repository: DocumentRepository, workspace_id: str
) -> Workspace:
    workspace = repository.get_workspace(workspace_id)
    if workspace is None:
        raise ApiError(404, "workspace_not_found", f"未找到 Workspace：{workspace_id}")
    return workspace


def _require_adapter(registry: AdapterRegistry, workspace: Workspace) -> DomainAdapter:
    """按 Workspace 记录解析适配器。

    客户端不能直接指定适配器：它只能选 Workspace，用哪个领域适配器是那份
    记录自己说了算。注册表里已经没有这个 ID 时明确报错，不悄悄退回医疗。
    """
    try:
        return registry.resolve(workspace.adapter_id)
    except UnknownAdapterError as error:
        raise ApiError(409, "workspace_adapter_unavailable", str(error)) from error


def _retrieval_route(
    retriever: Retriever,
    repository: DocumentRepository,
    graph: GraphRepository | None,
) -> tuple[Retriever, GraphRepository | None]:
    """本次请求走哪条检索链路。

    图索引已经按 Workspace 隔离，任何 Workspace 都可以走装配时那条混合检索：
    图仓储的每次查询都由调用方显式带上本次请求的 workspace_id，别的 Workspace
    的节点、关系与证据进不了结果集，因此不再需要按 Workspace 或适配器把图
    检索整个关掉。

    图后端不可用时退回本 Workspace 的关键词检索：还没有图数据的 Workspace
    照样能问答，而不是报错。
    """
    if graph is None:
        return KeywordRetriever(repository), None
    return retriever, graph


def _source_name(filename: str, field: str) -> str:
    """上传的文件名只作为来源名称。

    剥掉任何目录成分：这个名字会被存进文档元数据并回显在界面上，
    绝不允许它参与服务器路径拼接。
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        raise ApiError(400, "invalid_request", f"{field} 不能为空")
    return name


def _too_large_detail(size: int, limit: int) -> str:
    return (
        f"文件 {size / 1024 / 1024:.1f} MiB 超过 "
        f"{limit / 1024 / 1024:.0f} MiB 上限。"
    )


def _oversized_upload(request: Request, limit: int) -> JSONResponse | None:
    """按 Content-Length 提前拒绝明显超限的上传。

    base64 最多把字节数放大到 4/3，因此用 `length * 3 // 4` 反推原文下界；
    只有下界加余量都仍然超限时才拒绝，宁可不拦也不误杀。
    """
    if request.url.path != "/ingestions/file":
        return None
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError:
        return None
    if length * 3 // 4 <= limit + _UPLOAD_HEADER_SLACK:
        return None
    return JSONResponse(
        status_code=413,
        content={
            "detail": f"上传内容超过 {limit / 1024 / 1024:.0f} MiB 上限。",
            "error_code": "file_too_large",
        },
    )


def _ingestion_result_response(result: IngestionResult) -> dict[str, object]:
    return {
        "job": _job_response(result.job),
        "document": {
            "id": result.document.id,
            "source_name": result.document.source_name,
            "media_type": result.document.media_type,
        },
        "version": {
            "id": result.version.id,
            "number": result.version.number,
            "content_sha256": result.version.content_sha256,
            # 没有保存原件的版本（以及既有 DUTMed 数据）这四个字段整体为 null。
            "original_filename": result.version.original_filename,
            "original_sha256": result.version.original_sha256,
            "original_size": result.version.original_size,
            "stored_path": result.version.stored_path,
        },
        "chunks": [
            {"id": chunk.id, "index": chunk.index, "locator": chunk.locator}
            for chunk in result.chunks
        ],
    }


def _job_response(job: IngestionJob) -> dict[str, object]:
    return {
        "id": job.id,
        "document_id": job.document_id,
        "status": job.status.value,
        "processed_chunks": job.processed_chunks,
        "error": job.error,
    }


def _require_valid_max_hops(max_hops: int) -> None:
    if max_hops < 1 or max_hops > MAX_HOPS:
        raise ApiError(
            400, "invalid_request", f"max_hops 必须在 1 到 {MAX_HOPS} 之间"
        )


def _chunk_evidence(
    document_repository: DocumentRepository,
    relation: Relation,
    workspace_id: str,
) -> list[dict[str, object]]:
    """把关系的证据 chunk ID 解析成可阅读的原文片段。

    图侧已经按 Workspace 过滤过一次，这里再按证据所属文档复检一次：图和文档
    是两套存储，一条归属正确的关系仍可能引用到别的 Workspace 的 Chunk。
    """
    resolved = []
    for chunk_id in relation.evidence_chunk_ids:
        chunk = document_repository.get_chunk(chunk_id)
        if chunk is None:
            continue
        document = document_repository.get_document(chunk.document_id)
        if document is None or document.workspace_id != workspace_id:
            continue
        resolved.append(
            {
                "chunk_id": chunk.id,
                "content": chunk.content,
                "document_id": document.id,
                "document_version_id": chunk.document_version_id,
                "source_name": document.source_name,
                "locator": chunk.locator,
            }
        )
    return resolved


def _run_response(run: ExtractionRun) -> dict[str, object]:
    return {
        "id": run.id,
        "workspace_id": run.workspace_id,
        "document_id": run.document_id,
        "document_version_id": run.document_version_id,
        "adapter_id": run.adapter_id,
        "model_id": run.model_id,
        "status": run.status.value,
        "entity_count": run.entity_count,
        "relation_count": run.relation_count,
        "error": run.error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _candidate_evidence(
    document_repository: DocumentRepository, chunk_ids: tuple[str, ...]
) -> list[dict[str, object]]:
    """候选的证据 Chunk 原文。与关系证据同一种形状，前端不必维护两套渲染。"""
    resolved = []
    for chunk_id in chunk_ids:
        chunk = document_repository.get_chunk(chunk_id)
        if chunk is None:
            continue
        document = document_repository.get_document(chunk.document_id)
        resolved.append(
            {
                "chunk_id": chunk.id,
                "content": chunk.content,
                "source_name": document.source_name if document else "",
                "locator": chunk.locator,
            }
        )
    return resolved


def _candidate_entity_response(
    entity: CandidateEntity, document_repository: DocumentRepository
) -> dict[str, object]:
    return {
        "id": entity.id,
        "extraction_run_id": entity.extraction_run_id,
        "workspace_id": entity.workspace_id,
        "document_id": entity.document_id,
        "document_version_id": entity.document_version_id,
        "adapter_id": entity.adapter_id,
        "name": entity.name,
        "type": entity.type,
        "status": entity.status.value,
        "is_published": entity.is_published,
        "published_at": entity.published_at,
        "graph_id": entity.graph_id,
        "evidence": _candidate_evidence(
            document_repository, entity.evidence_chunk_ids
        ),
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
    }


def _candidate_relation_response(
    relation: CandidateRelation, document_repository: DocumentRepository
) -> dict[str, object]:
    return {
        "id": relation.id,
        "extraction_run_id": relation.extraction_run_id,
        "workspace_id": relation.workspace_id,
        "document_id": relation.document_id,
        "document_version_id": relation.document_version_id,
        "adapter_id": relation.adapter_id,
        "source_entity_id": relation.source_entity_id,
        "target_entity_id": relation.target_entity_id,
        "type": relation.type,
        "status": relation.status.value,
        "is_published": relation.is_published,
        "published_at": relation.published_at,
        "graph_id": relation.graph_id,
        "evidence": _candidate_evidence(
            document_repository, relation.evidence_chunk_ids
        ),
        "created_at": relation.created_at,
        "updated_at": relation.updated_at,
    }


def _published_relation_source(relation: CandidateRelation) -> dict[str, object]:
    """图关系对应的候选来源；正文仍通过 evidence 从 SQLite Chunk 获取。"""
    return {
        "candidate_relation_id": relation.id,
        "extraction_run_id": relation.extraction_run_id,
        "workspace_id": relation.workspace_id,
        "document_id": relation.document_id,
        "document_version_id": relation.document_version_id,
        "evidence_chunk_ids": list(relation.evidence_chunk_ids),
        "published_at": relation.published_at,
    }


def _candidates_payload(
    run: ExtractionRun,
    entities: tuple[CandidateEntity, ...],
    relations: tuple[CandidateRelation, ...],
    document_repository: DocumentRepository,
) -> dict[str, object]:
    return {
        "run": _run_response(run),
        "entities": [
            _candidate_entity_response(entity, document_repository)
            for entity in entities
        ],
        "relations": [
            _candidate_relation_response(relation, document_repository)
            for relation in relations
        ],
    }


def _graph_path_response(path: GraphPath | None) -> dict[str, object] | None:
    """完整的带方向路径：节点序列 + 逐跳关系 + 截断提示。"""
    if path is None:
        return None
    return {
        "nodes": [_entity_response(node) for node in path.nodes],
        "steps": [
            {
                "relation_id": step.relation.id,
                "type": step.relation.type,
                # 中文关系名只在这里定义一次，前端直接渲染，不再维护第二份映射表。
                "label": relation_label(step.relation.type),
                "direction": step.direction.value,
                "target": _entity_response(step.target),
                "evidence_chunk_ids": list(step.relation.evidence_chunk_ids),
            }
            for step in path.steps
        ],
        "truncations": [
            {
                "entity_id": truncation.entity_id,
                "total_edges": truncation.total_edges,
                "shown_edges": truncation.shown_edges,
            }
            for truncation in path.truncations
        ],
    }


def _evidence_response(evidence: Evidence) -> dict[str, object]:
    return {
        "id": evidence.id,
        "content": evidence.content,
        "document_id": evidence.document_id,
        "document_version": evidence.document_version,
        "source_name": evidence.source_name,
        "locator": evidence.locator,
        "chunk_id": evidence.chunk_id,
        "retrieval_method": evidence.retrieval_method,
        "retrieval_score": evidence.retrieval_score,
        "graph_path": _graph_path_response(evidence.graph_path),
    }


def _entity_response(entity: Entity) -> dict[str, str]:
    return {"id": entity.id, "name": entity.name, "type": entity.type}


def _workspace_response(workspace: Workspace) -> dict[str, str]:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "adapter_id": workspace.adapter_id,
        "created_at": workspace.created_at,
    }


def _relation_response(
    relation: Relation,
    center_entity_id: str,
    workspace_id: str,
    graph_repository: GraphRepository,
    document_repository: DocumentRepository,
) -> dict[str, object]:
    source = graph_repository.get_entity(relation.source_entity_id, workspace_id)
    target = graph_repository.get_entity(relation.target_entity_id, workspace_id)
    evidence = []
    for chunk_id in relation.evidence_chunk_ids:
        chunk = document_repository.get_chunk(chunk_id)
        if chunk is None:
            continue
        document = document_repository.get_document(chunk.document_id)
        if document is None or document.workspace_id != workspace_id:
            continue
        evidence.append(
            {
                "chunk_id": chunk.id,
                "source_name": document.source_name,
                "locator": chunk.locator,
            }
        )
    return {
        "id": relation.id,
        "type": relation.type,
        "label": relation_label(relation.type),
        "direction": (
            "outgoing" if relation.source_entity_id == center_entity_id else "incoming"
        ),
        "source": _entity_response(source) if source else None,
        "target": _entity_response(target) if target else None,
        "evidence": evidence,
    }


def _answer_response(
    answer: Answer, *, workspace_id: str, adapter_id: str, retriever: str
) -> dict[str, object]:
    return {
        "status": answer.status.value,
        "text": answer.text,
        "error_code": answer.error_code,
        "claims": [
            {"text": claim.text, "evidence_ids": list(claim.evidence_ids)}
            for claim in answer.claims
        ],
        "derived_associations": [
            {"text": association.text, "evidence_ids": list(association.evidence_ids)}
            for association in answer.derived_associations
        ],
        "evidences": [_evidence_response(evidence) for evidence in answer.evidences],
        "warnings": list(answer.warnings),
        # 本次回答用了哪个知识库、哪个领域适配器、哪条检索链路。这三种状态
        # （答出、证据不足、证据冲突）都带上，前端不必按 status 分支。
        "metrics": {
            **answer.metrics,
            "workspace_id": workspace_id,
            "adapter_id": adapter_id,
            "retriever": retriever,
        },
    }


def _feedback_response(feedback: Feedback) -> dict[str, object]:
    return {
        "id": feedback.id,
        "question": feedback.question,
        "answer_status": feedback.answer_status.value,
        "kind": feedback.kind.value,
        "evidence_ids": list(feedback.evidence_ids),
        "comment": feedback.comment,
        "created_at": feedback.created_at,
    }


def _evaluation_case_response(case: EvaluationCase) -> dict[str, object]:
    return {
        "id": case.id,
        "question": case.question,
        "expected_status": case.expected_status.value,
        "required_evidence_ids": list(case.required_evidence_ids),
        "tags": list(case.tags),
        "source": case.source,
    }



app = create_app()
