import json

from fastapi.testclient import TestClient
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import DEFAULT_WORKSPACE_ID, Chunk, Workspace
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.extraction.providers import (
    ExtractionError,
    ExtractionResponseError,
    ExtractiveCandidateExtractor,
    ModelCandidateExtractor,
    decode_extraction,
    normalize_name,
)
from tracegraph.extraction.service import (
    ExtractionFailedError,
    ExtractionService,
    UnknownExtractionTargetError,
    UnsupportedExtractionModelError,
)
from tracegraph.generation.models import (
    ModelEntry,
    ModelRegistry,
    UnknownGeneratorError,
)
from tracegraph.generation.providers import (
    ExtractiveAnswerGenerator,
    GenerationNetworkError,
    GenerationResponseError,
)
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.storage.candidates import InMemoryCandidateRepository
from tracegraph.storage.memory import InMemoryDocumentRepository

# DUTMed 记录转成 Markdown 后的真实形状：一级标题是疾病名，二级标题是章节。
DUTMED_MARKDOWN = """# 百日咳

## 简介

百日咳是由百日咳鲍特菌引起的急性呼吸道传染病。

## 症状

咳嗽、低热

## 推荐药物

琥乙红霉素片
"""


class _FakeCompleter:
    """一个假的对话补全器：只回放预先准备好的内容，不碰网络。"""

    name = "fake"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if isinstance(self.content, Exception):
            raise self.content
        return self.content


class _RecordingGraph:
    """记录有没有人写过图；抽取全过程它必须一次都没被碰到。"""

    name = "recording"

    def __init__(self) -> None:
        self.writes: list[str] = []


def _workspace(workspace_id: str, adapter_id: str) -> Workspace:
    return Workspace(
        id=workspace_id,
        name=workspace_id,
        adapter_id=adapter_id,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _ingest(
    repository,
    source_name="dutmed-百日咳.md",
    content=DUTMED_MARKDOWN,
    workspace_id=DEFAULT_WORKSPACE_ID,
):
    return TextIngestionService(repository).ingest_text(
        source_name, content, workspace_id
    )


def _registry(*, default_id: str = "extractive", generator=None, kind: str = "extractive"):
    if generator is None and kind == "extractive":
        generator = ExtractiveAnswerGenerator()
    entries = [ModelEntry(id=default_id, label=default_id, kind=kind, model="test-model")]
    generators = {default_id: generator} if generator is not None else {}
    return ModelRegistry(default_id, tuple(entries), generators)


def _service(repository, candidates, *, models=None, registry=None):
    return ExtractionService(
        repository,
        registry or build_default_adapter_registry(),
        candidates,
        models or _registry(),
    )


def _start(service, result, *, workspace_id=DEFAULT_WORKSPACE_ID, **overrides):
    """按文档启动一次抽取；绝大多数用例关心的都是「这个文档」。"""
    return service.start(
        workspace_id, document_id=result.document.id, **overrides
    )


# --------------------------------------------------------------------------
# 确定性摘录：完全由适配器词汇表驱动
# --------------------------------------------------------------------------


def test_extractive_run_produces_candidates_from_real_chunks() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    candidates = InMemoryCandidateRepository()

    run = _start(_service(documents, candidates), result)

    assert run.status.value == "succeeded"
    assert run.document_id == result.document.id
    assert run.document_version_id == result.version.id
    assert run.adapter_id == "medical"
    assert run.entity_count > 0 and run.relation_count > 0

    entities = candidates.list_entities(run.id)
    names = {entity.name for entity in entities}
    assert {"百日咳", "咳嗽", "低热", "琥乙红霉素片"} <= names
    assert candidates.list_workspace_relations(DEFAULT_WORKSPACE_ID)

    # 每条候选的证据都必须是这次真的读到的 Chunk。
    known = {chunk.id for chunk in result.chunks}
    for entity in entities:
        assert entity.evidence_chunk_ids
        assert set(entity.evidence_chunk_ids) <= known
    for relation in candidates.list_relations(run.id):
        assert relation.evidence_chunk_ids
        assert set(relation.evidence_chunk_ids) <= known


def test_extractive_run_reaches_every_status_in_order() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)

    seen: list[str] = []

    class _WatchingCandidates(InMemoryCandidateRepository):
        def save_run(self, run):
            seen.append(run.status.value)
            super().save_run(run)

    run = _start(_service(documents, _WatchingCandidates()), result)

    # pending → running 各写一次，终态由 save_extraction 一次写入。
    assert seen[:2] == ["pending", "running"]
    assert run.status.value == "succeeded"


