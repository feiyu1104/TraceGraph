"""工作空间级自定义抽取类型的开发期检查。

覆盖四层：组合层（effective_adapter）、抽取侧的白名单、审核侧的校验，
以及 SQLite 的往返持久化。
"""

import json

from fastapi.testclient import TestClient
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import ExtractionVocabulary, Workspace
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.domains.scoped import effective_adapter
from tracegraph.extraction.service import ExtractionService
from tracegraph.generation.models import ModelEntry, ModelRegistry
from tracegraph.generation.providers import ExtractiveAnswerGenerator
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.review.service import CandidateContentConflictError, CandidateReviewService
from tracegraph.storage.candidates import InMemoryCandidateRepository
from tracegraph.storage.memory import InMemoryDocumentRepository
from tracegraph.storage.sqlite import SQLiteDocumentRepository

# 评审用的自定义领域：一个和医疗完全无关的类型空间。
PROJECT_ENTITY_TYPES = ("Person", "Bug", "Service")
PROJECT_RELATION_TYPES = ("ASSIGNED_TO", "BLOCKS")

PROJECT_VOCABULARY = ExtractionVocabulary(
    subject_type="Bug",
    sections={
        "简介": ("Bug", "BLOCKS"),
        "负责人": ("Person", "ASSIGNED_TO"),
    },
)

# 与 DUTMED_MARKDOWN 同形状，但章节名对应上面这份词汇表。
PROJECT_MARKDOWN = """# 登录超时

## 简介

会话过期后无法重新登录

## 负责人

张三
"""


def _workspace(
    workspace_id: str,
    adapter_id: str = "general",
    *,
    entity_types: tuple[str, ...] | None = None,
    relation_types: tuple[str, ...] | None = None,
    vocabulary: ExtractionVocabulary | None = None,
) -> Workspace:
    return Workspace(
        id=workspace_id,
        name=workspace_id,
        adapter_id=adapter_id,
        created_at="2026-01-01T00:00:00+00:00",
        custom_entity_types=entity_types,
        custom_relation_types=relation_types,
        custom_vocabulary=vocabulary,
    )


def _service(documents, candidates) -> ExtractionService:
    models = ModelRegistry(
        "extractive",
        (ModelEntry(id="extractive", label="extractive", kind="extractive", model=""),),
        {"extractive": ExtractiveAnswerGenerator()},
    )
    return ExtractionService(
        documents, build_default_adapter_registry(), candidates, models
    )


# --------------------------------------------------------------------------
# 组合层
# --------------------------------------------------------------------------


def test_workspace_without_overrides_gets_the_adapter_itself() -> None:
    registry = build_default_adapter_registry()
    base = registry.resolve("general")

    # 没有覆盖时必须原对象直传，不是一层等价包装：绝大多数工作空间走的都是
    # 与改动前完全一样的那条路径。
    assert effective_adapter(_workspace("ws-plain"), base) is base


def test_only_relation_types_overridden_keeps_builtin_entity_types() -> None:
    base = build_default_adapter_registry().resolve("general")

    scoped = effective_adapter(
        _workspace("ws-partial", relation_types=PROJECT_RELATION_TYPES), base
    )

    # 两个字段互相独立：只覆盖关系类型，实体类型仍然是适配器内置的那一份。
    assert scoped.relation_types() == PROJECT_RELATION_TYPES
    assert scoped.entity_types() == base.entity_types()
    assert scoped.extraction_vocabulary() is base.extraction_vocabulary()


def test_scoped_adapter_delegates_the_rest_to_the_base() -> None:
    base = build_default_adapter_registry().resolve("general")
    question = "  登录   超时 怎么办  "

    scoped = effective_adapter(
        _workspace("ws-delegate", entity_types=PROJECT_ENTITY_TYPES), base
    )

    # 自定义类型不该改变问答链路依赖的任何一项，包括 name 与 version 两个属性。
    assert scoped.name == base.name
    assert scoped.version == base.version
    assert scoped.normalize_question(question) == base.normalize_question(question)
    assert scoped.preflight_status(question) == base.preflight_status(question)


