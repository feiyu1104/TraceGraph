"""服务端模型连接管理的回归检查（第十批 A）。

四件事各有一组用例：密钥只留在服务端，任何响应、错误与日志里都没有它；发现
请求的地址、超时、上游响应体与响应结构都被校验；连接可以新增、更新、删除并
切换默认模型；注册表被整体替换之后，`/models`、`/system`、问答与抽取立刻看到
新表，而且在并发读取时不会出现「默认 ID 与生成器错配」。

所有出网路径都用假的 opener 或注入的发现器替代：这里不访问真实网络，也不
使用任何真实 API Key。
"""

from io import BytesIO
import json
import os
from pathlib import Path
import stat
import threading
import traceback
from urllib.error import HTTPError

import anyio
import httpx2
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import Claim, Entity, Relation
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.generation.config import DEFAULT_TIMEOUT, GENERATOR_EXTRACTIVE
from tracegraph.generation.connections import (
    CONNECTIONS_PATH_ENV,
    ConnectionFile,
    ConnectionFileError,
    ConnectionNotFoundError,
    ConnectionStore,
    ModelConnectionService,
    load_connections_path,
)
from tracegraph.generation.discovery import (
    MAX_RESPONSE_BYTES,
    DiscoveryInvalidResponseError,
    DiscoveryUnauthorizedError,
    DiscoveryUnreachableError,
    DiscoveryUpstreamError,
    discover_models,
)
from tracegraph.generation.models import (
    EXTRACTIVE_LABEL,
    KIND_EXTRACTIVE,
    KIND_OPENAI,
    ModelEntry,
    ModelRegistry,
    RefreshingModelRegistry,
    load_model_registry,
)
from tracegraph.generation.providers import (
    ExtractiveAnswerGenerator,
    GeneratedAnswer,
)
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.retrieval.graph import GraphRetriever
from tracegraph.storage.candidates import InMemoryCandidateRepository
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository


# 测试专用密钥：它绝不能出现在任何响应、日志或异常里，下面多处按这条断言。
TEST_KEY = "sk-test-secret"
ROTATED_KEY = "sk-rotated-secret"
BASE_URL = "https://gateway.example.com/v1"

LOOPBACK = ("127.0.0.1", 51234)
REMOTE = ("10.0.0.5", 51234)


async def _send(application, method, path, payload, client, raise_app_exceptions):
    transport = httpx2.ASGITransport(
        app=application, client=client, raise_app_exceptions=raise_app_exceptions
    )
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.request(method, path, json=payload)


def call(
    application,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    client: tuple[str, int] = LOOPBACK,
    raise_app_exceptions: bool = True,
) -> httpx2.Response:
    """发一次请求；默认以环回客户端身份，`client` 就是模拟来源地址的位置。"""
    return anyio.run(
        _send, application, method, path, payload, client, raise_app_exceptions
    )


def _connection_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "label": "本地网关",
        "base_url": BASE_URL,
        "api_key": TEST_KEY,
        "models": [{"id": "m1", "label": "一号模型", "model": "alpha"}],
    }
    payload.update(overrides)
    return payload