def test_adapter_without_vocabulary_produces_no_candidates() -> None:
    documents = InMemoryDocumentRepository()
    documents.save_workspace(_workspace("ws-notes", "personal-notes"))
    result = _ingest(
        documents, "笔记.md", "# 周会\n\n## 结论\n\n下周继续", workspace_id="ws-notes"
    )
    candidates = InMemoryCandidateRepository()

    run = _start(_service(documents, candidates), result, workspace_id="ws-notes")

    # 个人笔记没有固定的章节约定：如实产出 0 条，而不是猜一个类型。
    assert run.status.value == "succeeded"
    assert run.entity_count == 0
    assert candidates.list_entities(run.id) == ()


def test_bad_model_candidates_are_dropped_not_saved() -> None:
    """未知类型、编造的 Chunk、自环与悬空端点：一条都不许进库。"""
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    known = result.chunks[0].id
    candidates = InMemoryCandidateRepository()
    completer = _FakeCompleter(
        json.dumps(
            {
                "entities": [
                    {"name": "百日咳", "type": "Disease", "evidence_chunk_ids": [known]},
                    {"name": "胡说", "type": "MadeUpType", "evidence_chunk_ids": [known]},
                ],
                "relations": [
                    # 端点不在本批实体里
                    {"source": "百日咳", "target": "没提过", "type": "HAS_SYMPTOM",
                     "evidence_chunk_ids": [known]},
                    # 自环
                    {"source": "百日咳", "target": "百日咳", "type": "HAS_SYMPTOM",
                     "evidence_chunk_ids": [known]},
                    # 未知关系类型
                    {"source": "百日咳", "target": "胡说", "type": "MADE_UP",
                     "evidence_chunk_ids": [known]},
                    # 端点实体本身已被丢弃
                    {"source": "百日咳", "target": "胡说", "type": "HAS_SYMPTOM",
                     "evidence_chunk_ids": [known]},
                ],
            },
            ensure_ascii=False,
        )
    )

    run = _start(_model_service(documents, candidates, completer), result)

    assert [
        (entity.name, entity.type) for entity in candidates.list_entities(run.id)
    ] == [("百日咳", "Disease")]
    assert candidates.list_relations(run.id) == ()


def test_evidence_outside_this_run_is_dropped() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    known = result.chunks[0].id
    candidates = InMemoryCandidateRepository()
    completer = _FakeCompleter(
        json.dumps(
            {
                "entities": [
                    {"name": "真的", "type": "Disease", "evidence_chunk_ids": [known]},
                    {"name": "编的", "type": "Disease", "evidence_chunk_ids": ["chk-made-up"]},
                ],
                "relations": [],
            },
            ensure_ascii=False,
        )
    )

    run = _start(_model_service(documents, candidates, completer), result)

    assert [
        entity.name for entity in candidates.list_entities(run.id)
    ] == ["真的"]


def test_extractive_merges_evidence_across_chunks_for_the_same_entity() -> None:
    extractor = ExtractiveCandidateExtractor()
    adapter = build_default_adapter_registry().resolve("medical")

    chunks = (
        Chunk(
            id="chk-1",
            document_id="doc-1",
            document_version_id="ver-1",
            index=0,
            content="咳嗽、低热",
            locator="百日咳 > 症状",
        ),
        Chunk(
            id="chk-2",
            document_id="doc-1",
            document_version_id="ver-1",
            index=1,
            content="咳嗽",
            locator="百日咳 > 并发症",
        ),
    )
    draft = extractor.extract(chunks, adapter)

    subject = next(item for item in draft.entities if item.name == "百日咳")
    assert subject.evidence_chunk_ids == ("chk-1", "chk-2")
    # 同名但不同类型的条目是两条不同的候选。
    assert {item.type for item in draft.entities if item.name == "咳嗽"} == {
        "Symptom",
        "Disease",
    }


