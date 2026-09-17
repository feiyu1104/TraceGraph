"""Workspace 级关键词检索与适配器选择的开发期检查。"""

import asyncio

import anyio
import httpx2
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    Entity,
    Relation,
    Workspace,
)
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.retrieval.graph import GraphRetriever
from tracegraph.retrieval.hybrid import HybridRetriever
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository

_CREATED_AT = "2026-01-01T00:00:00+00:00"

# 同一个关键词在两个 Workspace 里各出现一次：只要链路没按 Workspace 隔离，
# 两个请求就会拿到同一批证据，测试立刻能看出来。
_KEYWORD = "血压监测"
_CONTENT_A = "工作笔记：本周要复查血压监测的记录方式。"
_CONTENT_B = "另一份资料也提到血压监测这件事，但内容完全不同。"


async def _post(application, path: str, payload: dict) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=payload)


def _seed(documents: InMemoryDocumentRepository) -> None:
    """两个非医疗 Workspace，各有一份含同一关键词的文档。"""
    for workspace_id, adapter_id, name in (
        ("ws-a", "general", "通用资料库"),
        ("ws-b", "personal-notes", "个人笔记库"),
    ):
        documents.save_workspace(
            Workspace(
                id=workspace_id,
                name=name,
                adapter_id=adapter_id,
                created_at=_CREATED_AT,
            )
        )
    ingestion = TextIngestionService(documents)
    ingestion.ingest_text("a.md", _CONTENT_A, "ws-a")
    ingestion.ingest_text("b.md", _CONTENT_B, "ws-b")


def _two_workspace_repository() -> InMemoryDocumentRepository:
    documents = InMemoryDocumentRepository()
    _seed(documents)
    return documents


def _two_workspace_app(**kwargs):
    documents = _two_workspace_repository()
    return (
        create_app(
            documents,
            adapter_registry=build_default_adapter_registry(),
            **kwargs,
        ),
        documents,
    )


def test_keyword_retrieval_scans_only_the_requested_workspace() -> None:
    documents = _two_workspace_repository()
    retriever = KeywordRetriever(documents)

    evidences = retriever.retrieve(_KEYWORD, workspace_id="ws-a")

    assert evidences
    for evidence in evidences:
        assert documents.get_document(evidence.document_id).workspace_id == "ws-a"


def test_keyword_retrieval_limits_the_scan_at_the_document_level() -> None:
    """扫描范围必须一开始就限定住，不能先全量取回再过滤。"""

    class _RecordingRepository(InMemoryDocumentRepository):
        def __init__(self) -> None:
            super().__init__()
            self.scanned: list[str | None] = []

        def list_documents(self, workspace_id=None):
            self.scanned.append(workspace_id)
            return super().list_documents(workspace_id)

    recording = _RecordingRepository()
    _seed(recording)

    KeywordRetriever(recording).retrieve(_KEYWORD, workspace_id="ws-b")

    # 一次 list_documents 调用，且带上了目标 Workspace —— 没有全量那一次。
    assert recording.scanned == ["ws-b"]


def test_retrieval_returns_only_the_requested_workspace_evidence() -> None:
    application, documents = _two_workspace_app()

    first = anyio.run(
        _post,
        application,
        "/retrieval/search",
        {"query": _KEYWORD, "workspace_id": "ws-a"},
    )
    second = anyio.run(
        _post,
        application,
        "/retrieval/search",
        {"query": _KEYWORD, "workspace_id": "ws-b"},
    )

    assert first.status_code == second.status_code == 200
    a_payload, b_payload = first.json(), second.json()
    a_documents = {item["document_id"] for item in a_payload["evidences"]}
    b_documents = {item["document_id"] for item in b_payload["evidences"]}
    assert a_documents and b_documents
    # 同一关键词，两个 Workspace 的证据完全不重叠。
    assert a_documents.isdisjoint(b_documents)

    assert a_payload["workspace_id"] == "ws-a"
    assert a_payload["adapter_id"] == "general"
    assert a_payload["retriever"] == "keyword"
    assert b_payload["workspace_id"] == "ws-b"
    assert b_payload["adapter_id"] == "personal-notes"

    for workspace_id, payload in (("ws-a", a_payload), ("ws-b", b_payload)):
        for evidence in payload["evidences"]:
            owner = documents.get_document(evidence["document_id"])
            assert owner.workspace_id == workspace_id