def _base_registry(tmp_path: Path) -> ModelRegistry:
    """基础模型配置：两个在线条目，其中一个缺密钥因而不可用。

    密钥仍然只从环境映射里取 —— 基础配置这一层没有变。
    """
    path = tmp_path / "models.local.json"
    path.write_text(
        json.dumps(
            {
                "default": GENERATOR_EXTRACTIVE,
                "models": [
                    {
                        "id": "main",
                        "label": "主模型",
                        "base_url": "https://base.example.com/v1",
                        "model": "main-name",
                        "api_key_env": "MAIN_MODEL_KEY",
                    },
                    {
                        "id": "offline",
                        "label": "缺密钥的模型",
                        "base_url": "https://base.example.com/v1",
                        "model": "offline-name",
                        "api_key_env": "MISSING_MODEL_KEY",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return load_model_registry({"MAIN_MODEL_KEY": "sk-main-secret"}, path)


def _app(tmp_path: Path, *, discoverer: object | None = None, store=None):
    """装配一个带连接管理的应用；连接文件落在 tmp_path 里。"""
    path = Path(store.path) if store is not None else tmp_path / "model-connections.json"
    service = ModelConnectionService(
        _base_registry(tmp_path),
        store or ConnectionStore(path),
        discoverer=discoverer or FakeDiscoverer(),
    )
    application = create_app(
        InMemoryDocumentRepository(),
        model_registry=service.registry,
        model_connections=service,
    )
    return application, service


class FakeDiscoverer:
    """`ModelConnectionService` 的发现器替身：记录参数，返回预设清单。"""

    def __init__(self, models: tuple[str, ...] = ("alpha", "beta"), error=None) -> None:
        self.models = models
        self.error = error
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, base_url: str, api_key: str, timeout: float) -> tuple[str, ...]:
        self.calls.append((base_url, api_key, timeout))
        if self.error is not None:
            raise self.error
        return self.models


class FakeResponse:
    """`discover_models` 需要的响应对象：支持 `with`，并实现一次 `read(size)`。"""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, size: int | None = None) -> bytes:
        return self.body if size is None else self.body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exception: object) -> bool:
        return False


class FakeOpener:
    """`discover_models` 的 opener 替身：不碰网络，记录收到的 Request。"""

    def __init__(self, *, body: bytes = b"", error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body)


def _models_body(*names: object) -> bytes:
    return json.dumps({"object": "list", "data": [{"id": n} for n in names]}).encode(
        "utf-8"
    )


class StubModel:
    """离线替身模型：既能回答也能做候选抽取，问答与抽取因此不碰网络。"""

    name = "stub"
    model = "stub-model"

    def __init__(self) -> None:
        self.completions: list[tuple[str, str]] = []

    def generate(self, question: str, evidences) -> GeneratedAnswer:
        return GeneratedAnswer(
            claims=(
                Claim(
                    text="百日咳的推荐药物包括琥乙红霉素片。",
                    evidence_ids=(evidences[0].id,),
                ),
            )
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.completions.append((system_prompt, user_prompt))
        return '{"entities": [], "relations": []}'


def _stub_registry() -> tuple[ModelRegistry, StubModel]:
    """一份带离线替身的基础注册表；默认仍是内置的离线摘录。"""
    stub = StubModel()
    return (
        ModelRegistry(
            GENERATOR_EXTRACTIVE,
            (
                ModelEntry(
                    id=GENERATOR_EXTRACTIVE,
                    label=EXTRACTIVE_LABEL,
                    kind=KIND_EXTRACTIVE,
                ),
                ModelEntry(id="stub", label="离线替身", kind=KIND_OPENAI, model="stub-model"),
            ),
            {GENERATOR_EXTRACTIVE: ExtractiveAnswerGenerator(), "stub": stub},
        ),
        stub,
    )


def _stack():
    """百日咳 → 琥乙红霉素片 ← 小儿支原体肺炎 的最小可查询图谱。"""
    documents = InMemoryDocumentRepository()
    ingestion = TextIngestionService(documents)
    first = ingestion.ingest_text(
        "dutmed-百日咳-推荐药物.md", "百日咳的推荐药物包括琥乙红霉素片。"
    )
    second = ingestion.ingest_text(
        "dutmed-小儿支原体肺炎-推荐药物.md",
        "小儿支原体肺炎的推荐药物包括琥乙红霉素片。",
    )
    graph = InMemoryGraphRepository()
    graph.upsert_entity(Entity("d1", "百日咳", "Disease"))
    graph.upsert_entity(Entity("d2", "小儿支原体肺炎", "Disease"))
    graph.upsert_entity(Entity("m1", "琥乙红霉素片", "Drug"))
    graph.upsert_relation(
        Relation("r1", "d1", "m1", "RECOMMENDS_DRUG", (first.chunks[0].id,))
    )
    graph.upsert_relation(
        Relation("r2", "d2", "m1", "RECOMMENDS_DRUG", (second.chunks[0].id,))
    )
    return documents, GraphRetriever(documents, graph), graph, first.document.id


def _stub_app(tmp_path: Path, *, candidates=None):
    """图 + 离线替身模型 + 连接管理：用于问答与抽取的默认模型检查。"""
    documents, retriever, graph, document_id = _stack()
    base, stub = _stub_registry()
    service = ModelConnectionService(
        base,
        ConnectionStore(tmp_path / "model-connections.json"),
        discoverer=FakeDiscoverer(),
    )
    application = create_app(
        documents,
        retriever,
        graph_repository=graph,
        model_registry=service.registry,
        model_connections=service,
        adapter_registry=build_default_adapter_registry(),
        candidate_repository=candidates or InMemoryCandidateRepository(),
    )
    return application, service, stub, documents, document_id


# --------------------------------------------------------------------------
# 一、密钥只留在服务端
# --------------------------------------------------------------------------


def test_connection_key_is_persisted_but_never_returned(tmp_path) -> None:
    application, service = _app(tmp_path)

    created = call(
        application, "PUT", "/model-connections/local", _connection_payload(timeout=12)
    )

    assert created.status_code == 200
    assert created.json() == {
        "id": "local",
        "label": "本地网关",
        "base_url": BASE_URL,
        "timeout": 12.0,
        "has_api_key": True,
        "models": [{"id": "m1", "label": "一号模型", "model": "alpha"}],
    }
    # 密钥确实保存在运行数据目录的文件里 —— 它必须能被服务端再次使用。
    assert TEST_KEY in service.store.path.read_text(encoding="utf-8")
    # 但每一个查询接口都不回显它，连 Authorization 这个词都不出现。
    for path in ("/model-connections", "/models", "/system", "/healthz"):
        response = call(application, "GET", path)
        assert response.status_code == 200
        assert TEST_KEY not in response.text
        assert "Authorization" not in response.text
    listing = call(application, "GET", "/model-connections").json()
    assert listing["connections"][0]["has_api_key"] is True
    assert "api_key" not in listing["connections"][0]


def test_connection_file_only_holds_runtime_connections(tmp_path) -> None:
    """基础配置文件一个字节都不改：运行时连接只写在连接文件里。"""
    application, service = _app(tmp_path)
    config = tmp_path / "models.local.json"
    before = config.read_text(encoding="utf-8")

    call(application, "PUT", "/model-connections/local", _connection_payload())
    call(application, "PUT", "/models/default", {"model_id": "m1"})

    assert config.read_text(encoding="utf-8") == before
    assert json.loads(service.store.path.read_text(encoding="utf-8")) == {
        "version": 1,
        "default": "m1",
        "connections": [
            {
                "id": "local",
                "label": "本地网关",
                "base_url": BASE_URL,
                "api_key": TEST_KEY,
                "timeout": DEFAULT_TIMEOUT,
                "models": [{"id": "m1", "label": "一号模型", "model": "alpha"}],
            }
        ],
    }


def test_model_connection_repr_hides_the_key(tmp_path) -> None:
    _, service = _app(tmp_path)

    service.upsert(
        "local",
        base_url=BASE_URL,
        api_key=TEST_KEY,
        models=[{"id": "m1", "model": "alpha"}],
    )
    connection = service.store.load().connections[0]

    assert TEST_KEY not in repr(connection)
    assert "已隐藏" in repr(connection)


def test_discovery_errors_never_carry_the_key_or_a_chained_cause() -> None:
    """HTTPError 的原文里带着请求头（也就是密钥），因此刻意不链上它。"""
    upstream = HTTPError(
        f"{BASE_URL}/models",
        401,
        "Unauthorized",
        {"Authorization": f"Bearer {TEST_KEY}"},
        BytesIO(b""),
    )

    with pytest.raises(DiscoveryUnauthorizedError) as info:
        discover_models(BASE_URL, TEST_KEY, 5.0, opener=FakeOpener(error=upstream))

    assert info.value.__cause__ is None
    assert TEST_KEY not in str(info.value)
    rendered = "".join(
        traceback.format_exception(type(info.value), info.value, info.value.__traceback__)
    )
    assert TEST_KEY not in rendered


def test_no_log_output_contains_the_key(tmp_path, capsys) -> None:
    application, _ = _app(tmp_path)

    # 成功路径、发现失败、地址不合法、模型 ID 冲突与删除各走一遍。
    call(application, "PUT", "/model-connections/local", _connection_payload())
    call(application, "POST", "/model-connections/local/discover")
    call(
        application,
        "PUT",
        "/model-connections/other",
        {"base_url": "ftp://gateway.example.com/v1", "api_key": TEST_KEY},
    )
    call(
        application,
        "PUT",
        "/model-connections/other",
        _connection_payload(models=[{"id": "main", "model": "alpha"}]),
    )
    call(application, "DELETE", "/model-connections/local")

    captured = capsys.readouterr()
    assert TEST_KEY not in captured.out
    assert TEST_KEY not in captured.err


class FailingStore(ConnectionStore):
    """落盘必然失败的存储：验证「校验 → 落盘 → 替换」这条一致性边界。"""

    def save(self, data: ConnectionFile) -> None:
        raise OSError("磁盘写入失败")


# --------------------------------------------------------------------------
# 二、发现：地址、超时、响应体与响应结构
# --------------------------------------------------------------------------


def test_temporary_discovery_writes_no_file(tmp_path) -> None:
    discoverer = FakeDiscoverer(("alpha", "beta"))
    application, service = _app(tmp_path, discoverer=discoverer)

    response = call(
        application,
        "POST",
        "/model-connections/discover",
        {"base_url": BASE_URL, "api_key": TEST_KEY, "timeout": 9},
    )

    assert response.status_code == 200
    assert response.json() == {"base_url": BASE_URL, "models": ["alpha", "beta"]}
    assert discoverer.calls == [(BASE_URL, TEST_KEY, 9.0)]
    assert TEST_KEY not in response.text
    # 试算没有留下任何痕迹：文件不存在，连接清单仍然是空的。
    assert not service.store.path.exists()
    assert call(application, "GET", "/model-connections").json()["connections"] == []


def test_saved_connection_discovery_reuses_the_stored_key(tmp_path) -> None:
    discoverer = FakeDiscoverer()
    application, service = _app(tmp_path, discoverer=discoverer)
    call(application, "PUT", "/model-connections/local", _connection_payload(timeout=7))

    response = call(application, "POST", "/model-connections/local/discover")

    assert response.status_code == 200
    # 客户端没有再提交密钥，服务端用的是保存下来的那一份。
    assert discoverer.calls == [(BASE_URL, TEST_KEY, 7.0)]
    assert TEST_KEY not in response.text
    assert service.store.load().connections[0].api_key == TEST_KEY


def test_discovery_reads_only_openai_style_model_ids() -> None:
    opener = FakeOpener(
        body=json.dumps(
            {
                "object": "list",
                "data": [
                    {"id": "alpha"},
                    {"id": "beta"},
                    {"id": "alpha"},
                    {"id": "  "},
                    {"id": 7},
                    {"nope": 1},
                    "junk",
                    {"id": "gamma", "owned_by": "local"},
                ],
            }
        ).encode("utf-8")
    )

    models = discover_models(BASE_URL, TEST_KEY, 5.0, opener=opener)

    assert models == ("alpha", "beta", "gamma")
    request = opener.requests[0]
    assert request.full_url == f"{BASE_URL}/models"
    assert request.get_header("Authorization") == f"Bearer {TEST_KEY}"


def test_discovery_caps_the_model_count() -> None:
    # 清单条数上限：超出部分直接丢弃，不撑爆注册表。
    body = _models_body(*(f"model-{index}" for index in range(500)))

    models = discover_models(BASE_URL, TEST_KEY, 5.0, opener=FakeOpener(body=body))

    assert len(models) == 200
    assert models[0] == "model-0"
    assert models[-1] == "model-199"


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b'{"object": "list"}',
        b'{"data": {}}',
        b'[{"id": "alpha"}]',
    ],
)
def test_invalid_upstream_bodies_are_rejected(body: bytes) -> None:
    with pytest.raises(DiscoveryInvalidResponseError) as info:
        discover_models(BASE_URL, TEST_KEY, 5.0, opener=FakeOpener(body=body))

    assert TEST_KEY not in str(info.value)


def test_oversized_upstream_body_is_rejected() -> None:
    oversized = _models_body("alpha") + b" " * (MAX_RESPONSE_BYTES + 1)

    with pytest.raises(DiscoveryInvalidResponseError):
        discover_models(BASE_URL, TEST_KEY, 5.0, opener=FakeOpener(body=oversized))


def test_unreachable_upstream_is_reported_as_connection_failure() -> None:
    with pytest.raises(DiscoveryUnreachableError) as info:
        discover_models(
            BASE_URL, TEST_KEY, 5.0, opener=FakeOpener(error=OSError("connection refused"))
        )

    assert "无法连接" in str(info.value)
    assert TEST_KEY not in str(info.value)


def test_upstream_status_codes_map_to_safe_discovery_errors() -> None:
    for status, expected in ((401, DiscoveryUnauthorizedError), (500, DiscoveryUpstreamError)):
        error = HTTPError(f"{BASE_URL}/models", status, "err", {}, BytesIO(b""))
        with pytest.raises(expected) as info:
            discover_models(BASE_URL, TEST_KEY, 5.0, opener=FakeOpener(error=error))
        assert str(status) in str(info.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "gateway.example.com/v1",
        "ftp://gateway.example.com/v1",
        f"https://user:{TEST_KEY}@gateway.example.com/v1",
        "https://gateway.example.com/v1?key=1",
        "https://gateway.example.com/v1#frag",
        "   ",
    ],
)
def test_invalid_base_urls_are_rejected(tmp_path, base_url: str) -> None:
    application, service = _app(tmp_path)

    response = call(
        application,
        "POST",
        "/model-connections/discover",
        {"base_url": base_url, "api_key": TEST_KEY},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_base_url"
    assert TEST_KEY not in response.text
    assert not service.store.path.exists()


@pytest.mark.parametrize("timeout", [0, 0.5, 61, 3600])
def test_out_of_range_timeouts_are_rejected(tmp_path, timeout: float) -> None:
    application, _ = _app(tmp_path)

    response = call(
        application,
        "POST",
        "/model-connections/discover",
        {"base_url": BASE_URL, "api_key": TEST_KEY, "timeout": timeout},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_connection"
    assert TEST_KEY not in response.text


def test_discovery_failures_map_to_safe_status_codes(tmp_path) -> None:
    failure = DiscoveryUnauthorizedError("远端模型服务拒绝了这次请求。")
    application, _ = _app(tmp_path, discoverer=FakeDiscoverer(error=failure))

    response = call(
        application,
        "POST",
        "/model-connections/discover",
        {"base_url": BASE_URL, "api_key": TEST_KEY},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "discovery_unauthorized"


# --------------------------------------------------------------------------
# 三、只允许环回客户端调用管理接口
# --------------------------------------------------------------------------


MANAGEMENT_CALLS = [
    ("GET", "/model-connections", None),
    ("POST", "/model-connections/discover", {"base_url": BASE_URL, "api_key": TEST_KEY}),
    ("POST", "/model-connections/local/discover", None),
    ("PUT", "/model-connections/local", _connection_payload()),
    ("DELETE", "/model-connections/local", None),
    ("PUT", "/models/default", {"model_id": GENERATOR_EXTRACTIVE}),
]


@pytest.mark.parametrize("method, path, payload", MANAGEMENT_CALLS)
def test_management_endpoints_reject_non_loopback_clients(tmp_path, method, path, payload) -> None:
    application, service = _app(tmp_path)

    response = call(application, method, path, payload, client=REMOTE)

    assert response.status_code == 403
    assert response.json()["error_code"] == "management_forbidden"
    assert TEST_KEY not in response.text
    # 被拒绝的请求也没有留下任何配置。
    assert not service.store.path.exists()
    # 同一次调用换成环回客户端就不再是拒绝。
    assert call(application, method, path, payload).status_code != 403


@pytest.mark.parametrize("client", [("127.0.0.1", 1), ("::1", 1), ("::ffff:127.0.0.1", 1)])
def test_loopback_client_forms_are_accepted(tmp_path, client) -> None:
    application, _ = _app(tmp_path)

    assert call(application, "GET", "/model-connections", client=client).status_code == 200


def test_ordinary_endpoints_stay_open_to_non_loopback_clients(tmp_path) -> None:
    application, _ = _app(tmp_path)

    assert call(application, "GET", "/models", client=REMOTE).status_code == 200
    assert call(application, "GET", "/system", client=REMOTE).status_code == 200
    answer = call(
        application, "POST", "/query", {"question": "百日咳用什么药"}, client=REMOTE
    )
    assert answer.status_code == 200


def test_management_endpoints_report_when_not_wired() -> None:
    application = create_app(InMemoryDocumentRepository())

    response = call(application, "GET", "/model-connections")

    # 没装配连接管理就明确报不可用，而不是回一份空清单骗客户端。
    assert response.status_code == 503
    assert response.json()["error_code"] == "model_management_unavailable"


# --------------------------------------------------------------------------
# 四、连接的新增、更新、删除与默认模型
# --------------------------------------------------------------------------


def test_connection_lifecycle(tmp_path) -> None:
    application, service = _app(tmp_path)

    created = call(application, "PUT", "/model-connections/local", _connection_payload())
    assert created.status_code == 200
    assert call(application, "GET", "/models").json()["models"][-1]["id"] == "m1"

    updated = call(
        application,
        "PUT",
        "/model-connections/local",
        _connection_payload(label="改名后的网关", timeout=20),
    )
    assert updated.status_code == 200
    assert updated.json()["label"] == "改名后的网关"
    assert updated.json()["timeout"] == 20.0
    assert len(service.store.load().connections) == 1

    deleted = call(application, "DELETE", "/model-connections/local")

    assert deleted.status_code == 200
    assert deleted.json() == {"id": "local", "default": GENERATOR_EXTRACTIVE}
    assert service.store.load().connections == ()
    assert [item["id"] for item in call(application, "GET", "/models").json()["models"]] == [
        GENERATOR_EXTRACTIVE,
        "main",
        "offline",
    ]
    assert call(application, "GET", "/model-connections").json()["connections"] == []
    assert call(application, "DELETE", "/model-connections/local").status_code == 404


def test_update_without_a_key_keeps_the_stored_one(tmp_path) -> None:
    application, service = _app(tmp_path)
    call(application, "PUT", "/model-connections/local", _connection_payload())

    # 省略 api_key：沿用原密钥。
    kept = call(
        application, "PUT", "/model-connections/local", _connection_payload(label="新名字")
    )
    assert kept.status_code == 200
    assert kept.json()["has_api_key"] is True
    assert service.store.load().connections[0].api_key == TEST_KEY
    before = service.store.path.read_text(encoding="utf-8")

    # 显式给空字符串：拒绝，绝不悄悄把密钥覆盖成空。
    empty = call(
        application, "PUT", "/model-connections/local", _connection_payload(api_key="")
    )
    assert empty.status_code == 400
    assert empty.json()["error_code"] == "invalid_connection"
    assert service.store.path.read_text(encoding="utf-8") == before

    # 换一把密钥时才会被替换。
    call(application, "PUT", "/model-connections/local", _connection_payload(api_key=ROTATED_KEY))
    assert service.store.load().connections[0].api_key == ROTATED_KEY


def test_create_without_a_key_is_rejected(tmp_path) -> None:
    application, service = _app(tmp_path)
    payload = _connection_payload()
    payload.pop("api_key")

    response = call(application, "PUT", "/model-connections/local", payload)

    assert response.status_code == 400
    assert not service.store.path.exists()


def test_unknown_connection_returns_not_found(tmp_path) -> None:
    application, service = _app(tmp_path)

    assert call(application, "GET", "/model-connections").status_code == 200
    assert call(application, "POST", "/model-connections/missing/discover").status_code == 404
    assert call(application, "DELETE", "/model-connections/missing").status_code == 404
    with pytest.raises(ConnectionNotFoundError):
        service.delete("missing")


def test_builtin_extractive_cannot_be_a_connection_or_a_model(tmp_path) -> None:
    application, service = _app(tmp_path)

    as_connection = call(
        application, "PUT", f"/model-connections/{GENERATOR_EXTRACTIVE}", _connection_payload()
    )
    as_model = call(
        application,
        "PUT",
        "/model-connections/local",
        _connection_payload(models=[{"id": GENERATOR_EXTRACTIVE, "model": "alpha"}]),
    )
    deleted = call(application, "DELETE", f"/model-connections/{GENERATOR_EXTRACTIVE}")

    assert as_connection.status_code == 400
    assert as_model.status_code == 400
    assert deleted.status_code == 400
    assert not service.store.path.exists()


def test_default_model_change_is_visible_immediately(tmp_path) -> None:
    application, service = _app(tmp_path)
    call(application, "PUT", "/model-connections/local", _connection_payload())
    assert call(application, "GET", "/models").json()["default"] == GENERATOR_EXTRACTIVE

    changed = call(application, "PUT", "/models/default", {"model_id": "m1"})

    assert changed.status_code == 200
    # 返回的就是新的清单，界面一次刷新到位。
    assert changed.json()["default"] == "m1"
    assert call(application, "GET", "/models").json()["default"] == "m1"
    system = call(application, "GET", "/system").json()
    # /system 报的是当下生效的默认模型，不是启动时算好的那一份。
    assert system["generator"] == "m1"
    assert system["llm_configured"] == "true"
    assert system["llm_model"] == "alpha"
    assert TEST_KEY not in json.dumps(system, ensure_ascii=False)

    # 切回内置离线模型是正常操作。
    assert call(application, "PUT", "/models/default", {"model_id": GENERATOR_EXTRACTIVE}).status_code == 200
    assert service.store.load().default_id == GENERATOR_EXTRACTIVE


def test_unknown_and_unavailable_default_models_keep_existing_semantics(tmp_path) -> None:
    application, _ = _app(tmp_path)

    unknown = call(application, "PUT", "/models/default", {"model_id": "nope"})
    unavailable = call(application, "PUT", "/models/default", {"model_id": "offline"})

    # 与 /query、/extractions 的既有语义一致：未知 400，不可用 503。
    assert unknown.status_code == 400
    assert unknown.json()["error_code"] == "invalid_generator"
    assert unavailable.status_code == 503
    assert unavailable.json()["error_code"] == "generator_unavailable"


def test_query_without_a_model_uses_the_new_default(tmp_path) -> None:
    application, _, stub, _, _ = _stub_app(tmp_path)
    question = {"question": "百日咳用什么药", "max_hops": 2}

    before = call(application, "POST", "/query", question).json()
    assert before["metrics"]["generator"] == GENERATOR_EXTRACTIVE
    assert stub.completions == []

    assert call(application, "PUT", "/models/default", {"model_id": "stub"}).status_code == 200
    after = call(application, "POST", "/query", question).json()

    # 不指定模型的问答立刻改用新默认，不需要重启。
    assert after["status"] == "answered"
    assert after["metrics"]["requested_generator"] == "stub"
    assert after["metrics"]["generator"] == "stub"
    assert after["metrics"]["model"] == "stub-model"


def test_extraction_without_a_model_uses_the_new_default(tmp_path) -> None:
    candidates = InMemoryCandidateRepository()
    application, _, stub, _, document_id = _stub_app(tmp_path, candidates=candidates)

    before = call(
        application, "POST", "/extractions", {"document_id": document_id}
    ).json()
    assert before["model_id"] == GENERATOR_EXTRACTIVE

    assert call(application, "PUT", "/models/default", {"model_id": "stub"}).status_code == 200
    response = call(application, "POST", "/extractions", {"document_id": document_id})

    assert response.status_code == 200
    run = response.json()
    # 不指定模型的抽取同样落到新默认上，并且真的走到了那个模型。
    assert run["model_id"] == "stub"
    assert run["status"] == "succeeded"
    assert stub.completions


def test_deleting_the_default_connection_falls_back_to_extractive(tmp_path) -> None:
    application, service = _app(tmp_path)
    call(application, "PUT", "/model-connections/local", _connection_payload())
    assert call(application, "PUT", "/models/default", {"model_id": "m1"}).status_code == 200

    response = call(application, "DELETE", "/model-connections/local")

    # 默认模型跟着连接消失：回到内置离线选项，而不是留一个指向空处的值。
    assert response.json() == {"id": "local", "default": GENERATOR_EXTRACTIVE}
    assert call(application, "GET", "/models").json()["default"] == GENERATOR_EXTRACTIVE
    listing = call(application, "GET", "/model-connections").json()
    assert listing["default"] == GENERATOR_EXTRACTIVE
    assert service.registry.current.default_id == GENERATOR_EXTRACTIVE


def test_write_failure_keeps_the_old_file_and_registry(tmp_path) -> None:
    path = tmp_path / "model-connections.json"
    working = ModelConnectionService(
        _base_registry(tmp_path), ConnectionStore(path), discoverer=FakeDiscoverer()
    )
    working.upsert(
        "local",
        base_url=BASE_URL,
        api_key=TEST_KEY,
        models=[{"id": "m1", "model": "alpha"}],
    )
    working.set_default("m1")
    before_file = path.read_text(encoding="utf-8")
    before_models = working.registry.current.describe()

    failing = ModelConnectionService(
        _base_registry(tmp_path), FailingStore(path), discoverer=FakeDiscoverer()
    )
    with pytest.raises(OSError):
        failing.upsert(
            "second",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m2", "model": "beta"}],
        )
    with pytest.raises(OSError):
        failing.set_default(GENERATOR_EXTRACTIVE)

    # 落盘失败：磁盘与内存都还是上一份完整的配置。
    assert path.read_text(encoding="utf-8") == before_file
    assert failing.registry.current.describe() == before_models
    assert failing.registry.current.default_id == "m1"


def test_write_failure_through_the_api_returns_a_key_free_500(tmp_path) -> None:
    path = tmp_path / "model-connections.json"
    working = ModelConnectionService(
        _base_registry(tmp_path), ConnectionStore(path), discoverer=FakeDiscoverer()
    )
    working.upsert(
        "local",
        base_url=BASE_URL,
        api_key=TEST_KEY,
        models=[{"id": "m1", "model": "alpha"}],
    )
    failing = ModelConnectionService(
        _base_registry(tmp_path), FailingStore(path), discoverer=FakeDiscoverer()
    )
    application = create_app(
        InMemoryDocumentRepository(),
        model_registry=failing.registry,
        model_connections=failing,
    )

    response = call(
        application,
        "PUT",
        "/model-connections/second",
        _connection_payload(models=[{"id": "m2", "model": "beta"}]),
        raise_app_exceptions=False,
    )

    assert response.status_code == 500
    assert TEST_KEY not in response.text
    # 客户端读到的仍然是失败之前的那份配置。
    listing = call(application, "GET", "/model-connections").json()
    assert [item["id"] for item in listing["connections"]] == ["local"]
    assert call(application, "GET", "/models").json()["models"][-1]["id"] == "m1"


# --------------------------------------------------------------------------
# 五、与基础配置的兼容
# --------------------------------------------------------------------------


def test_runtime_connections_merge_with_the_base_config(tmp_path) -> None:
    application, _ = _app(tmp_path)
    call(application, "PUT", "/model-connections/local", _connection_payload())

    body = call(application, "GET", "/models").json()

    # 基础条目一个不少、顺序不变，运行时模型追加在后面。
    assert [entry["id"] for entry in body["models"]] == [
        GENERATOR_EXTRACTIVE,
        "main",
        "offline",
        "m1",
    ]
    assert body["default"] == GENERATOR_EXTRACTIVE
    assert body["models"][1]["model"] == "main-name"
    assert body["models"][2]["available"] is False
    assert body["models"][3]["kind"] == KIND_OPENAI
    assert body["models"][3]["available"] is True


def test_missing_connections_file_behaves_exactly_as_before(tmp_path) -> None:
    base = _base_registry(tmp_path)
    service = ModelConnectionService(
        base,
        ConnectionStore(tmp_path / "model-connections.json"),
        discoverer=FakeDiscoverer(),
    )

    assert not service.store.path.exists()
    assert service.registry.current.default_id == base.default_id
    assert service.registry.current.describe() == base.describe()
    assert service.describe() == {"default": base.default_id, "connections": []}


def test_conflicting_model_ids_are_rejected(tmp_path) -> None:
    application, service = _app(tmp_path)
    conflicting_with_base = call(
        application,
        "PUT",
        "/model-connections/local",
        _connection_payload(models=[{"id": "main", "model": "alpha"}]),
    )
    assert conflicting_with_base.status_code == 409
    assert conflicting_with_base.json()["error_code"] == "connection_conflict"

    created = call(
        application, "PUT", "/model-connections/first", _connection_payload()
    )
    assert created.status_code == 200
    conflicting_with_peer = call(
        application,
        "PUT",
        "/model-connections/second",
        _connection_payload(models=[{"id": "m1", "model": "beta"}]),
    )
    assert conflicting_with_peer.status_code == 409

    # 两次冲突都没有改变磁盘：只剩成功的那一条连接。
    assert [item.id for item in service.store.load().connections] == ["first"]
    assert TEST_KEY not in conflicting_with_peer.text


def test_connections_path_can_be_moved_by_environment() -> None:
    moved = load_connections_path({CONNECTIONS_PATH_ENV: "runtime/elsewhere.json"})

    assert moved == Path("runtime/elsewhere.json")
    assert load_connections_path({}) == Path("data/local/model-connections.json")


def test_default_connections_path_is_git_ignored() -> None:
    """默认位置的连接文件里是明文密钥，它必须被版本库显式忽略。"""
    root = Path(__file__).resolve().parents[1]
    ignored = [
        line.strip()
        for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    ]

    assert "data/local/model-connections.json" in ignored
    assert "data/local/" in ignored


@pytest.mark.skipif(os.name != "posix", reason="Windows 上 chmod 只影响只读位")
def test_connection_file_permissions_are_restricted(tmp_path) -> None:
    application, service = _app(tmp_path)
    call(application, "PUT", "/model-connections/local", _connection_payload())

    assert stat.S_IMODE(service.store.path.stat().st_mode) == 0o600


# --------------------------------------------------------------------------
# 六、损坏的文件与运行时替换
# --------------------------------------------------------------------------


def test_corrupt_connection_file_is_reported_without_leaking(tmp_path) -> None:
    path = tmp_path / "model-connections.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "connections": [
                    {
                        "id": "broken",
                        "base_url": "not-a-url",
                        "api_key": TEST_KEY,
                        "models": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConnectionFileError) as info:
        ConnectionStore(path).load()
    message = str(info.value)
    assert TEST_KEY not in message
    # 只报位置与规则，不回显文件内容。
    assert "base_url" in message
    assert "not-a-url" not in message

    with pytest.raises(ConnectionFileError):
        ModelConnectionService(_base_registry(tmp_path), ConnectionStore(path))


def test_unparseable_connection_file_reports_without_leaking(tmp_path) -> None:
    path = tmp_path / "model-connections.json"
    path.write_text('{"api_key": "' + TEST_KEY + '",', encoding="utf-8")

    with pytest.raises(ConnectionFileError) as info:
        ConnectionStore(path).load()

    assert TEST_KEY not in str(info.value)


def test_unknown_file_version_is_rejected(tmp_path) -> None:
    path = tmp_path / "model-connections.json"
    path.write_text(json.dumps({"version": 99, "connections": []}), encoding="utf-8")

    with pytest.raises(ConnectionFileError):
        ConnectionStore(path).load()


def test_concurrent_reads_never_mix_default_id_and_generator() -> None:
    """替换注册表时读到的必须是完整的一份表：ID 与生成器不能来自两份快照。"""
    extractive = ExtractiveAnswerGenerator()
    stub = StubModel()
    first = ModelRegistry(
        GENERATOR_EXTRACTIVE,
        (
            ModelEntry(
                id=GENERATOR_EXTRACTIVE, label=EXTRACTIVE_LABEL, kind=KIND_EXTRACTIVE
            ),
        ),
        {GENERATOR_EXTRACTIVE: extractive},
    )
    second = ModelRegistry(
        "stub",
        (ModelEntry(id="stub", label="离线替身", kind=KIND_OPENAI, model="stub-model"),),
        {"stub": stub},
    )
    handle = RefreshingModelRegistry(first)
    expected = {GENERATOR_EXTRACTIVE: extractive, "stub": stub}
    readings: list[tuple[str, object]] = []
    stop = threading.Event()

    def read_repeatedly() -> None:
        while not stop.is_set():
            model_id, generator = handle.current.resolve_selection(None)
            readings.append((model_id, generator))

    readers = [threading.Thread(target=read_repeatedly) for _ in range(4)]
    for reader in readers:
        reader.start()
    try:
        for index in range(2000):
            handle.replace(second if index % 2 else first)
    finally:
        stop.set()
        for reader in readers:
            reader.join()

    assert readings
    assert {model_id for model_id, _ in readings} == set(expected)
    for model_id, generator in readings:
        assert generator is expected[model_id]
    assert handle.current in (first, second)
    assert handle.current.resolve_selection(None)[1] is handle.current.default_generator


# --------------------------------------------------------------------------
# 七、并发写的一致性
# --------------------------------------------------------------------------
#
# 并发用例不用「睡一会儿看结果」来碰运气：所有交错都由事件闸门钉死在确定
# 的一刻 —— 某条事务停在写之前或写之后，另一条再发出，然后放行。这样既没有
# 依赖时序的等待，也不需要真实网络。


class InterleavingStore(ConnectionStore):
    """可以在写入前后停住的存储，用来构造确定性的并发交错。

    两个位置对应配置事务里两个真实的窗口：

    - `pause_before_write`：已经读完旧配置、还没落盘 —— 修复前，另一个写
      事务正是在这里读到同一份旧配置，于是后落盘的那份把前一份整条覆盖；
    - `pause_after_write`：文件已更新、运行时快照还没替换 —— 读接口正是在
      这里可能把新一代的连接清单和上一代的默认模型拼在一起。

    `overlaps` 是回归探针：上一次写事务还没落盘时，又有一次读写重新加载了
    配置，就会被记下一笔。加了事务锁的正确实现下它必然始终为空。
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._guard = threading.Lock()
        self._writing = False
        self.overlaps: list[str] = []
        self.writes: list[tuple[str, ...]] = []
        self.pause_before_write = False
        self.pause_after_write = False
        self.write_started = threading.Event()
        self.release_write = threading.Event()
        self.file_written = threading.Event()
        self.release_replace = threading.Event()

    def load(self) -> ConnectionFile:
        with self._guard:
            if self._writing:
                self.overlaps.append("上一次写还没落盘，这次操作就重新加载了配置")
        return super().load()

    def save(self, data: ConnectionFile) -> None:
        with self._guard:
            pause_before = self.pause_before_write
            self.pause_before_write = False
            self._writing = True
        try:
            if pause_before:
                self.write_started.set()
                # 这里的超时只是兜底：真出问题时要让测试失败，而不是把整套
                # 用例永久挂住。放行由测试线程负责，正常路径不会等满。
                assert self.release_write.wait(timeout=10)
            super().save(data)
            with self._guard:
                self.writes.append(tuple(item.id for item in data.connections))
            if self.pause_after_write:
                self.pause_after_write = False
                self.file_written.set()
                assert self.release_replace.wait(timeout=10)
        finally:
            with self._guard:
                self._writing = False


class BlockingDiscoverer(FakeDiscoverer):
    """会一直卡住的远程发现：用来验证请求远端期间没有攥着事务锁。"""

    def __init__(self) -> None:
        super().__init__(("alpha", "beta"))
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, base_url: str, api_key: str, timeout: float) -> tuple[str, ...]:
        self.calls.append((base_url, api_key, timeout))
        self.entered.set()
        assert self.release.wait(timeout=10)
        return self.models


class _NoLock:
    """空锁：只在这一个测试里还原「修复前没有事务锁」的行为。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exception: object) -> bool:
        return False


class Worker:
    """在后台线程里跑一次服务调用，并留住结果或异常。

    线程里的异常不往上冒，所以必须自己收住 —— 否则写操作静默失败会被
    读成「另一个写没生效」，把断言引到错误的方向。
    """

    def __init__(self, action) -> None:
        self.action = action
        self.started = threading.Event()
        self.result: object = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run)

    def _run(self) -> None:
        self.started.set()
        try:
            self.result = self.action()
        except BaseException as error:  # noqa: BLE001 - 测试要留住任何失败
            self.error = error

    def start(self) -> "Worker":
        self.thread.start()
        return self

    def join(self) -> "Worker":
        # 超时是死锁兜底：真被锁住了要报错，不能让用例永久挂住。
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "后台的配置写操作没有结束（疑似被锁住）"
        return self


def _service(tmp_path: Path, store, discoverer=None) -> ModelConnectionService:
    return ModelConnectionService(
        _base_registry(tmp_path), store, discoverer=discoverer or FakeDiscoverer()
    )


def _ids(service: ModelConnectionService) -> list[str]:
    return [item.id for item in service.store.load().connections]


def _assert_disk_matches_registry(service: ModelConnectionService) -> None:
    """磁盘配置与运行时快照必须是同一代：文件里的模型都能在表里找到。"""
    data = service.store.load()
    expected = {entry.id for entry in service.base.entries} | {
        model.id for connection in data.connections for model in connection.models
    }
    assert {entry.id for entry in service.registry.current.entries} == expected
    assert service.registry.current.default_id == (
        data.default_id or service.base.default_id
    )


def test_concurrent_inserts_keep_both_connections(tmp_path) -> None:
    """两条新增并发执行，最终两条都得留下 —— 这就是原问题的最短复现。"""
    store = InterleavingStore(tmp_path / "model-connections.json")
    store.pause_before_write = True
    service = _service(tmp_path, store)

    first = Worker(
        lambda: service.upsert(
            "first",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m1", "model": "alpha"}],
        )
    ).start()
    # 第一条正停在「读完了、还没落盘」：修复前，第二条就是在这里读到同一份
    # 旧配置的。加锁之后它会一直等到整条事务结束。
    assert store.write_started.wait(timeout=10)
    second = Worker(
        lambda: service.upsert(
            "second",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m2", "model": "beta"}],
        )
    ).start()
    assert second.started.wait(timeout=10)
    store.release_write.set()
    first.join()
    second.join()

    assert first.error is None and second.error is None
    assert _ids(service) == ["first", "second"]
    assert store.overlaps == []
    _assert_disk_matches_registry(service)


def test_concurrent_updates_do_not_lose_one_of_them(tmp_path) -> None:
    """两条连接各自被并发更新：后写入的那份不能把前一份的改动抹掉。"""
    store = InterleavingStore(tmp_path / "model-connections.json")
    service = _service(tmp_path, store)
    for connection_id, model_id in (("first", "m1"), ("second", "m2")):
        service.upsert(
            connection_id,
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": model_id, "model": "alpha"}],
        )

    store.pause_before_write = True
    renaming = Worker(
        lambda: service.upsert(
            "first",
            base_url=BASE_URL,
            label="改过的第一条",
            models=[{"id": "m1", "model": "alpha"}],
        )
    ).start()
    assert store.write_started.wait(timeout=10)
    rekeying = Worker(
        lambda: service.upsert(
            "second",
            base_url=BASE_URL,
            label="改过的第二条",
            api_key=ROTATED_KEY,
            models=[{"id": "m2", "model": "beta"}],
        )
    ).start()
    assert rekeying.started.wait(timeout=10)
    store.release_write.set()
    renaming.join()
    rekeying.join()

    assert renaming.error is None and rekeying.error is None
    saved = service.store.load().connections
    assert [item.label for item in saved] == ["改过的第一条", "改过的第二条"]
    assert [item.api_key for item in saved] == [TEST_KEY, ROTATED_KEY]
    assert store.overlaps == []
    _assert_disk_matches_registry(service)


def test_concurrent_default_change_and_insert_are_both_kept(tmp_path) -> None:
    """改默认模型与新增连接同时发生：两处改动都要落在同一份配置上。"""
    store = InterleavingStore(tmp_path / "model-connections.json")
    service = _service(tmp_path, store)
    service.upsert(
        "local",
        base_url=BASE_URL,
        api_key=TEST_KEY,
        models=[{"id": "m1", "model": "alpha"}],
    )

    store.pause_before_write = True
    adding = Worker(
        lambda: service.upsert(
            "second",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m2", "model": "beta"}],
        )
    ).start()
    assert store.write_started.wait(timeout=10)
    switching = Worker(lambda: service.set_default("m1")).start()
    assert switching.started.wait(timeout=10)
    store.release_write.set()
    adding.join()
    switching.join()

    assert adding.error is None and switching.error is None
    assert _ids(service) == ["local", "second"]
    assert service.registry.current.default_id == "m1"
    assert store.overlaps == []
    _assert_disk_matches_registry(service)


def test_describe_never_combines_two_generations(tmp_path) -> None:
    """删掉默认连接会同时改动清单与默认模型，读接口不能各取一代。"""
    store = InterleavingStore(tmp_path / "model-connections.json")
    service = _service(tmp_path, store)
    service.upsert(
        "local",
        base_url=BASE_URL,
        api_key=TEST_KEY,
        models=[{"id": "m1", "model": "alpha"}],
    )
    service.set_default("m1")

    # 停在「文件已更新、快照还没换」的那一刻：不加锁的读正是在这里读到新
    # 文件配旧注册表 —— 清单里已经没有 local，默认却还指着 m1。
    store.pause_after_write = True
    removing = Worker(lambda: service.delete("local")).start()
    assert store.file_written.wait(timeout=10)
    reader = Worker(service.describe).start()
    assert reader.started.wait(timeout=10)
    store.release_replace.set()
    removing.join()
    reader.join()

    assert removing.error is None and reader.error is None
    body = reader.result
    assert body is not None
    pair = (body["default"], [item["id"] for item in body["connections"]])
    assert pair in (("m1", ["local"]), (GENERATOR_EXTRACTIVE, []))
    assert store.overlaps == []
    _assert_disk_matches_registry(service)


def test_a_failing_write_leaves_the_other_transaction_intact(tmp_path) -> None:
    """一条写失败时，另一条并发写留下的配置在磁盘与内存里都必须完好。"""
    path = tmp_path / "model-connections.json"
    calls = {"count": 0}

    class SecondSaveFails(InterleavingStore):
        def save(self, data: ConnectionFile) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("磁盘写入失败")
            super().save(data)

    store = SecondSaveFails(path)
    store.pause_before_write = True
    service = _service(tmp_path, store)

    saving = Worker(
        lambda: service.upsert(
            "first",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m1", "model": "alpha"}],
        )
    ).start()
    assert store.write_started.wait(timeout=10)
    failing = Worker(
        lambda: service.upsert(
            "second",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m2", "model": "beta"}],
        )
    ).start()
    assert failing.started.wait(timeout=10)
    store.release_write.set()
    saving.join()
    failing.join()

    assert saving.error is None
    assert isinstance(failing.error, OSError)
    # 失败的那条既没有落盘也没有进快照；成功的那条在两个地方都在。
    assert _ids(service) == ["first"]
    assert service.registry.current.entry("m2") is None
    assert store.overlaps == []
    _assert_disk_matches_registry(service)


def test_a_slow_discovery_does_not_block_configuration_writes(tmp_path) -> None:
    """远端发现可以卡很久，但不能把本机的连接增删改锁在门外。"""
    discoverer = BlockingDiscoverer()
    service = _service(
        tmp_path, ConnectionStore(tmp_path / "model-connections.json"), discoverer
    )
    service.upsert(
        "local",
        base_url=BASE_URL,
        api_key=TEST_KEY,
        models=[{"id": "m1", "model": "alpha"}],
    )

    discovery = Worker(lambda: service.discover_connection("local")).start()
    assert discoverer.entered.wait(timeout=10)

    # 发现还卡在远端时，一次配置修改必须能照常跑完 —— 如果它在锁上等，
    # join() 会因超时直接断言失败，而不是悄悄挂住。
    rotated = Worker(
        lambda: service.upsert(
            "local",
            base_url=BASE_URL,
            label="换过密钥的连接",
            api_key=ROTATED_KEY,
            models=[{"id": "m1", "model": "alpha"}],
        )
    ).start()
    assert rotated.started.wait(timeout=10)
    rotated.join()
    assert rotated.error is None

    discoverer.release.set()
    discovery.join()
    assert discovery.error is None
    # 本次发现用的是开始时取到的那一份快照：地址与密钥都是旧的。
    assert discoverer.calls == [(BASE_URL, TEST_KEY, DEFAULT_TIMEOUT)]
    assert discovery.result == {"base_url": BASE_URL, "models": ["alpha", "beta"]}
    assert service.store.load().connections[0].api_key == ROTATED_KEY


def test_the_lock_is_what_prevents_the_lost_update(tmp_path) -> None:
    """摘掉事务锁，同一个交错就真的会丢一条连接。

    这条用例是上面那几条的对照组：它证明「两条并发新增都留下」不是空跑 ——
    把锁换成空实现、复现修复前的行为，后落盘的那份就把前一份整条覆盖掉。
    """
    store = InterleavingStore(tmp_path / "model-connections.json")
    store.pause_before_write = True
    service = _service(tmp_path, store)
    service._lock = _NoLock()

    first = Worker(
        lambda: service.upsert(
            "first",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m1", "model": "alpha"}],
        )
    ).start()
    assert store.write_started.wait(timeout=10)
    # 没有锁，第二条整条跑完 —— 它读到的是还没有 first 的那份旧配置。
    second = Worker(
        lambda: service.upsert(
            "second",
            base_url=BASE_URL,
            api_key=TEST_KEY,
            models=[{"id": "m2", "model": "beta"}],
        )
    ).start()
    second.join()
    assert second.error is None
    store.release_write.set()
    first.join()
    assert first.error is None

    # 后落盘的第一条覆盖了第二条：磁盘上只剩一条连接，而且探针确实记下了
    # 这次「上一条还没落盘就又重新读配置」的交错。
    assert _ids(service) == ["first"]
    assert store.overlaps != []
    assert service.registry.current.entry("m2") is None