def test_locator_without_a_known_section_is_ignored() -> None:
    extractor = ExtractiveCandidateExtractor()
    adapter = build_default_adapter_registry().resolve("medical")

    def chunk(chunk_id: str, locator: str, content: str) -> Chunk:
        return Chunk(
            id=chunk_id,
            document_id="doc-1",
            document_version_id="ver-1",
            index=0,
            content=content,
            locator=locator,
        )

    # 简介是自由文本，没有对应的实体类型；三层定位符归属不明；无定位符的全文
    # 片段同样不猜。
    draft = extractor.extract(
        (
            chunk("chk-1", "百日咳 > 简介", "百日咳是传染病。"),
            chunk("chk-2", "百日咳 > 症状 > 说明", "咳嗽"),
            chunk("chk-3", "全文", "咳嗽"),
        ),
        adapter,
    )

    assert draft.entities == ()
    assert draft.relations == ()


def test_normalize_name_folds_whitespace_and_edge_punctuation() -> None:
    assert normalize_name("  百日咳 ") == normalize_name("百日咳")
    assert normalize_name("(百日咳)") == normalize_name("百日咳")
    assert normalize_name("Aspirin") == normalize_name("aspirin")


# --------------------------------------------------------------------------
# 模型抽取：结构校验与失败路径
# --------------------------------------------------------------------------


def _model_service(documents, candidates, completer, *, kind="openai-compatible"):
    return ExtractionService(
        documents,
        build_default_adapter_registry(),
        candidates,
        _registry(
            default_id="model-a",
            generator=completer,
            kind=kind,
        ),
    )


def test_model_extraction_accepts_only_known_types_and_evidence() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    candidates = InMemoryCandidateRepository()
    chunks = result.chunks
    known = chunks[0].id

    completer = _FakeCompleter(
        '{"entities": ['
        f'{{"name": "百日咳", "type": "Disease", "evidence_chunk_ids": ["{known}"]}},'
        f'{{"name": "症状", "type": "MadeUp", "evidence_chunk_ids": ["{known}"]}},'
        '{"name": "编的", "type": "Disease", "evidence_chunk_ids": ["chk-made-up"]}'
        '], "relations": ['
        f'{{"source": "百日咳", "target": "百日咳", "type": "HAS_SYMPTOM",'
        f' "evidence_chunk_ids": ["{known}"]}}'
        "]}"
    )

    run = _start(_model_service(documents, candidates, completer), result)

    # 未知类型、编造的 Chunk ID、自环关系全部被挡掉，只剩一条干净的实体。
    entities = candidates.list_entities(run.id)
    assert [(entity.name, entity.type) for entity in entities] == [("百日咳", "Disease")]
    assert candidates.list_relations(run.id) == ()


def test_model_prompt_lists_the_current_adapter_types_only() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    completer = _FakeCompleter('{"entities": [], "relations": []}')

    run = _start(
        _model_service(documents, InMemoryCandidateRepository(), completer), result
    )

    assert run.status.value == "succeeded"
    system_prompt, user_prompt = completer.calls[0]
    for entity_type in ("Disease", "Symptom", "Drug", "Recipe"):
        assert entity_type in system_prompt
    for relation_type in ("HAS_SYMPTOM", "RECOMMENDS_DRUG"):
        assert relation_type in system_prompt
    # 别的适配器的类型不在提示词里；片段 ID 只出现在用户消息里。
    assert "OCCURRED_AT" not in system_prompt
    assert result.chunks[0].id in user_prompt
    assert result.chunks[0].id not in system_prompt


def test_model_answer_that_is_not_json_fails_the_run() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    completer = _FakeCompleter("这不是 JSON")
    candidates = InMemoryCandidateRepository()

    with pytest.raises(ExtractionFailedError) as error:
        _start(_model_service(documents, candidates, completer), result)

    failed = candidates.get_run(error.value.run.id)
    assert failed.status.value == "failed"
    assert failed.error
    assert candidates.list_entities(failed.id) == ()


