"""审核、发布、Workspace 文档列表与图检索接口的端到端检查。"""

from fastapi.testclient import TestClient
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import DEFAULT_WORKSPACE_ID, Workspace
from tracegraph.domains.registry import build_default_adapter_registry
from tracegraph.generation.models import ModelEntry, ModelRegistry
from tracegraph.generation.providers import ExtractiveAnswerGenerator
from tracegraph.ingestion.lifecycle import DocumentLifecycleService
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.storage.candidates import InMemoryCandidateRepository
from tracegraph.storage.graph import InMemoryGraphRepository
from tracegraph.storage.memory import InMemoryDocumentRepository

_OTHER = "ws-b"
_CREATED_AT = "2026-01-01T00:00:00+00:00"

DUTMED_MARKDOWN = """# 百日咳

## 症状

咳嗽、低热

## 推荐药物

琥乙红霉素片
"""

OTHER_MARKDOWN = """# 小儿支原体肺炎

## 推荐药物

琥乙红霉素片
"""


def _registry() -> ModelRegistry:
    entry = ModelEntry(id="extractive", label="extractive", kind="extractive", model="t")
    return ModelRegistry("extractive", (entry,), {"extractive": ExtractiveAnswerGenerator()})


@pytest.fixture
def client():
    documents = InMemoryDocumentRepository()
    candidates = InMemoryCandidateRepository()
    graph = InMemoryGraphRepository()
    documents.save_workspace(
        Workspace(
            id=_OTHER, name=_OTHER, adapter_id="medical", created_at=_CREATED_AT
        )
    )
    application = create_app(
        repository=documents,
        candidate_repository=candidates,
        graph_repository=graph,
        adapter_registry=build_default_adapter_registry(),
        model_registry=_registry(),
    )
    with TestClient(application) as test_client:
        test_client.documents = documents
        test_client.candidates = candidates
        test_client.graph = graph
        test_client.own = TextIngestionService(documents).ingest_text(
            "dutmed-百日咳.md", DUTMED_MARKDOWN, DEFAULT_WORKSPACE_ID
        )
        test_client.foreign = TextIngestionService(documents).ingest_text(
            "dutmed-支原体肺炎.md", OTHER_MARKDOWN, _OTHER
        )
        test_client.own_run = test_client.post(
            "/extractions",
            json={
                "workspace_id": DEFAULT_WORKSPACE_ID,
                "document_id": test_client.own.document.id,
            },
        ).json()
        test_client.foreign_run = test_client.post(
            "/extractions",
            json={"workspace_id": _OTHER, "document_id": test_client.foreign.document.id},
        ).json()
        yield test_client


def _candidates(client, run_id: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict:
    return client.get(f"/workspaces/{workspace_id}/candidates").json()


def _ids(client, run_id: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
    payload = client.get(f"/extractions/{run_id}/candidates").json()
    return (
        [item["id"] for item in payload["entities"]],
        [item["id"] for item in payload["relations"]],
    )


def _approve_all(client, run_id: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
    entity_ids, relation_ids = _ids(client, run_id, workspace_id)
    first = client.post(
        "/candidates/batch-review",
        json={
            "workspace_id": workspace_id,
            "status": "approved",
            "entity_ids": entity_ids,
            "relation_ids": [],
        },
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/candidates/batch-review",
        json={
            "workspace_id": workspace_id,
            "status": "approved",
            "entity_ids": [],
            "relation_ids": relation_ids,
        },
    )
    assert second.status_code == 200, second.text


def test_review_endpoints_exist_with_the_expected_shapes(client) -> None:
    paths = client.app.openapi()["paths"]

    assert set(paths["/candidate-entities/{candidate_id}"]) == {"patch"}
    assert set(paths["/candidate-relations/{candidate_id}"]) == {"patch"}
    assert set(paths["/candidates/batch-review"]) == {"post"}
    assert set(paths["/workspaces/{workspace_id}/graph-publications"]) == {"post"}
    assert set(paths["/workspaces/{workspace_id}/documents"]) == {"get"}


def test_entity_can_be_approved_then_returned_to_pending(client) -> None:
    entity_ids, _ = _ids(client, client.own_run["id"])

    approved = client.patch(
        f"/candidate-entities/{entity_ids[0]}",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "status": "approved"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    back = client.patch(
        f"/candidate-entities/{entity_ids[0]}",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "status": "pending"},
    )
    assert back.json()["status"] == "pending"


def test_illegal_transition_returns_a_conflict_code(client) -> None:
    entity_ids, _ = _ids(client, client.own_run["id"])
    client.patch(
        f"/candidate-entities/{entity_ids[0]}",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "status": "approved"},
    )

    rejected = client.patch(
        f"/candidate-entities/{entity_ids[0]}",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "status": "rejected"},
    )

    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "illegal_candidate_transition"


def test_cross_workspace_review_is_rejected(client) -> None:
    entity_ids, _ = _ids(client, client.own_run["id"])

    response = client.patch(
        f"/candidate-entities/{entity_ids[0]}",
        json={"workspace_id": _OTHER, "status": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "candidate_workspace_mismatch"


def test_missing_candidate_returns_404(client) -> None:
    response = client.patch(
        "/candidate-entities/cand-missing",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "status": "approved"},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"


def test_batch_review_rolls_back_entirely(client) -> None:
    entity_ids, relation_ids = _ids(client, client.own_run["id"])
    client.patch(
        f"/candidate-entities/{entity_ids[0]}",
        json={"workspace_id": DEFAULT_WORKSPACE_ID, "status": "approved"},
    )

    response = client.post(
        "/candidates/batch-review",
        json={
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "status": "approved",
            "entity_ids": entity_ids,
            "relation_ids": relation_ids,
        },
    )

    assert response.status_code == 409
    remaining = client.get(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/candidates", params={"status": "approved"}
    ).json()
    # 只有先前单独批准的那一条是 approved，整批没有落地。
    assert [item["id"] for item in remaining["entities"]] == [entity_ids[0]]


def test_publishing_pending_candidates_is_rejected(client) -> None:
    entity_ids, _ = _ids(client, client.own_run["id"])

    response = client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"candidate_entity_ids": entity_ids, "candidate_relation_ids": []},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "candidate_not_approved"
    assert client.graph.statistics(DEFAULT_WORKSPACE_ID).entities == 0


def test_publishing_an_approved_batch_and_repeating_it(client) -> None:
    _approve_all(client, client.own_run["id"])
    entity_ids, relation_ids = _ids(client, client.own_run["id"])
    payload = {
        "candidate_entity_ids": entity_ids,
        "candidate_relation_ids": relation_ids,
    }

    first = client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications", json=payload
    )
    second = client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications", json=payload
    )

    assert first.status_code == 200, first.text
    assert first.json()["counts"]["entities_created"] == len(entity_ids)
    assert first.json()["counts"]["relations_created"] == len(relation_ids)
    assert first.json()["backend"] == "memory"
    # 重复发布不新增任何东西。
    assert second.json()["counts"]["entities_created"] == 0
    assert second.json()["counts"]["relations_created"] == 0
    assert second.json()["counts"]["entities_skipped"] == len(entity_ids)
    assert second.json()["counts"]["relations_skipped"] == len(relation_ids)
    assert client.graph.statistics(DEFAULT_WORKSPACE_ID).entities == len(entity_ids)


