"""内置领域适配器注册表与 Workspace 适配器校验的开发期检查。"""

import json

import anyio
import httpx2
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import DEFAULT_WORKSPACE_ADAPTER_ID, AnswerStatus
from tracegraph.domains.registry import (
    AdapterEntry,
    AdapterRegistry,
    DuplicateAdapterError,
    UnknownAdapterError,
    build_default_adapter_registry,
)
from tracegraph.generation.service import AnswerService
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.storage.memory import InMemoryDocumentRepository

_BUILTIN_IDS = ["medical", "general", "personal-notes"]
_DESCRIBED_KEYS = {
    "id",
    "label",
    "description",
    "version",
    "entity_types",
    "relation_types",
    "builtin",
}


async def _get(application, path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def _post(application, path: str, payload: dict) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=payload)


@pytest.fixture
def application():
    return create_app(InMemoryDocumentRepository())


def test_builtin_adapters_are_listed_in_stable_order() -> None:
    registry = build_default_adapter_registry()

    first = registry.describe()
    second = registry.describe()

    assert [entry["id"] for entry in first["adapters"]] == _BUILTIN_IDS
    # 顺序稳定：同一个注册表每次描述的字节完全一致。
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    assert [entry["builtin"] for entry in first["adapters"]] == [True, True, True]


def test_adapter_description_carries_no_prompts_paths_or_secrets() -> None:
    described = build_default_adapter_registry().describe()

    for entry in described["adapters"]:
        assert set(entry) == _DESCRIBED_KEYS
        assert entry["entity_types"] and entry["relation_types"]
        assert entry["label"] and entry["description"] and entry["version"]

    serialized = json.dumps(described, ensure_ascii=False)
    for forbidden in (
        "120",  # 医疗急症提示原文
        "急诊",
        "prompt",
        "Prompt",
        "api_key",
        "sk-",
        "base_url",
        "BASE_URL",
        "src/",
        "\\",
        "/",
        ".py",
    ):
        assert forbidden not in serialized


def test_duplicate_adapter_id_is_rejected() -> None:
    entry = AdapterEntry(
        adapter=build_default_adapter_registry().resolve("general"),
        label="通用",
        description="重复登记。",
    )

    with pytest.raises(DuplicateAdapterError, match="general"):
        AdapterRegistry((entry, entry))


def test_unknown_adapter_id_is_rejected() -> None:
    registry = build_default_adapter_registry()

    assert registry.resolve("general").name == "general"
    with pytest.raises(UnknownAdapterError, match="ws-1"):
        registry.resolve("ws-1")


def test_registry_exposes_no_way_to_register_at_runtime() -> None:
    registry = build_default_adapter_registry()

    # 构造之后没有任何写入入口，ID 清单因此不会被请求改掉。
    for method in ("register", "add", "update", "remove", "unregister", "clear"):
        assert not hasattr(registry, method)
    assert [entry["id"] for entry in registry.describe()["adapters"]] == _BUILTIN_IDS


def test_adapters_endpoint_returns_the_builtin_catalog(application) -> None:
    response = anyio.run(_get, application, "/adapters")

    assert response.status_code == 200
    adapters = response.json()["adapters"]
    assert [entry["id"] for entry in adapters] == _BUILTIN_IDS
    assert len({entry["id"] for entry in adapters}) == len(adapters)


def test_adapters_endpoint_is_read_only(application) -> None:
    # 没有 POST /adapters：FastAPI 对只注册了 GET 的路径回 405。
    response = anyio.run(_post, application, "/adapters", {"id": "evil"})

    assert response.status_code == 405


@pytest.mark.parametrize("adapter_id", _BUILTIN_IDS)
def test_workspace_can_be_created_with_each_builtin_adapter(
    application, adapter_id: str
) -> None:
    response = anyio.run(
        _post,
        application,
        "/workspaces",
        {"name": f"知识库 {adapter_id}", "adapter_id": adapter_id},
    )

    assert response.status_code == 200
    assert response.json()["adapter_id"] == adapter_id
    # 建出来的 Workspace 能被读回来，适配器没有被改写。
    stored = anyio.run(_get, application, f"/workspaces/{response.json()['id']}")
    assert stored.json()["adapter_id"] == adapter_id


def test_unknown_adapter_id_is_rejected(application) -> None:
    response = anyio.run(
        _post,
        application,
        "/workspaces",
        {"name": "知识库", "adapter_id": "medical-ish"},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_adapter"
    # 没有留下半成品 Workspace，只剩存储层保证存在的 default。
    listed = anyio.run(_get, application, "/workspaces").json()["workspaces"]
    assert [workspace["id"] for workspace in listed] == ["ws-default"]


@pytest.mark.parametrize("adapter_id", ["", "   "])
def test_blank_adapter_id_is_rejected(application, adapter_id: str) -> None:
    response = anyio.run(
        _post, application, "/workspaces", {"name": "知识库", "adapter_id": adapter_id}
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_adapter"


def test_missing_adapter_id_is_rejected_by_request_validation(application) -> None:
    response = anyio.run(_post, application, "/workspaces", {"name": "知识库"})

    assert response.status_code == 422


def test_default_workspace_still_uses_medical(application) -> None:
    response = anyio.run(_get, application, "/workspaces/ws-default")

    assert response.status_code == 200
    assert response.json()["adapter_id"] == DEFAULT_WORKSPACE_ADAPTER_ID == "medical"


@pytest.mark.parametrize("adapter_id", ["general", "personal-notes"])
def test_non_medical_adapters_do_not_apply_medical_rules(adapter_id: str) -> None:
    adapter = build_default_adapter_registry().resolve(adapter_id)

    # 医疗急症词与医疗范围外的词，在非医疗适配器里都不该被拦截。
    for question in ("胸痛得厉害还喘不上气", "今天股票怎么样", "帮我写代码"):
        assert adapter.preflight_status(adapter.normalize_question(question)) is None

    # 空白规范化仍然生效。
    assert adapter.normalize_question("  高血压   怎么办  ") == "高血压 怎么办"

    message = adapter.status_message(AnswerStatus.INSUFFICIENT_EVIDENCE, "任意问题")
    assert message
    for forbidden in ("急诊", "120", "就医", "医疗"):
        assert forbidden not in message


def test_non_medical_workspace_answers_without_medical_escalation() -> None:
    repository = InMemoryDocumentRepository()
    adapter = build_default_adapter_registry().resolve("general")
    service = AnswerService(KeywordRetriever(repository), adapter)

    answer = service.answer("胸痛得厉害还喘不上气")

    # 不再升级为急症，而是走正常检索：知识库为空，因此是证据不足。
    assert answer.status is AnswerStatus.INSUFFICIENT_EVIDENCE
    assert "120" not in answer.text