def test_model_network_failure_fails_the_run() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    completer = _FakeCompleter(GenerationNetworkError("无法连接模型服务。"))
    candidates = InMemoryCandidateRepository()

    with pytest.raises(ExtractionFailedError) as error:
        _start(_model_service(documents, candidates, completer), result)

    failed = candidates.get_run(error.value.run.id)
    assert failed.status.value == "failed"
    assert "无法连接模型服务" in failed.error


def test_structured_output_must_keep_the_agreed_shape() -> None:
    with pytest.raises(ExtractionResponseError):
        decode_extraction({"entities": []})
    with pytest.raises(ExtractionResponseError):
        decode_extraction({"entities": [{"name": "x", "type": "Disease"}], "relations": []})
    with pytest.raises(ExtractionResponseError):
        decode_extraction({"entities": [], "relations": [{"source": "a", "target": "b"}]})


def test_model_completer_wraps_generation_errors() -> None:
    adapter = build_default_adapter_registry().resolve("general")
    with pytest.raises(ExtractionError):
        ModelCandidateExtractor(_FakeCompleter(GenerationResponseError("x"))).extract(
            (), adapter
        )


def test_unknown_model_and_unsupported_kind_are_reported() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)

    with pytest.raises(UnknownGeneratorError):
        _start(_service(documents, InMemoryCandidateRepository()), result, model_id="nope")

    service = _service(
        documents,
        InMemoryCandidateRepository(),
        models=_registry(
            default_id="weird", generator=_FakeCompleter("{}"), kind="something-else"
        ),
    )
    with pytest.raises(UnsupportedExtractionModelError):
        _start(service, result)


# --------------------------------------------------------------------------
# 归属与隔离
# --------------------------------------------------------------------------


def test_document_of_another_workspace_is_not_found() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    documents.save_workspace(_workspace("ws-other", "general"))

    with pytest.raises(UnknownExtractionTargetError):
        _service(documents, InMemoryCandidateRepository()).start(
            "ws-other", document_id=result.document.id
        )


def test_unknown_document_and_version_are_not_found() -> None:
    documents = InMemoryDocumentRepository()
    _ingest(documents)
    service = _service(documents, InMemoryCandidateRepository())

    with pytest.raises(UnknownExtractionTargetError):
        service.start(DEFAULT_WORKSPACE_ID, document_id="doc-missing")
    with pytest.raises(UnknownExtractionTargetError):
        service.start(DEFAULT_WORKSPACE_ID, document_version_id="ver-missing")
    with pytest.raises(ExtractionError):
        service.start(DEFAULT_WORKSPACE_ID)


def test_other_workspace_cannot_read_candidates() -> None:
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    documents.save_workspace(_workspace("ws-other", "general"))
    candidates = InMemoryCandidateRepository()
    run = _start(_service(documents, candidates), result)

    assert candidates.list_workspace_entities("ws-other") == ()
    assert candidates.list_workspace_relations("ws-other") == ()
    assert candidates.list_entities(run.id)


def test_extraction_never_touches_the_graph() -> None:
    """抽取服务拿不到图仓储：候选没有任何写进正式图谱的路径。"""
    documents = InMemoryDocumentRepository()
    result = _ingest(documents)
    graph = _RecordingGraph()
    service = ExtractionService(
        documents,
        build_default_adapter_registry(),
        InMemoryCandidateRepository(),
        _registry(),
    )

    run = _start(service, result)

    assert run.status.value == "succeeded"
    assert graph.writes == []
    assert not any("graph" in name for name in vars(service))


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    documents = InMemoryDocumentRepository()
    candidates = InMemoryCandidateRepository()
    application = create_app(
        repository=documents,
        candidate_repository=candidates,
        adapter_registry=build_default_adapter_registry(),
        model_registry=_registry(),
    )
    with TestClient(application) as test_client:
        result = _ingest(documents)
        test_client.document_id = result.document.id
        test_client.version_id = result.version.id
        yield test_client