def test_published_candidates_expose_their_graph_mapping(client) -> None:
    _approve_all(client, client.own_run["id"])
    client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.own_run["id"]},
    )

    payload = client.get(
        f"/extractions/{client.own_run['id']}/candidates"
    ).json()

    for candidate in (*payload["entities"], *payload["relations"]):
        assert candidate["workspace_id"] == DEFAULT_WORKSPACE_ID
        assert candidate["is_published"] is True
        assert candidate["published_at"]
        assert candidate["graph_id"]


def test_publishing_a_run_publishes_everything_approved(client) -> None:
    _approve_all(client, client.own_run["id"])

    response = client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.own_run["id"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["entities_created"] > 0


def test_publishing_a_foreign_run_is_rejected(client) -> None:
    _approve_all(client, client.foreign_run["id"], _OTHER)

    response = client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.foreign_run["id"]},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "candidate_workspace_mismatch"


def test_published_graph_is_visible_through_the_graph_endpoints(client) -> None:
    _approve_all(client, client.own_run["id"])
    published = client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.own_run["id"]},
    ).json()

    search = client.get(
        "/graph/entities",
        params={"workspace_id": DEFAULT_WORKSPACE_ID, "query": "百日咳"},
    )
    assert search.status_code == 200
    assert search.json()["workspace_id"] == DEFAULT_WORKSPACE_ID
    entity = search.json()["entities"][0]
    assert entity["id"] in published["created_entity_ids"]

    relations = client.get(
        f"/graph/entities/{entity['id']}/relations",
        params={"workspace_id": DEFAULT_WORKSPACE_ID},
    ).json()
    assert relations["relations"]

    relation_id = relations["relations"][0]["id"]
    evidence = client.get(
        f"/graph/relations/{relation_id}/evidence",
        params={"workspace_id": DEFAULT_WORKSPACE_ID},
    ).json()
    # 证据回到 SQLite 里的真实 Chunk，正文不是图里那份。
    assert evidence["evidence"]
    assert evidence["evidence"][0]["content"]
    assert evidence["evidence"][0]["source_name"] == "dutmed-百日咳.md"
    assert evidence["evidence"][0]["document_id"] == client.own.document.id
    assert evidence["evidence"][0]["document_version_id"] == client.own.version.id
    assert evidence["sources"]
    assert evidence["sources"][0]["extraction_run_id"] == client.own_run["id"]
    assert evidence["sources"][0]["candidate_relation_id"]
    assert evidence["sources"][0]["document_id"] == client.own.document.id
    assert evidence["sources"][0]["document_version_id"] == client.own.version.id