def test_query_cites_only_the_requested_workspace_chunks() -> None:
    application, documents = _two_workspace_app()

    response = anyio.run(
        _post,
        application,
        "/query",
        {"question": _KEYWORD, "workspace_id": "ws-a"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "answered"
    assert payload["evidences"]
    for evidence in payload["evidences"]:
        owner = documents.get_document(evidence["document_id"])
        assert owner.workspace_id == "ws-a"
    for claim in payload["claims"]:
        assert claim["evidence_ids"]
    assert payload["metrics"]["workspace_id"] == "ws-a"
    assert payload["metrics"]["adapter_id"] == "general"
    assert payload["metrics"]["retriever"] == "keyword"


def test_general_workspace_does_not_trigger_medical_escalation() -> None:
    application, _ = _two_workspace_app()

    response = anyio.run(
        _post,
        application,
        "/query",
        {"question": "胸痛得厉害还喘不上气", "workspace_id": "ws-a"},
    )

    assert response.status_code == 200
    payload = response.json()
    # 医疗适配器会把这个症状升级为急症；通用 Workspace 只是照常检索。
    assert payload["status"] == "insufficient_evidence"
    assert "120" not in (payload["text"] or "")
    assert "急诊" not in (payload["text"] or "")
    assert payload["metrics"]["adapter_id"] == "general"


def test_medical_workspace_keeps_the_emergency_rule() -> None:
    application = create_app()

    response = anyio.run(
        _post,
        application,
        "/query",
        {"question": "胸痛得厉害还喘不上气", "workspace_id": DEFAULT_WORKSPACE_ID},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "emergency_escalation"
    assert "120" in payload["text"]
    # 急症拒答没有检索结果，指标仍要带出本次用的是哪个 Workspace 与适配器。
    assert payload["metrics"]["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert payload["metrics"]["adapter_id"] == "medical"


def test_personal_notes_workspace_uses_its_own_insufficient_message() -> None:
    application, _ = _two_workspace_app()

    response = anyio.run(
        _post,
        application,
        "/query",
        {"question": "下周的会议室预订改到几点", "workspace_id": "ws-b"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "insufficient_evidence"
    assert payload["text"] == "当前笔记中没有找到足够证据支持回答。"
    assert payload["metrics"]["adapter_id"] == "personal-notes"


def test_non_medical_workspace_still_uses_the_graph_with_its_own_id() -> None:
    """图检索不再挑 Workspace 或适配器；隔离靠每次查询都带上本次的 workspace_id。"""

    class _RecordingGraph(InMemoryGraphRepository):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[tuple[str, str]] = []

        def search_entities(self, query: str, workspace_id: str, limit: int = 5):
            self.seen.append(("search_entities", workspace_id))
            return super().search_entities(query, workspace_id, limit=limit)

        def expand_frontier(self, node_ids, **kwargs):
            self.seen.append(("expand_frontier", kwargs["workspace_id"]))
            return super().expand_frontier(node_ids, **kwargs)

    graph = _RecordingGraph()
    # 默认 Workspace 里放一批同名实体：非医疗 Workspace 的查询不能看见它们。
    graph.upsert_entity(Entity("d1", "血压监测", "Disease"))
    documents = _two_workspace_repository()
    application = create_app(
        documents,
        retriever=HybridRetriever(
            (KeywordRetriever(documents), GraphRetriever(documents, graph))
        ),
        graph_repository=graph,
        adapter_registry=build_default_adapter_registry(),
    )

    response = anyio.run(
        _post,
        application,
        "/query",
        {"question": _KEYWORD, "workspace_id": "ws-a"},
    )

    assert response.status_code == 200
    assert response.json()["metrics"]["retriever"] == "hybrid"
    assert graph.seen
    # 每一次图查询带的都是本次请求的 Workspace，一个 ws-default 都没有。
    assert {workspace_id for _, workspace_id in graph.seen} == {"ws-a"}
    # ws-a 里没有图实体，证据只可能来自它自己那份文档；默认 Workspace 的
    # 同名实体既没被当成起点，也没把它的文档带进结果。
    own_document = documents.list_documents("ws-a")[0]
    assert {evidence["document_id"] for evidence in response.json()["evidences"]} == {
        own_document.id
    }


def test_default_workspace_keeps_hybrid_retrieval_and_multi_hop() -> None:
    documents = InMemoryDocumentRepository()
    chunks = {
        key: TextIngestionService(documents)
        .ingest_text(name, content)
        .chunks[0]
        .id
        for key, (name, content) in {
            "r1": ("dutmed-百日咳-推荐药物.md", "百日咳的推荐药物包括琥乙红霉素片。"),
            "r2": (
                "dutmed-小儿支原体肺炎-推荐药物.md",
                "小儿支原体肺炎的推荐药物包括琥乙红霉素片。",
            ),
        }.items()
    }
    graph = InMemoryGraphRepository()
    graph.upsert_entity(Entity("d1", "百日咳", "Disease"))
    graph.upsert_entity(Entity("d2", "小儿支原体肺炎", "Disease"))
    graph.upsert_entity(Entity("m1", "琥乙红霉素片", "Drug"))
    graph.upsert_relation(Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", (chunks["r1"],)))
    graph.upsert_relation(Relation("r2", "d2", "m1", "RECOMMENDS_DRUG", (chunks["r2"],)))
    application = create_app(
        documents,
        retriever=HybridRetriever(
            (KeywordRetriever(documents), GraphRetriever(documents, graph))
        ),
        graph_repository=graph,
    )

    response = anyio.run(
        _post,
        application,
        "/retrieval/search",
        {"query": "百日咳的推荐药物", "max_hops": 2, "workspace_id": DEFAULT_WORKSPACE_ID},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["retriever"] == "hybrid"
    assert payload["adapter_id"] == "medical"
    assert any(
        evidence["graph_path"] is not None
        and len(evidence["graph_path"]["steps"]) == 2
        for evidence in payload["evidences"]
    )


@pytest.mark.parametrize("path", ["/query", "/retrieval/search"])
def test_unknown_workspace_is_reported(path: str) -> None:
    application, _ = _two_workspace_app()
    payload = (
        {"question": _KEYWORD, "workspace_id": "ws-nowhere"}
        if path == "/query"
        else {"query": _KEYWORD, "workspace_id": "ws-nowhere"}
    )

    response = anyio.run(_post, application, path, payload)

    assert response.status_code == 404
    assert response.json()["error_code"] == "workspace_not_found"


@pytest.mark.parametrize("path", ["/query", "/retrieval/search"])
def test_workspace_with_unknown_adapter_is_reported(path: str) -> None:
    application, documents = _two_workspace_app()
    # 历史数据：注册表里已经没有这个适配器 ID 了。
    documents.save_workspace(
        Workspace(
            id="ws-legacy",
            name="迁移过来的旧库",
            adapter_id="dutmed-legacy",
            created_at=_CREATED_AT,
        )
    )
    payload = (
        {"question": _KEYWORD, "workspace_id": "ws-legacy"}
        if path == "/query"
        else {"query": _KEYWORD, "workspace_id": "ws-legacy"}
    )

    response = anyio.run(_post, application, path, payload)

    assert response.status_code == 409
    assert response.json()["error_code"] == "workspace_adapter_unavailable"


def test_client_cannot_pick_the_adapter_directly() -> None:
    application, _ = _two_workspace_app()

    # 只有 Workspace 记录的 adapter_id 说了算，请求里带的这个字段不起作用。
    response = anyio.run(
        _post,
        application,
        "/query",
        {
            "question": _KEYWORD,
            "workspace_id": "ws-a",
            "adapter_id": "medical",
        },
    )

    assert response.status_code == 200
    assert response.json()["metrics"]["adapter_id"] == "general"


def test_concurrent_queries_do_not_swap_workspaces_or_adapters() -> None:
    application, _ = _two_workspace_app()
    questions = [
        {"question": _KEYWORD, "workspace_id": "ws-a"},
        {"question": _KEYWORD, "workspace_id": "ws-b"},
    ] * 8

    async def _ask():
        transport = httpx2.ASGITransport(app=application)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await asyncio.gather(
                *(
                    client.post("/query", json=payload)
                    for payload in questions
                )
            )

    responses = anyio.run(_ask)

    assert len(responses) == len(questions)
    for payload, response in zip(questions, responses):
        assert response.status_code == 200
        metrics = response.json()["metrics"]
        assert metrics["workspace_id"] == payload["workspace_id"]
        assert metrics["adapter_id"] == (
            "general" if payload["workspace_id"] == "ws-a" else "personal-notes"
        )
        for evidence in response.json()["evidences"]:
            assert evidence["source_name"].startswith(
                "a.md" if payload["workspace_id"] == "ws-a" else "b.md"
            )


def test_requests_without_workspace_id_still_reach_the_default() -> None:
    application = create_app()

    answer = anyio.run(_post, application, "/query", {"question": "高血压"})
    retrieval = anyio.run(
        _post, application, "/retrieval/search", {"query": "高血压"}
    )

    assert answer.status_code == retrieval.status_code == 200
    assert answer.json()["metrics"]["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert answer.json()["metrics"]["adapter_id"] == "medical"
    assert retrieval.json()["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert retrieval.json()["adapter_id"] == "medical"