def test_extraction_endpoints_expose_runs_and_candidates(client) -> None:
    started = client.post(
        "/extractions",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "document_id": client.document_id},
    )
    assert started.status_code == 200
    run = started.json()
    assert run["status"] == "succeeded"
    assert run["adapter_id"] == "medical"
    assert run["model_id"] == "extractive"
    assert run["entity_count"] > 0

    status = client.get(f"/extractions/{run['id']}")
    assert status.status_code == 200
    assert status.json() == run

    payload = client.get(f"/extractions/{run['id']}/candidates").json()
    assert payload["run"]["id"] == run["id"]
    names = {entity["name"] for entity in payload["entities"]}
    assert {"百日咳", "咳嗽"} <= names
    entity = next(item for item in payload["entities"] if item["name"] == "咳嗽")
    assert entity["status"] == "pending"
    # 候选带着可阅读的原文片段，而不是只有一个 Chunk ID。
    assert entity["evidence"][0]["content"]
    assert entity["evidence"][0]["locator"] == "百日咳 > 症状"

    workspace = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/candidates").json()
    assert workspace["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert len(workspace["entities"]) == len(payload["entities"])
    assert len(workspace["relations"]) == len(payload["relations"])


def test_workspace_candidate_filters(client) -> None:
    client.post(
        "/extractions",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "document_id": client.document_id},
    )
    base = f"/workspaces/{DEFAULT_WORKSPACE_ID}/candidates"

    documented = client.get(base, params={"document_id": client.document_id}).json()
    assert documented["entities"]
    assert client.get(base, params={"document_id": "doc-missing"}).json()["entities"] == []
    assert client.get(base, params={"status": "approved"}).json()["entities"] == []
    assert client.get(base, params={"entity_type": "Drug"}).json()["entities"]
    assert client.get(base, params={"entity_type": "Recipe"}).json()["entities"] == []
    assert client.get(base, params={"relation_type": "HAS_SYMPTOM"}).json()["relations"]


def test_extraction_endpoints_report_errors(client) -> None:
    assert client.post("/extractions", json={}).status_code == 400
    assert client.get("/extractions/run-missing").status_code == 404
    assert client.get("/extractions/run-missing/candidates").status_code == 404

    unknown_workspace = client.post(
        "/extractions", json={"workspace_id": "ws-ghost", "document_id": client.document_id}
    )
    assert unknown_workspace.status_code == 404
    assert unknown_workspace.json()["error_code"] == "workspace_not_found"

    other_document = client.post(
        "/extractions",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "document_id": "doc-missing"},
    )
    assert other_document.status_code == 404
    assert other_document.json()["error_code"] == "not_found"

    unknown_model = client.post(
        "/extractions",
        json={
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "document_id": client.document_id,
            "model_id": "nope",
        },
    )
    assert unknown_model.status_code == 400
    assert unknown_model.json()["error_code"] == "invalid_generator"


def test_candidate_write_paths_are_only_review_and_publication(client) -> None:
    """候选的写入口只有审核与发布这几条，没有动作式路由，也没有别的发布口。"""
    paths = client.app.openapi()["paths"]
    assert {path for path in paths if "candidate" in path} == {
        "/extractions/{run_id}/candidates",
        "/workspaces/{workspace_id}/candidates",
        "/candidate-entities/{candidate_id}",
        "/candidate-relations/{candidate_id}",
        "/candidates/batch-review",
    }
    for path, item in paths.items():
        # 审核是改状态而不是调用动作：没有 approve/reject 之类的独立路由，
        # 因此不可能出现「绕过状态机直接置位」的入口。
        assert not path.endswith("/approve") and not path.endswith("/reject")
        if "publish" in path:
            # 唯一能写图后端的路由，且它按 Workspace 收窄。
            assert path == "/workspaces/{workspace_id}/graph-publications"
            assert set(item) == {"post"}


def test_deleting_a_document_removes_its_candidates(client) -> None:
    client.post(
        "/extractions",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "document_id": client.document_id},
    )
    before = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/candidates").json()
    assert before["entities"]

    assert client.delete(f"/documents/{client.document_id}").status_code == 200

    after = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/candidates").json()
    assert after["entities"] == []
    assert after["relations"] == []