# --------------------------------------------------------------------------
# 抽取侧
# --------------------------------------------------------------------------


def test_custom_vocabulary_drives_the_extractive_extractor() -> None:
    documents = InMemoryDocumentRepository()
    documents.save_workspace(
        _workspace(
            "ws-project",
            entity_types=PROJECT_ENTITY_TYPES,
            relation_types=PROJECT_RELATION_TYPES,
            vocabulary=PROJECT_VOCABULARY,
        )
    )
    result = TextIngestionService(documents).ingest_text(
        "bugs.md", PROJECT_MARKDOWN, "ws-project"
    )
    candidates = InMemoryCandidateRepository()

    run = _service(documents, candidates).start(
        "ws-project", document_id=result.document.id
    )

    assert run.status.value == "succeeded"
    assert run.entity_count > 0
    types = {entity.type for entity in candidates.list_entities(run.id)}
    assert types == {"Bug", "Person"}
    # 医疗类型一个都不许漏进来。
    assert "Disease" not in types


def test_custom_types_without_vocabulary_produce_no_candidates() -> None:
    documents = InMemoryDocumentRepository()
    documents.save_workspace(
        _workspace("ws-novocab", entity_types=PROJECT_ENTITY_TYPES)
    )
    result = TextIngestionService(documents).ingest_text(
        "bugs.md", PROJECT_MARKDOWN, "ws-novocab"
    )
    candidates = InMemoryCandidateRepository()

    run = _service(documents, candidates).start(
        "ws-novocab", document_id=result.document.id
    )

    # 通用适配器本来就没有词汇表：给了自定义类型也不猜章节，如实产出 0 条。
    assert run.status.value == "succeeded"
    assert run.entity_count == 0


class _FakeCompleter:
    """假的对话补全器：回放固定内容，并记下收到的提示词，不碰网络。"""

    name = "fake"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.content


def _model_service(documents, candidates, completer) -> ExtractionService:
    models = ModelRegistry(
        "model-a",
        (
            ModelEntry(
                id="model-a", label="model-a", kind="openai-compatible", model="test-model"
            ),
        ),
        {"model-a": completer},
    )
    return ExtractionService(
        documents, build_default_adapter_registry(), candidates, models
    )


def test_model_prompt_lists_the_custom_types_instead_of_the_builtin_ones() -> None:
    documents = InMemoryDocumentRepository()
    documents.save_workspace(
        _workspace(
            "ws-prompt",
            entity_types=PROJECT_ENTITY_TYPES,
            relation_types=PROJECT_RELATION_TYPES,
        )
    )
    result = TextIngestionService(documents).ingest_text(
        "bugs.md", PROJECT_MARKDOWN, "ws-prompt"
    )
    completer = _FakeCompleter('{"entities": [], "relations": []}')

    run = _model_service(documents, InMemoryCandidateRepository(), completer).start(
        "ws-prompt", document_id=result.document.id
    )

    assert run.status.value == "succeeded"
    system_prompt, _ = completer.calls[0]
    # Bug / Service 只在覆盖清单里；Concept / OCCURRED_AT 只在 general 内置清单里。
    # 两边都断言，才分得清「提示词用的是覆盖值」和「提示词两个都写了」。
    assert "Bug" in system_prompt and "Service" in system_prompt
    assert "ASSIGNED_TO" in system_prompt and "BLOCKS" in system_prompt
    assert "Concept" not in system_prompt
    assert "OCCURRED_AT" not in system_prompt


