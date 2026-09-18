"""抽取任务历史接口与候选仓储 list_runs 的回归检查。

任务是「按 Workspace 隔离 + 可按文档过滤 + 按创建时间倒序」三件事，因此
两种候选仓储实现（内存与 SQLite）必须给出同一份结果。
"""

from fastapi.testclient import TestClient
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ID,
    ExtractionRun,
    ExtractionStatus,
    Workspace,
)
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.generation.models import ModelEntry, ModelRegistry
from tracegraph.generation.providers import ExtractiveAnswerGenerator
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.storage.candidates import (
    InMemoryCandidateRepository,
    SQLiteCandidateRepository,
)
from tracegraph.storage.memory import InMemoryDocumentRepository
from tracegraph.storage.sqlite import SQLiteDocumentRepository

_OTHER = "ws-b"
_CREATED_AT = "2026-01-01T00:00:00+00:00"

FIRST_MARKDOWN = """# 百日咳

## 症状

咳嗽、低热
"""

SECOND_MARKDOWN = """# 小儿支原体肺炎

## 推荐药物

琥乙红霉素片
"""


def _registry() -> ModelRegistry:
    entry = ModelEntry(id="extractive", label="extractive", kind="extractive", model="t")
    return ModelRegistry("extractive", (entry,), {"extractive": ExtractiveAnswerGenerator()})


