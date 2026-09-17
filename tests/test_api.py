import base64
import json

import anyio
import httpx2
import pytest

from tracegraph.api import app, create_app, load_max_upload_bytes
from tracegraph.core.contracts import Entity, Relation
from tracegraph.generation.models import load_model_registry
from tracegraph.generation.providers import GenerationNetworkError
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.retrieval.graph import GraphRetriever
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository


async def get(path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def post(application, path: str, payload: dict) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=payload)


async def ingest(application, source_name: str, content: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/ingestions",
            json={"source_name": source_name, "content": content},
        )


async def get_from(application, path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def upload(application, filename: str, raw: bytes) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/ingestions/file",
            json={
                "filename": filename,
                "content_base64": base64.b64encode(raw).decode("ascii"),
            },
        )


def test_health_endpoint() -> None:
    response = anyio.run(get, "/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "TraceGraph",
        "version": "0.1.0",
    }


def test_ingestion_job_can_be_queried() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(ingest, application, "指南.md", "# 高血压\n\n应定期监测血压。")
    job = response.json()["job"]
    queried = anyio.run(get_from, application, f"/ingestion-jobs/{job['id']}")

    assert response.status_code == 200
    assert job["status"] == "succeeded"
    assert queried.status_code == 200
    assert queried.json() == job


def test_duplicate_ingestion_creates_queryable_skipped_job() -> None:
    application = create_app(InMemoryDocumentRepository())
    content = "# 高血压\n\n应定期监测血压。"

    anyio.run(ingest, application, "指南.md", content)
    duplicate = anyio.run(ingest, application, "指南.md", content)
    job = duplicate.json()["job"]
    queried = anyio.run(get_from, application, f"/ingestion-jobs/{job['id']}")

    assert job["status"] == "skipped"
    assert queried.json() == job


def test_unsupported_ingestion_is_rejected() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(ingest, application, "报告.docx", "not a DOCX")

    assert response.status_code == 400
    assert "仅支持" in response.json()["detail"]


def test_unknown_ingestion_job_returns_not_found() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(get_from, application, "/ingestion-jobs/missing")

    assert response.status_code == 404