def test_model_candidate_outside_the_custom_types_is_dropped() -> None:
    documents = InMemoryDocumentRepository()
    documents.save_workspace(
        _workspace(
            "ws-drop",
            entity_types=PROJECT_ENTITY_TYPES,
            relation_types=PROJECT_RELATION_TYPES,
        )
    )
    result = TextIngestionService(documents).ingest_text(
        "bugs.md", PROJECT_MARKDOWN, "ws-drop"
    )
    known = result.chunks[0].id
    candidates = InMemoryCandidateRepository()
    completer = _FakeCompleter(
        json.dumps(
            {
                "entities": [
                    {"name": "登录超时", "type": "Bug", "evidence_chunk_ids": [known]},
                    {"name": "张三", "type": "Person", "evidence_chunk_ids": [known]},
                    # general 的内置类型，但不在覆盖清单里：必须丢掉。
                    {"name": "概念", "type": "Concept", "evidence_chunk_ids": [known]},
                    # 医疗类型同样不许漏进来。
                    {"name": "百日咳", "type": "Disease", "evidence_chunk_ids": [known]},
                ],
                "relations": [
                    {
                        "source": "张三",
                        "target": "登录超时",
                        "type": "ASSIGNED_TO",
                        "evidence_chunk_ids": [known],
                    },
                    {
                        "source": "张三",
                        "target": "登录超时",
                        "type": "TREATS",
                        "evidence_chunk_ids": [known],
                    },
                ],
            }
        )
    )

    run = _model_service(documents, candidates, completer).start(
        "ws-drop", document_id=result.document.id
    )

    assert run.status.value == "succeeded"
    assert {(entity.name, entity.type) for entity in candidates.list_entities(run.id)} == {
        ("登录超时", "Bug"),
        ("张三", "Person"),
    }
    assert [relation.type for relation in candidates.list_relations(run.id)] == [
        "ASSIGNED_TO"
    ]


# --------------------------------------------------------------------------
# 审核侧
# --------------------------------------------------------------------------


def test_review_rejects_a_type_outside_the_workspace_override() -> None:
    documents = InMemoryDocumentRepository()
    documents.save_workspace(
        _workspace(
            "ws-review",
            entity_types=PROJECT_ENTITY_TYPES,
            relation_types=PROJECT_RELATION_TYPES,
            vocabulary=PROJECT_VOCABULARY,
        )
    )
    result = TextIngestionService(documents).ingest_text(
        "bugs.md", PROJECT_MARKDOWN, "ws-review"
    )
    candidates = InMemoryCandidateRepository()
    run = _service(documents, candidates).start(
        "ws-review", document_id=result.document.id
    )
    entity = candidates.list_entities(run.id)[0]
    review = CandidateReviewService(
        documents, candidates, build_default_adapter_registry()
    )

    # 这一对类型是刻意挑的，两边都能把「覆盖有没有生效」区分开：
    # Concept 在 general 内置清单里但不在覆盖清单里，Bug 反过来。
    base_types = build_default_adapter_registry().resolve("general").entity_types()
    assert "Concept" in base_types and "Concept" not in PROJECT_ENTITY_TYPES
    assert "Bug" not in base_types and "Bug" in PROJECT_ENTITY_TYPES

    # 审核入口的收口点是 _adapter()：它必须看到工作空间的覆盖，否则人工审核
    # 放行的类型会和抽取提示词、候选白名单对不上。
    with pytest.raises(CandidateContentConflictError):
        review.review_entity("ws-review", entity.id, entity_type="Concept")

    # 覆盖清单里的类型照常放行。
    updated = review.review_entity("ws-review", entity.id, entity_type="Bug")
    assert updated.type == "Bug"


# --------------------------------------------------------------------------
# API 往返与校验
# --------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(InMemoryDocumentRepository()))


def test_workspace_without_custom_types_reports_null(client: TestClient) -> None:
    created = client.post(
        "/workspaces", json={"name": "普通库", "adapter_id": "general"}
    ).json()

    assert created["custom_types"] is None
    # 建库响应与读回响应必须是同一份形状，否则前端要到刷新后才拿到值。
    assert client.get(f"/workspaces/{created['id']}").json()["custom_types"] is None