def _run(
    run_id: str,
    document_id: str,
    version_id: str,
    created_at: str,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> ExtractionRun:
    return ExtractionRun(
        id=run_id,
        workspace_id=workspace_id,
        document_id=document_id,
        document_version_id=version_id,
        adapter_id="medical",
        model_id="extractive",
        status=ExtractionStatus.RUNNING,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture
def client():
    documents = InMemoryDocumentRepository()
    candidates = InMemoryCandidateRepository()
    documents.save_workspace(
        Workspace(id=_OTHER, name=_OTHER, adapter_id="medical", created_at=_CREATED_AT)
    )
    application = create_app(
        repository=documents,
        candidate_repository=candidates,
        adapter_registry=build_default_adapter_registry(),
        model_registry=_registry(),
    )
    with TestClient(application) as test_client:
        ingestion = TextIngestionService(documents)
        test_client.first = ingestion.ingest_text(
            "first.md", FIRST_MARKDOWN, DEFAULT_WORKSPACE_ID
        )
        test_client.second = ingestion.ingest_text(
            "second.md", SECOND_MARKDOWN, DEFAULT_WORKSPACE_ID
        )
        test_client.foreign = ingestion.ingest_text("foreign.md", FIRST_MARKDOWN, _OTHER)
        # 走接口各抽一次，另外两条任务直接落库 —— 接口生成的任务时间戳来自
        # 真实时钟，注入的两条才能给出确定的先后顺序。
        test_client.first_run = test_client.post(
            "/extractions",
            json={
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "document_id": test_client.first.document.id,
            },
        ).json()
        test_client.foreign_run = test_client.post(
            "/extractions",
            json={"workspace_id": _OTHER, "document_id": test_client.foreign.document.id},
        ).json()
        candidates.save_run(
            _run(
                "run-second-old",
                test_client.second.document.id,
                test_client.second.version.id,
                "2026-01-02T00:00:00+00:00",
            )
        )
        candidates.save_run(
            _run(
                "run-second-new",
                test_client.second.document.id,
                test_client.second.version.id,
                "2026-01-03T00:00:00+00:00",
            )
        )
        yield test_client


def test_the_endpoint_exists_with_the_expected_shape(client) -> None:
    paths = client.app.openapi()["paths"]

    assert set(paths["/workspaces/{workspace_id}/extractions"]) == {"get"}


def test_runs_carry_every_field_the_workspace_needs(client) -> None:
    response = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/extractions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_id"] == DEFAULT_WORKSPACE_ID
    first = next(
        run for run in payload["runs"] if run["id"] == client.first_run["id"]
    )
    assert set(first) == {
        "id",
        "workspace_id",
        "document_id",
        "document_version_id",
        "adapter_id",
        "model_id",
        "status",
        "entity_count",
        "relation_count",
        "error",
        "created_at",
        "updated_at",
    }
    assert first["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert first["document_id"] == client.first.document.id
    assert first["document_version_id"] == client.first.version.id
    assert first["status"] == "succeeded"


def test_runs_are_isolated_by_workspace(client) -> None:
    own = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/extractions").json()
    other = client.get(f"/workspaces/{_OTHER}/extractions").json()

    assert client.foreign_run["id"] not in {run["id"] for run in own["runs"]}
    assert [run["id"] for run in other["runs"]] == [client.foreign_run["id"]]
    assert all(run["workspace_id"] == _OTHER for run in other["runs"])


def test_unknown_workspace_is_reported_as_such(client) -> None:
    response = client.get("/workspaces/ws-missing/extractions")

    assert response.status_code == 404
    assert response.json()["error_code"] == "workspace_not_found"


def test_document_filter_keeps_only_that_document(client) -> None:
    response = client.get(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/extractions",
        params={"document_id": client.second.document.id},
    )

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert [run["id"] for run in runs] == ["run-second-new", "run-second-old"]
    assert all(run["document_id"] == client.second.document.id for run in runs)


def test_every_workspace_run_is_returned_newest_first(client) -> None:
    runs = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/extractions").json()["runs"]

    assert {run["id"] for run in runs} == {
        client.first_run["id"],
        "run-second-old",
        "run-second-new",
    }
    stamps = [run["created_at"] for run in runs]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.parametrize("implementation", ["memory", "sqlite"])
def test_both_candidate_repositories_list_runs_alike(
    tmp_path, implementation: str
) -> None:
    database = tmp_path / "tracegraph.db"
    documents = SQLiteDocumentRepository(database)
    documents.save_workspace(
        Workspace(id=_OTHER, name=_OTHER, adapter_id="medical", created_at=_CREATED_AT)
    )
    ingestion = TextIngestionService(documents)
    first = ingestion.ingest_text("first.md", FIRST_MARKDOWN, DEFAULT_WORKSPACE_ID)
    second = ingestion.ingest_text("second.md", SECOND_MARKDOWN, DEFAULT_WORKSPACE_ID)
    foreign = ingestion.ingest_text("foreign.md", FIRST_MARKDOWN, _OTHER)

    with SQLiteCandidateRepository(database) as sqlite_candidates:
        candidates = (
            sqlite_candidates if implementation == "sqlite" else InMemoryCandidateRepository()
        )
        candidates.save_run(
            _run(
                "run-second-old",
                second.document.id,
                second.version.id,
                "2026-01-02T00:00:00+00:00",
            )
        )
        candidates.save_run(
            _run(
                "run-second-new",
                second.document.id,
                second.version.id,
                "2026-01-03T00:00:00+00:00",
            )
        )
        candidates.save_run(
            _run(
                "run-first",
                first.document.id,
                first.version.id,
                "2026-01-01T00:00:00+00:00",
            )
        )
        candidates.save_run(
            _run(
                "run-foreign",
                foreign.document.id,
                foreign.version.id,
                "2026-01-04T00:00:00+00:00",
                workspace_id=_OTHER,
            )
        )

        everything = candidates.list_runs(DEFAULT_WORKSPACE_ID)
        only_second = candidates.list_runs(
            DEFAULT_WORKSPACE_ID, document_id=second.document.id
        )
        only_other = candidates.list_runs(_OTHER)

        documents.close()

    assert [run.id for run in everything] == [
        "run-second-new",
        "run-second-old",
        "run-first",
    ]
    assert [run.id for run in only_second] == ["run-second-new", "run-second-old"]
    assert [run.id for run in only_other] == ["run-foreign"]