def _graph_stack():
    """百日咳 → 琥乙红霉素片 ← 小儿支原体肺炎 的最小可查询图谱。"""
    documents = InMemoryDocumentRepository()
    ingestion = TextIngestionService(documents)
    chunks = {
        "r1": ingestion.ingest_text(
            "dutmed-百日咳-推荐药物.md", "百日咳的推荐药物包括琥乙红霉素片。"
        ).chunks[0].id,
        "r2": ingestion.ingest_text(
            "dutmed-小儿支原体肺炎-推荐药物.md",
            "小儿支原体肺炎的推荐药物包括琥乙红霉素片。",
        ).chunks[0].id,
    }
    graph = InMemoryGraphRepository()
    graph.upsert_entity(Entity("d1", "百日咳", "Disease"))
    graph.upsert_entity(Entity("d2", "小儿支原体肺炎", "Disease"))
    graph.upsert_entity(Entity("m1", "琥乙红霉素片", "Drug"))
    graph.upsert_relation(Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", (chunks["r1"],)))
    graph.upsert_relation(Relation("r2", "d2", "m1", "RECOMMENDS_DRUG", (chunks["r2"],)))
    return documents, GraphRetriever(documents, graph), graph


def _graph_app():
    documents, retriever, graph = _graph_stack()
    return create_app(documents, retriever, graph_repository=graph)


def test_query_exposes_the_full_directed_graph_path() -> None:
    application = _graph_app()

    response = anyio.run(
        post, application, "/query", {"question": "百日咳用什么药", "max_hops": 2}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "answered"
    assert body["metrics"]["max_hops"] == 2
    assert body["derived_associations"]

    multi_hop = [item for item in body["evidences"] if len(item["graph_path"]["steps"]) > 1]
    assert multi_hop
    path = multi_hop[0]["graph_path"]
    assert [node["name"] for node in path["nodes"]] == [
        "百日咳",
        "琥乙红霉素片",
        "小儿支原体肺炎",
    ]
    assert [step["relation_id"] for step in path["steps"]] == ["r1", "r2"]
    assert [step["direction"] for step in path["steps"]] == ["outgoing", "incoming"]
    assert path["steps"][0]["evidence_chunk_ids"]
    assert isinstance(path["truncations"], list)


def test_keyword_only_evidence_has_no_graph_path() -> None:
    application = create_app(InMemoryDocumentRepository())
    anyio.run(ingest, application, "指南.md", "# 检查\n\n高血压患者应定期监测血压。")

    response = anyio.run(
        post, application, "/retrieval/search", {"query": "高血压监测", "max_hops": 1}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["max_hops"] == 1
    assert body["evidences"][0]["graph_path"] is None


def test_query_accepts_the_web_ui_hop_payload() -> None:
    application = _graph_app()

    response = anyio.run(
        post, application, "/query", {"question": "百日咳用什么药", "max_hops": 3}
    )

    assert response.status_code == 200
    assert response.json()["metrics"]["max_hops"] == 3


def test_relation_evidence_endpoint_returns_source_text() -> None:
    application = _graph_app()

    response = anyio.run(
        get_from, application, "/graph/relations/r1/evidence"
    )
    body = response.json()

    assert response.status_code == 200
    assert body["relation"]["type"] == "RECOMMENDS_DRUG"
    assert body["relation"]["source"]["name"] == "百日咳"
    assert body["evidence"][0]["source_name"] == "dutmed-百日咳-推荐药物.md"


def test_unknown_relation_evidence_returns_not_found() -> None:
    application = _graph_app()

    response = anyio.run(get_from, application, "/graph/relations/missing/evidence")

    assert response.status_code == 404


def test_graph_endpoints_are_unavailable_without_a_graph_backend() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(get_from, application, "/graph/relations/r1/evidence")

    assert response.status_code == 503


def test_out_of_range_max_hops_is_rejected_with_400() -> None:
    application = _graph_app()

    for path, payload in (
        ("/query", {"question": "百日咳用什么药", "max_hops": 4}),
        ("/query", {"question": "百日咳用什么药", "max_hops": 0}),
        ("/retrieval/search", {"query": "百日咳", "max_hops": 4}),
    ):
        response = anyio.run(post, application, path, payload)
        assert response.status_code == 400, (path, payload)
        assert "max_hops" in response.json()["detail"]


def test_errors_carry_a_machine_readable_code() -> None:
    application = _graph_app()

    # 参数错误：detail 仍是字符串，error_code 作为同级字段追加。
    rejected = anyio.run(
        post, application, "/query", {"question": "百日咳用什么药", "max_hops": 9}
    )
    assert rejected.json()["error_code"] == "invalid_request"
    assert isinstance(rejected.json()["detail"], str)

    # 缺少图后端。
    without_graph = create_app(InMemoryDocumentRepository())
    unavailable = anyio.run(get_from, without_graph, "/graph/entities?query=苯中毒")
    assert unavailable.status_code == 503
    assert unavailable.json()["error_code"] == "graph_unavailable"

    # 不存在的资源。
    missing = anyio.run(get_from, application, "/graph/relations/missing/evidence")
    assert missing.json()["error_code"] == "not_found"


def test_query_reports_the_generator_that_produced_the_answer() -> None:
    application = _graph_app()

    body = anyio.run(
        post, application, "/query", {"question": "百日咳用什么药", "max_hops": 2}
    ).json()

    assert body["error_code"] is None
    assert body["metrics"]["generator"] == "extractive"
    assert body["metrics"]["generation_degraded"] is False


class _BrokenGenerator:
    name = "openai-compatible"

    def generate(self, question, evidences):
        raise GenerationNetworkError("http://gateway.internal/v1 拒绝连接")


def test_generation_failure_is_reported_without_leaking_details() -> None:
    documents, retriever, graph = _graph_stack()
    application = create_app(
        documents,
        retriever,
        graph_repository=graph,
        answer_generator=_BrokenGenerator(),
        generation_status={
            "generator": "openai-compatible",
            "llm_configured": "true",
            "llm_model": "some-model",
            "llm_fallback": "none",
        },
    )

    response = anyio.run(
        post, application, "/query", {"question": "百日咳用什么药", "max_hops": 1}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "system_error"
    assert body["error_code"] == "generation_network_error"
    assert body["claims"] == []
    # 上游地址与异常细节只留在服务端。
    assert "gateway.internal" not in response.text


def test_system_reports_the_offline_generator_by_default() -> None:
    body = anyio.run(get_from, create_app(InMemoryDocumentRepository()), "/system").json()

    assert body["generator"] == "extractive"
    assert body["llm_configured"] == "false"
    assert body["llm_model"] == ""
    assert body["llm_fallback"] == "none"


def test_system_reports_the_configured_model_without_credentials() -> None:
    application = create_app(
        InMemoryDocumentRepository(),
        generation_status={
            "generator": "openai-compatible",
            "llm_configured": "true",
            "llm_model": "some-model",
            "llm_fallback": "extractive",
        },
    )

    body = anyio.run(get_from, application, "/system").json()

    assert body["generator"] == "extractive"
    assert body["llm_configured"] == "true"
    assert body["llm_model"] == "some-model"
    assert body["llm_fallback"] == "extractive"
    assert "api_key" not in " ".join(body)


def test_system_reports_the_default_model_id_not_the_generator_class(tmp_path) -> None:
    # `/models` 与 `metrics.generator` 都用注册表 ID；`/system` 若报生成器类名，
    # 前端按 ID 反查显示名称就会落空。
    application = create_app(
        InMemoryDocumentRepository(), model_registry=_model_registry(tmp_path)
    )

    body = anyio.run(get_from, application, "/system").json()

    assert body["generator"] == "main"
    assert body["generator"] != "openai-compatible"


def test_frontend_bundle_is_served_with_a_history_fallback(
    tmp_path, monkeypatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>TraceGraph</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export {};", encoding="utf-8")
    monkeypatch.setenv("TRACEGRAPH_FRONTEND_DIST", str(dist))
    application = create_app(InMemoryDocumentRepository())

    index = anyio.run(get_from, application, "/app")
    deep_link = anyio.run(get_from, application, "/app/graph/anything")
    asset = anyio.run(get_from, application, "/app/assets/app.js")

    assert index.status_code == 200
    assert "TraceGraph" in index.text
    # 前端路由刷新不能 404。
    assert deep_link.status_code == 200
    assert deep_link.text == index.text
    assert asset.status_code == 200
    assert asset.text == "export {};"


def _model_registry(tmp_path, default: str = "main", **overrides: object):
    """一份最小的两模型配置，密钥只从传入的环境映射里取。"""
    entry = {
        "id": "main",
        "label": "主模型",
        "base_url": "https://gateway.example.com/v1",
        "model": "main-name",
        "api_key_env": "MAIN_MODEL_KEY",
    }
    entry.update(overrides)
    path = tmp_path / "models.local.json"
    path.write_text(
        json.dumps({"default": default, "models": [entry]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return load_model_registry({"MAIN_MODEL_KEY": "sk-secret"}, path)


def test_models_endpoint_lists_extractive_and_hides_the_gateway(tmp_path) -> None:
    application = create_app(
        InMemoryDocumentRepository(), model_registry=_model_registry(tmp_path)
    )

    response = anyio.run(get_from, application, "/models")
    body = response.json()

    assert response.status_code == 200
    assert body["default"] == "main"
    assert [entry["id"] for entry in body["models"]] == ["extractive", "main"]
    assert body["models"][1] == {
        "id": "main",
        "label": "主模型",
        "model": "main-name",
        "available": True,
        "kind": "openai-compatible",
        "reason": "",
    }
    # 网关地址与密钥都不在响应里，连字段名都不出现。
    assert "base_url" not in response.text
    assert "gateway.example.com" not in response.text
    assert "sk-secret" not in response.text
    assert "MAIN_MODEL_KEY" not in response.text


def test_models_endpoint_still_offers_offline_extraction_without_a_registry() -> None:
    application = create_app(InMemoryDocumentRepository())

    body = anyio.run(get_from, application, "/models").json()

    assert body["default"] == "extractive"
    assert [entry["id"] for entry in body["models"]] == ["extractive"]


def test_query_without_generator_id_uses_the_server_default() -> None:
    application = _graph_app()

    body = anyio.run(
        post, application, "/query", {"question": "百日咳用什么药", "max_hops": 2}
    ).json()

    assert body["metrics"]["generator"] == "extractive"
    # 不传 generator_id 时记录默认模型 ID，前端据此能显示「这次本来要用哪个」。
    assert body["metrics"]["requested_generator"] == "extractive"
    assert body["metrics"]["model"] == ""


def test_query_accepts_an_explicit_offline_generator() -> None:
    application = _graph_app()

    body = anyio.run(
        post,
        application,
        "/query",
        {"question": "百日咳用什么药", "max_hops": 2, "generator_id": "extractive"},
    ).json()

    assert body["metrics"]["requested_generator"] == "extractive"
    assert body["metrics"]["generator"] == "extractive"


def test_unknown_generator_id_returns_400() -> None:
    application = _graph_app()

    response = anyio.run(
        post,
        application,
        "/query",
        {"question": "百日咳用什么药", "generator_id": "no-such-model"},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_generator"


def test_unavailable_generator_returns_503_without_falling_back(tmp_path) -> None:
    # 密钥缺失：条目还在清单里，但请求它必须明确失败，绝不改用别的在线模型。
    registry = _model_registry(
        tmp_path, default="extractive", api_key_env="ABSENT_KEY"
    )
    documents, retriever, graph = _graph_stack()
    application = create_app(
        documents, retriever, graph_repository=graph, model_registry=registry
    )

    listing = anyio.run(get_from, application, "/models").json()
    response = anyio.run(
        post,
        application,
        "/query",
        {"question": "百日咳用什么药", "max_hops": 2, "generator_id": "main"},
    )

    assert listing["models"][1]["available"] is False
    assert "ABSENT_KEY" in listing["models"][1]["reason"]
    assert response.status_code == 503
    assert response.json()["error_code"] == "generator_unavailable"


def test_answer_text_never_repeats_the_derived_associations() -> None:
    application = _graph_app()

    body = anyio.run(
        post, application, "/query", {"question": "百日咳用什么药", "max_hops": 2}
    ).json()

    assert body["claims"]
    assert body["derived_associations"]
    for association in body["derived_associations"]:
        assert association["text"] not in body["text"]


def test_uploaded_document_is_ingested_and_searchable() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(
        upload, application, "科室须知.txt", "呼吸内科负责处理咳嗽与呼吸困难。".encode("utf-8")
    )
    body = response.json()
    found = anyio.run(
        post, application, "/retrieval/search", {"query": "呼吸内科", "max_hops": 1}
    ).json()

    assert response.status_code == 200
    assert body["job"]["status"] == "succeeded"
    assert body["document"]["source_name"] == "科室须知.txt"
    assert body["version"]["number"] == 1
    assert body["chunks"]
    # 普通上传只进文本知识库，检索得到但没有任何图路径。
    assert found["evidences"][0]["graph_path"] is None


def test_reuploading_identical_content_is_skipped() -> None:
    application = create_app(InMemoryDocumentRepository())

    anyio.run(upload, application, "科室须知.txt", "呼吸内科负责处理咳嗽。".encode("utf-8"))
    second = anyio.run(upload, application, "科室须知.txt", "呼吸内科负责处理咳嗽。".encode("utf-8"))

    assert second.json()["job"]["status"] == "skipped"
    assert second.json()["version"]["number"] == 1


@pytest.mark.parametrize(
    "filename, raw",
    [
        ("须知.txt", "呼吸内科负责咳嗽的诊治。".encode("utf-8")),
        ("须知.md", "# 呼吸内科\n\n负责咳嗽的诊治。".encode("utf-8")),
        ("须知.json", json.dumps({"科室": "呼吸内科"}, ensure_ascii=False).encode()),
        ("须知.jsonl", b'{"ke":"huxi"}\n{"ke":"kesou"}'),
        ("须知.csv", "科室,职责\n呼吸内科,咳嗽\n".encode("utf-8")),
    ],
)
def test_supported_text_formats_can_be_uploaded(filename: str, raw: bytes) -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(upload, application, filename, raw)

    assert response.status_code == 200, response.text
    assert response.json()["document"]["source_name"] == filename
    assert response.json()["chunks"]


def test_pdf_without_extractable_text_is_reported_clearly() -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    from io import BytesIO

    buffer = BytesIO()
    writer.write(buffer)
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(upload, application, "扫描件.pdf", buffer.getvalue())

    assert response.status_code == 400
    assert "OCR" in response.json()["detail"]


def test_upload_rejects_an_unsupported_extension() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(upload, application, "指南.docx", b"not a DOCX")

    assert response.status_code == 400
    assert response.json()["error_code"] == "unsupported_document"


def test_upload_filename_is_reduced_to_a_bare_name() -> None:
    # 文件名只作来源名称，任何目录成分都不该被保留。
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(upload, application, r"..\..\windows\指南.txt", b"hello")

    assert response.status_code == 200
    assert response.json()["document"]["source_name"] == "指南.txt"


def test_upload_over_the_limit_is_rejected_by_the_header_gate() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(
        upload, application, "big.txt", b"x" * (12 * 1024 * 1024)
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "file_too_large"


def test_upload_over_the_limit_is_rejected_even_when_the_header_passes(
    monkeypatch,
) -> None:
    # Content-Length 只是第一道闸门，精确判定必须按解码后的真实字节数复检。
    monkeypatch.setenv("TRACEGRAPH_MAX_UPLOAD_MB", "1")
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(upload, application, "big.txt", b"x" * 1_100_000)

    assert response.status_code == 413
    assert response.json()["error_code"] == "file_too_large"


def test_upload_within_the_limit_still_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("TRACEGRAPH_MAX_UPLOAD_MB", "1")
    application = create_app(InMemoryDocumentRepository())

    response = anyio.run(upload, application, "ok.txt", b"x" * 512_000)

    assert response.status_code == 200


@pytest.mark.parametrize("value", ["soon", "0", "-2"])
def test_invalid_upload_limit_is_rejected_at_startup(value: str, monkeypatch) -> None:
    monkeypatch.setenv("TRACEGRAPH_MAX_UPLOAD_MB", value)

    with pytest.raises(ValueError):
        load_max_upload_bytes()


def test_upload_limit_defaults_to_ten_mebibytes() -> None:
    assert load_max_upload_bytes({}) == 10 * 1024 * 1024
    assert load_max_upload_bytes({"TRACEGRAPH_MAX_UPLOAD_MB": "2"}) == 2 * 1024 * 1024


def test_missing_frontend_build_is_reported_clearly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRACEGRAPH_FRONTEND_DIST", str(tmp_path / "absent"))

    response = anyio.run(get_from, create_app(InMemoryDocumentRepository()), "/app")

    assert response.status_code == 503
    assert response.json()["error_code"] == "frontend_unavailable"