def test_custom_types_round_trip_through_the_api(client: TestClient) -> None:
    payload = {
        "name": "项目库",
        "adapter_id": "general",
        "custom_types": {
            "entity_types": list(PROJECT_ENTITY_TYPES),
            "relation_types": list(PROJECT_RELATION_TYPES),
            "vocabulary": {
                "subject_type": "Bug",
                "sections": {"简介": ["Bug", "BLOCKS"]},
                "separator": "、",
            },
        },
    }

    created = client.post("/workspaces", json=payload)
    assert created.status_code == 200
    stored = client.get(f"/workspaces/{created.json()['id']}").json()

    assert stored["custom_types"]["entity_types"] == list(PROJECT_ENTITY_TYPES)
    assert stored["custom_types"]["relation_types"] == list(PROJECT_RELATION_TYPES)
    assert stored["custom_types"]["vocabulary"]["sections"] == {"简介": ["Bug", "BLOCKS"]}


def test_partial_override_keeps_the_other_side_null(client: TestClient) -> None:
    created = client.post(
        "/workspaces",
        json={
            "name": "只改关系",
            "adapter_id": "general",
            "custom_types": {"relation_types": list(PROJECT_RELATION_TYPES)},
        },
    ).json()

    # null 表示「沿用内置」，前端据此回落到 /adapters 的清单。
    assert created["custom_types"]["entity_types"] is None
    assert created["custom_types"]["relation_types"] == list(PROJECT_RELATION_TYPES)


@pytest.mark.parametrize("field", ["entity_types", "relation_types"])
def test_empty_type_list_is_rejected(client: TestClient, field: str) -> None:
    response = client.post(
        "/workspaces",
        json={"name": "空清单", "adapter_id": "general", "custom_types": {field: []}},
    )

    # 空清单的含义是「一个类型都不允许」，让它落库这个库会安静地抽不出东西。
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"


def test_blank_type_names_are_dropped_and_deduplicated(client: TestClient) -> None:
    created = client.post(
        "/workspaces",
        json={
            "name": "去重",
            "adapter_id": "general",
            "custom_types": {"entity_types": [" Bug ", "Bug", "Person", "  "]},
        },
    ).json()

    assert created["custom_types"]["entity_types"] == ["Bug", "Person"]


def test_type_names_are_deduplicated_case_sensitively(client: TestClient) -> None:
    created = client.post(
        "/workspaces",
        json={
            "name": "大小写",
            "adapter_id": "general",
            "custom_types": {"entity_types": ["Bug", "bug"]},
        },
    ).json()

    # 类型名是提示词里的字面量，不做大小写折叠：两个都留着，交给用户自己收敛。
    assert created["custom_types"]["entity_types"] == ["Bug", "bug"]


@pytest.mark.parametrize(
    "vocabulary",
    [
        # subject_type 越界
        {"subject_type": "Disease", "sections": {}},
        # 章节的实体类型越界
        {"subject_type": "Bug", "sections": {"简介": ["Disease", "BLOCKS"]}},
        # 章节的关系类型越界
        {"subject_type": "Bug", "sections": {"简介": ["Bug", "TREATS"]}},
        # 越界的那一半是「没覆盖」的那一半：内置清单里也没有这些
        {"subject_type": "Bug", "sections": {"简介": ["Bug", "MENTIONS"]}},
    ],
)
def test_vocabulary_outside_the_effective_types_is_rejected(
    client: TestClient, vocabulary: dict
) -> None:
    response = client.post(
        "/workspaces",
        json={
            "name": "越界词汇表",
            "adapter_id": "general",
            "custom_types": {
                "entity_types": list(PROJECT_ENTITY_TYPES),
                "relation_types": list(PROJECT_RELATION_TYPES),
                "vocabulary": vocabulary,
            },
        },
    )

    # 这是唯一一次一致性校验：抽取侧对越界类型是静默丢弃，不拦的话用户只会
    # 看到「抽取成功但 0 条候选」。
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_request"