def test_graph_endpoints_require_a_workspace_id(client) -> None:
    _approve_all(client, client.own_run["id"])
    client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.own_run["id"]},
    )

    missing = client.get("/graph/entities", params={"query": "百日咳"})
    assert missing.status_code == 422

    # 别的 Workspace 拿同一个查询词，查不到默认 Workspace 的节点。
    other = client.get(
        "/graph/entities", params={"workspace_id": _OTHER, "query": "百日咳"}
    )
    assert other.status_code == 200
    assert other.json()["entities"] == []


def test_two_workspaces_publish_the_same_entity_without_merging(client) -> None:
    for run, workspace_id in (
        (client.own_run["id"], DEFAULT_WORKSPACE_ID),
        (client.foreign_run["id"], _OTHER),
    ):
        _approve_all(client, run, workspace_id)
        client.post(
            f"/workspaces/{workspace_id}/graph-publications",
            json={"extraction_run_id": run},
        )

    def drug_id(workspace_id: str) -> str:
        found = client.get(
            "/graph/entities", params={"workspace_id": workspace_id, "query": "琥乙红霉素片"}
        ).json()["entities"]
        return found[0]["id"]

    assert drug_id(DEFAULT_WORKSPACE_ID) != drug_id(_OTHER)
    assert client.graph.statistics(DEFAULT_WORKSPACE_ID).entities > 0
    assert client.graph.statistics(_OTHER).entities > 0


def test_document_list_reports_versions_chunks_and_candidate_counts(client) -> None:
    body = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/documents").json()

    assert body["workspace_id"] == DEFAULT_WORKSPACE_ID
    assert len(body["documents"]) == 1
    row = body["documents"][0]
    assert row["id"] == client.own.document.id
    assert row["source_name"] == "dutmed-百日咳.md"
    assert row["latest_version_id"] == client.own.version.id
    assert row["created_at"] and row["updated_at"]
    assert row["latest_version_created_at"]
    assert row["chunk_count"] == len(client.own.chunks)
    assert row["has_extraction_runs"] is True
    assert row["candidates"]["pending"] > 0
    assert row["candidates"]["total"] > 0
    assert row["candidates"]["published"] == 0
    # 别的 Workspace 的文档不出现在这里。
    assert row["id"] != client.foreign.document.id


def test_document_list_marks_publication_and_leaves_the_other_workspace_empty(
    client,
) -> None:
    _approve_all(client, client.own_run["id"])
    client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.own_run["id"]},
    )

    own = client.get(f"/workspaces/{DEFAULT_WORKSPACE_ID}/documents").json()["documents"][0]
    assert own["candidates"]["published"] > 0
    assert own["candidates"]["pending"] == 0

    other = client.get(f"/workspaces/{_OTHER}/documents").json()["documents"][0]
    assert other["candidates"]["published"] == 0
    assert other["candidates"]["pending"] > 0


def test_documents_without_extraction_report_no_runs(client) -> None:
    TextIngestionService(client.documents).ingest_text(
        "notes.md", "# 会议记录\n\n## 议题\n\n下周三讨论排期。\n", _OTHER
    )

    body = client.get(f"/workspaces/{_OTHER}/documents").json()
    without_run = next(
        row for row in body["documents"] if row["source_name"] == "notes.md"
    )

    assert without_run["has_extraction_runs"] is False
    assert without_run["candidates"]["total"] == 0
    assert without_run["chunk_count"] > 0


def test_deleting_a_document_with_published_graph_is_a_conflict(client) -> None:
    _approve_all(client, client.own_run["id"])
    client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.own_run["id"]},
    )

    response = client.delete(f"/documents/{client.own.document.id}")

    assert response.status_code == 409
    assert response.json()["error_code"] == "document_graph_published"


def test_deleting_a_document_without_publication_still_cleans_up(client) -> None:
    response = client.delete(f"/documents/{client.own.document.id}")

    assert response.status_code == 200
    assert response.json()["deleted_chunk_ids"]
    assert client.candidates.get_run(client.own_run["id"]) is None


def test_lifecycle_conflict_is_raised_by_the_service_too(client) -> None:
    """接口层与生命周期服务给出同一个判断，删文档不靠接口兜底。"""
    _approve_all(client, client.own_run["id"])
    client.post(
        f"/workspaces/{DEFAULT_WORKSPACE_ID}/graph-publications",
        json={"extraction_run_id": client.own_run["id"]},
    )
    service = DocumentLifecycleService(
        client.documents, client.graph, candidates=client.candidates
    )

    with pytest.raises(Exception) as error:
        service.delete(client.own.document.id)

    assert error.value.error_code == "document_graph_published"


def test_review_does_not_touch_the_graph(client) -> None:
    _approve_all(client, client.own_run["id"])

    # 全部批准之后图后端仍然是空的：发布是另一次显式调用。
    assert client.graph.statistics(DEFAULT_WORKSPACE_ID).entities == 0


def test_empty_workspace_still_answers_with_keyword_retrieval(client) -> None:
    response = client.post(
        "/query",
        json={"question": "琥乙红霉素片", "workspace_id": _OTHER},
    )

    assert response.status_code == 200
    assert response.json()["status"] != "error"
