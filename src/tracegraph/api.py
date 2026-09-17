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
    DEFAULT_WORKSPACE_ADAPTER_ID,
    DEFAULT_WORKSPACE_ID,
    MAX_HOPS,
    Answer,
    AnswerStatus,
    EvaluationCase,
    Evidence,
    Entity,
    Feedback,
    FeedbackKind,
    GraphPath,
    IngestionJob,
    Relation,
    Workspace,
)
from tracegraph.core.ports import (
    DocumentRepository,
    DomainAdapter,
    FeedbackRepository,
    GraphRepository,
    OriginalDocumentStore,
    Retriever,
)
from tracegraph.domains.medical.adapter import MedicalDomainAdapter
from tracegraph.feedback.service import FeedbackService
from tracegraph.feedback.storage import InMemoryFeedbackRepository
from tracegraph.generation.models import (
    ModelRegistry,
    UnknownGeneratorError,
    UnavailableGeneratorError,
)
from tracegraph.generation.providers import AnswerGenerator, relation_label
from tracegraph.generation.service import AnswerService
from tracegraph.ingestion.service import IngestionResult, TextIngestionService
from tracegraph.ingestion.lifecycle import DocumentLifecycleService
from tracegraph.ingestion.text import UnsupportedDocumentError
from tracegraph.observability import RequestMetrics
from tracegraph.retrieval.keyword import KeywordRetriever
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


class QueryRequest(BaseModel):
    question: str
    limit: int = Field(default=5, ge=1, le=20)
    max_hops: int = DEFAULT_MAX_HOPS
    # 不传表示用服务端默认模型；离线摘录固定为 "extractive"。
    generator_id: str | None = None


class WorkspaceCreateRequest(BaseModel):
    name: str
    # 当前只有一个领域适配器，缺省即医疗；本阶段不做适配器动态加载。
    adapter_id: str = DEFAULT_WORKSPACE_ADAPTER_ID


class FeedbackRequest(BaseModel):
    question: str
    answer_status: AnswerStatus
    kind: FeedbackKind
    evidence_ids: list[str] = Field(default_factory=list)
    comment: str | None = None


class PromoteFeedbackRequest(BaseModel):
    expected_status: AnswerStatus


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
    lifecycle_service = DocumentLifecycleService(
        document_repository, graph_repository, original_store
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

    @application.post("/workspaces")
    def create_workspace(request: WorkspaceCreateRequest) -> dict[str, str]:
        name = request.name.strip()
        if not name:
            raise ApiError(400, "invalid_request", "name 不能为空。")
        workspace = Workspace(
            id=f"ws-{uuid.uuid4().hex}",
            name=name,
            adapter_id=request.adapter_id.strip() or DEFAULT_WORKSPACE_ADAPTER_ID,
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
        return {
            "document_id": document_id,
            "deleted_chunk_ids": list(deleted_chunk_ids),
        }

    @application.post("/retrieval/search")
    def retrieve(request: RetrievalRequest) -> dict[str, object]:
        _require_valid_max_hops(request.max_hops)
        try:
            evidences = active_retriever.retrieve(
                request.query, request.limit, request.max_hops
            )
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return {
            "query": request.query,
            "max_hops": request.max_hops,
            "evidences": [_evidence_response(evidence) for evidence in evidences],
        }

    @application.get("/graph/entities")
    def search_graph_entities(
        query: str,
        limit: int = QueryParam(default=10, ge=1, le=50),
    ) -> dict[str, object]:
        if graph_repository is None:
            raise ApiError(503, "graph_unavailable", "图存储未启用")
        try:
            entities = graph_repository.search_entities(query, limit)
        except ValueError as error:
            raise ApiError(400, "invalid_request", str(error)) from error
        return {
            "query": query,
            "entities": [_entity_response(entity) for entity in entities],
        }

    @application.get("/graph/entities/{entity_id}/relations")
    def graph_entity_relations(
        entity_id: str,
        limit: int = QueryParam(default=30, ge=1, le=100),
    ) -> dict[str, object]:
        if graph_repository is None:
            raise ApiError(503, "graph_unavailable", "图存储未启用")
        entity = graph_repository.get_entity(entity_id)
        if entity is None:
            raise ApiError(404, "not_found", "图实体不存在")
        relations = graph_repository.list_relations((entity_id,), limit)
        return {
            "entity": _entity_response(entity),
            "relations": [
                _relation_response(
                    relation,
                    entity_id,
                    graph_repository,
                    document_repository,
                )
                for relation in relations
            ],
        }

    @application.get("/graph/relations/{relation_id}/evidence")
    def graph_relation_evidence(relation_id: str) -> dict[str, object]:
        """按需返回某条关系溯源到的原文片段，供界面点击路径中的关系时懒加载。"""
        if graph_repository is None:
            raise ApiError(503, "graph_unavailable", "图存储未启用")
        relation = graph_repository.get_relation(relation_id)
        if relation is None:
            raise ApiError(404, "not_found", "图关系不存在")
        source = graph_repository.get_entity(relation.source_entity_id)
        target = graph_repository.get_entity(relation.target_entity_id)
        return {
            "relation": {
                "id": relation.id,
                "type": relation.type,
                "source": _entity_response(source) if source else None,
                "target": _entity_response(target) if target else None,
            },
            "evidence": _chunk_evidence(document_repository, relation),
        }

    @application.post("/query")
    def query(request: QueryRequest) -> dict[str, object]:
        _require_valid_max_hops(request.max_hops)
        try:
            answer = answer_service.answer(
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
        return _answer_response(answer)

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
    document_repository: DocumentRepository, relation: Relation
) -> list[dict[str, object]]:
    """把关系的证据 chunk ID 解析成可阅读的 DUTMed 原文片段。"""
    resolved = []
    for chunk_id in relation.evidence_chunk_ids:
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
    graph_repository: GraphRepository,
    document_repository: DocumentRepository,
) -> dict[str, object]:
    source = graph_repository.get_entity(relation.source_entity_id)
    target = graph_repository.get_entity(relation.target_entity_id)
    evidence = []
    for chunk_id in relation.evidence_chunk_ids:
        chunk = document_repository.get_chunk(chunk_id)
        if chunk is None:
            continue
        document = document_repository.get_document(chunk.document_id)
        evidence.append(
            {
                "chunk_id": chunk.id,
                "source_name": document.source_name if document else "",
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


def _answer_response(answer: Answer) -> dict[str, object]:
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
        "metrics": dict(answer.metrics),
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
