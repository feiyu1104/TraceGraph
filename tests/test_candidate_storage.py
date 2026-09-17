from dataclasses import replace
import sqlite3

import pytest

from tracegraph.core.contracts import (
    CandidateEntity,
    CandidateKind,
    CandidateRelation,
    CandidateStatus,
    ExtractionRun,
    ExtractionStatus,
    Workspace,
)
from tracegraph.ingestion.service import TextIngestionService
from tracegraph.storage.candidates import (
    InMemoryCandidateRepository,
    SQLiteCandidateRepository,
)
from tracegraph.storage.sqlite import SQLiteDocumentRepository

NOW = "2026-01-01T00:00:00+00:00"
WORKSPACE_ID = "ws-default"
OTHER_WORKSPACE_ID = "ws-other"
CANDIDATE_TABLES = (
    "extraction_runs",
    "candidate_entities",
    "candidate_relations",
    "candidate_entity_evidence",
    "candidate_relation_evidence",
)


def _run(document_id: str, version_id: str, **overrides) -> ExtractionRun:
    fields = {
        "id": "run-1",
        "workspace_id": WORKSPACE_ID,
        "document_id": document_id,
        "document_version_id": version_id,
        "adapter_id": "medical",
        "model_id": "extractive",
        "status": ExtractionStatus.RUNNING,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ExtractionRun(**{**fields, **overrides})


def _entity(run: ExtractionRun, entity_id: str, name: str, evidence) -> CandidateEntity:
    return CandidateEntity(
        id=entity_id,
        extraction_run_id=run.id,
        workspace_id=run.workspace_id,
        document_id=run.document_id,
        document_version_id=run.document_version_id,
        adapter_id=run.adapter_id,
        name=name,
        normalized_name=name.casefold(),
        type="Disease",
        evidence_chunk_ids=tuple(evidence),
        created_at=NOW,
        updated_at=NOW,
    )


def _relation(
    run: ExtractionRun, relation_id: str, source: str, target: str, evidence
) -> CandidateRelation:
    return CandidateRelation(
        id=relation_id,
        extraction_run_id=run.id,
        workspace_id=run.workspace_id,
        document_id=run.document_id,
        document_version_id=run.document_version_id,
        adapter_id=run.adapter_id,
        source_entity_id=source,
        target_entity_id=target,
        type="HAS_SYMPTOM",
        evidence_chunk_ids=tuple(evidence),
        created_at=NOW,
        updated_at=NOW,
    )


def _ingest(database, source_name: str, content: str, workspace_id=WORKSPACE_ID):
    with SQLiteDocumentRepository(database) as documents:
        if documents.get_workspace(workspace_id) is None:
            documents.save_workspace(
                Workspace(
                    id=workspace_id,
                    name=workspace_id,
                    adapter_id="medical",
                    created_at=NOW,
                )
            )
        return TextIngestionService(documents).ingest_text(
            source_name, content, workspace_id
        )


@pytest.fixture
def ingested(tmp_path):
    """一个有真实 Chunk 的库：候选的证据必须指向真实存在的 Chunk。"""
    database = tmp_path / "tracegraph.db"
    return database, _ingest(database, "百日咳.md", "# 百日咳\n\n## 症状\n\n咳嗽、低热")


def test_migration_is_idempotent_and_keeps_existing_rows(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)

    with SQLiteCandidateRepository(database) as candidates:
        candidates.save_extraction(
            run, (_entity(run, "ent-1", "百日咳", (result.chunks[0].id,)),), ()
        )

    # 再开一次同一个库：迁移只做加法，已有的候选不受影响。
    with SQLiteCandidateRepository(database) as candidates:
        assert candidates.get_run(run.id) is not None
        assert len(candidates.list_entities(run.id)) == 1

    with sqlite3.connect(database) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert set(CANDIDATE_TABLES) <= names


def test_evidence_rejects_chunk_that_does_not_exist(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)

    with SQLiteCandidateRepository(database) as candidates:
        candidates.save_run(run)
        with pytest.raises(sqlite3.IntegrityError):
            candidates.save_extraction(
                _run(result.document.id, result.version.id, entity_count=9),
                (_entity(run, "ent-1", "百日咳", ("chk-does-not-exist",)),),
                (),
            )
        # 整批回滚：候选、证据与任务终态一起退回，没有半批数据。
        assert candidates.list_entities(run.id) == ()
        assert candidates.get_run(run.id) == run

        candidates.save_extraction(
            run, (_entity(run, "ent-1", "百日咳", (result.chunks[0].id,)),), ()
        )
        assert len(candidates.list_entities(run.id)) == 1


def test_evidence_must_reference_a_chunk_of_the_same_run(ingested) -> None:
    """证据必须指向本次抽取真的读过的 Chunk，这一点由服务层保证。

    数据库只兜住「Chunk 存在」这一层：真正把候选钉死在本次读到的片段上的，
    是抽取服务手里的 known_chunk_ids，见 test_extraction.py。
    """
    database, result = ingested
    other = _ingest(database, "别的.md", "# 别的\n\n正文")
    run = _run(result.document.id, result.version.id)

    with SQLiteCandidateRepository(database) as candidates:
        with pytest.raises(sqlite3.IntegrityError):
            candidates.save_extraction(
                run, (_entity(run, "ent-1", "别的", (other.chunks[0].id[:-1],)),), ()
            )
        assert candidates.list_entities(run.id) == ()


def test_batch_rejects_duplicate_entity_within_one_run(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)
    chunk_id = result.chunks[0].id

    with SQLiteCandidateRepository(database) as candidates:
        with pytest.raises(ValueError, match="重复实体"):
            candidates.save_extraction(
                run,
                (
                    _entity(run, "ent-1", "百日咳", (chunk_id,)),
                    _entity(run, "ent-2", "百日咳", (chunk_id,)),
                ),
                (),
            )


def test_batch_rejects_relation_pointing_outside_the_batch(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)
    chunk_id = result.chunks[0].id

    with SQLiteCandidateRepository(database) as candidates:
        with pytest.raises(ValueError, match="两端"):
            candidates.save_extraction(
                run,
                (_entity(run, "ent-1", "百日咳", (chunk_id,)),),
                (_relation(run, "rel-1", "ent-1", "ent-missing", (chunk_id,)),),
            )


def test_relations_and_their_evidence_are_persisted(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)
    chunk_id = result.chunks[0].id
    entities = (
        _entity(run, "ent-1", "百日咳", (chunk_id,)),
        _entity(run, "ent-2", "咳嗽", (chunk_id,)),
    )

    with SQLiteCandidateRepository(database) as candidates:
        candidates.save_extraction(
            run, entities, (_relation(run, "rel-1", "ent-1", "ent-2", (chunk_id,)),)
        )
        stored = candidates.list_entities(run.id)
        assert tuple(sorted(stored, key=lambda entity: entity.id)) == entities
        assert candidates.list_relations(run.id)[0].evidence_chunk_ids == (chunk_id,)
        assert {
            (item.candidate_id, item.candidate_kind)
            for item in candidates.list_evidence(run.id)
        } == {
            ("ent-1", CandidateKind.ENTITY),
            ("ent-2", CandidateKind.ENTITY),
            ("rel-1", CandidateKind.RELATION),
        }


def test_workspace_filter_never_returns_other_workspaces(ingested) -> None:
    database, result = ingested
    other = _ingest(database, "别的.md", "# 别的\n\n正文", OTHER_WORKSPACE_ID)
    run = _run(result.document.id, result.version.id)
    other_run = _run(
        other.document.id,
        other.version.id,
        id="run-2",
        workspace_id=OTHER_WORKSPACE_ID,
    )

    with SQLiteCandidateRepository(database) as candidates:
        candidates.save_extraction(
            run, (_entity(run, "ent-1", "百日咳", (result.chunks[0].id,)),), ()
        )
        candidates.save_extraction(
            other_run, (_entity(other_run, "ent-2", "别的", (other.chunks[0].id,)),), ()
        )

        assert [entity.name for entity in candidates.list_workspace_entities(WORKSPACE_ID)] == [
            "百日咳"
        ]
        assert [
            entity.name
            for entity in candidates.list_workspace_entities(OTHER_WORKSPACE_ID)
        ] == ["别的"]


def test_workspace_filters_by_document_status_and_type(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)
    chunk_id = result.chunks[0].id

    with SQLiteCandidateRepository(database) as candidates:
        candidates.save_extraction(
            run, (_entity(run, "ent-1", "百日咳", (chunk_id,)),), ()
        )
        assert candidates.list_workspace_entities(WORKSPACE_ID, document_id="doc-missing") == ()
        assert len(
            candidates.list_workspace_entities(
                WORKSPACE_ID, document_id=result.document.id
            )
        ) == 1
        assert len(
            candidates.list_workspace_entities(
                WORKSPACE_ID, status=CandidateStatus.PENDING
            )
        ) == 1
        assert (
            candidates.list_workspace_entities(
                WORKSPACE_ID, status=CandidateStatus.APPROVED
            )
            == ()
        )
        assert len(
            candidates.list_workspace_entities(WORKSPACE_ID, entity_type="Disease")
        ) == 1
        assert candidates.list_workspace_entities(WORKSPACE_ID, entity_type="Drug") == ()


def test_delete_document_removes_runs_and_candidates(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)
    chunk_id = result.chunks[0].id

    with SQLiteCandidateRepository(database) as candidates, SQLiteDocumentRepository(
        database
    ) as documents:
        candidates.save_extraction(
            run,
            (
                _entity(run, "ent-1", "百日咳", (chunk_id,)),
                _entity(run, "ent-2", "咳嗽", (chunk_id,)),
            ),
            (_relation(run, "rel-1", "ent-1", "ent-2", (chunk_id,)),),
        )
        # 先删候选再删文档：证据关联引用 chunks，反序会先撞上外键。
        candidates.delete_document(result.document.id)
        documents.delete_document(result.document.id)

        assert candidates.get_run(run.id) is None
        assert candidates.list_entities(run.id) == ()
        assert candidates.list_evidence(run.id) == ()


def test_run_status_and_failure_reason_round_trip(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)

    with SQLiteCandidateRepository(database) as candidates:
        candidates.save_run(run)
        candidates.save_run(
            replace(
                run,
                status=ExtractionStatus.FAILED,
                error="模型服务返回 HTTP 500。",
                updated_at="2026-01-01T00:01:00+00:00",
            )
        )
        stored = candidates.get_run(run.id)
        assert stored.status is ExtractionStatus.FAILED
        assert stored.error == "模型服务返回 HTTP 500。"
        assert stored.updated_at == "2026-01-01T00:01:00+00:00"


def test_in_memory_repository_matches_sqlite_semantics(ingested) -> None:
    database, result = ingested
    run = _run(result.document.id, result.version.id)
    chunk_id = result.chunks[0].id

    with SQLiteCandidateRepository(database) as sqlite_repository:
        for repository in (sqlite_repository, InMemoryCandidateRepository()):
            repository.save_extraction(
                run, (_entity(run, "ent-1", "百日咳", (chunk_id,)),), ()
            )
            with pytest.raises(ValueError, match="重复实体"):
                repository.save_extraction(
                    run,
                    (
                        _entity(run, "ent-3", "咳嗽", (chunk_id,)),
                        _entity(run, "ent-4", "咳嗽", (chunk_id,)),
                    ),
                    (),
                )
            assert len(repository.list_workspace_entities(WORKSPACE_ID)) == 1
            repository.delete_document(result.document.id)
            assert repository.list_entities(run.id) == ()
            assert repository.get_run(run.id) is None