def test_vocabulary_can_use_the_builtin_types_when_nothing_is_overridden(
    client: TestClient,
) -> None:
    # 只给了词汇表、没给类型清单：校验必须对着适配器的内置清单做，而不是空集。
    response = client.post(
        "/workspaces",
        json={
            "name": "自带词汇表",
            "adapter_id": "general",
            "custom_types": {
                "vocabulary": {"subject_type": "Topic", "sections": {}}
            },
        },
    )

    assert response.status_code == 200


def test_workspace_creation_still_requires_an_adapter_id(client: TestClient) -> None:
    response = client.post("/workspaces", json={"name": "没有适配器"})

    assert response.status_code == 422


# --------------------------------------------------------------------------
# 持久化
# --------------------------------------------------------------------------


def test_sqlite_round_trips_custom_types(tmp_path) -> None:
    database = tmp_path / "graph.sqlite3"
    workspace = _workspace(
        "ws-persist",
        entity_types=PROJECT_ENTITY_TYPES,
        relation_types=PROJECT_RELATION_TYPES,
        vocabulary=PROJECT_VOCABULARY,
    )
    with SQLiteDocumentRepository(database) as repository:
        repository.save_workspace(workspace)

    with SQLiteDocumentRepository(database) as repository:
        stored = repository.get_workspace("ws-persist")

    assert stored is not None
    assert stored.custom_entity_types == PROJECT_ENTITY_TYPES
    assert stored.custom_relation_types == PROJECT_RELATION_TYPES
    vocabulary = stored.custom_vocabulary
    assert vocabulary is not None
    assert vocabulary.subject_type == "Bug"
    assert vocabulary.separator == "、"
    # JSON 里 section 的值是 list，读回来必须是 tuple：契约声明的是二元组。
    assert vocabulary.sections["简介"] == ("Bug", "BLOCKS")
    assert isinstance(vocabulary.sections["简介"], tuple)


def test_sqlite_keeps_a_workspace_without_overrides_empty(tmp_path) -> None:
    database = tmp_path / "graph.sqlite3"
    with SQLiteDocumentRepository(database) as repository:
        repository.save_workspace(_workspace("ws-plain"))
        stored = repository.get_workspace("ws-plain")

    assert stored is not None
    assert stored.custom_entity_types is None
    assert stored.custom_relation_types is None
    assert stored.custom_vocabulary is None


def test_malformed_json_in_a_custom_column_falls_back_to_no_override(tmp_path) -> None:
    database = tmp_path / "graph.sqlite3"
    with SQLiteDocumentRepository(database) as repository:
        repository.save_workspace(_workspace("ws-broken"))
        # 绕过写入端直接塞坏数据：一行读不懂的配置不该让这个知识库无法访问。
        repository._connection.execute(
            "UPDATE workspaces SET custom_entity_types = ?, custom_vocabulary = ?"
            " WHERE id = ?",
            ("{not json", json.dumps(["wrong", "shape"]), "ws-broken"),
        )
        stored = repository.get_workspace("ws-broken")

    assert stored is not None
    assert stored.custom_entity_types is None
    assert stored.custom_vocabulary is None


def test_existing_workspaces_survive_the_new_columns(tmp_path) -> None:
    """迁移只做加法：加列之前建的库读回来仍是「没有自定义」。"""
    database = tmp_path / "graph.sqlite3"
    with SQLiteDocumentRepository(database) as repository:
        columns = {
            row["name"]
            for row in repository._connection.execute("PRAGMA table_info(workspaces)")
        }
        assert {"custom_entity_types", "custom_relation_types", "custom_vocabulary"} <= columns
        # 存储层自己保证的默认库也带着新列。
        default = repository.get_workspace("ws-default")
        assert default is not None
        assert default.custom_entity_types is None
