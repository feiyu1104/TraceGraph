"""上传原件永久保存的开发期回归检查。"""

import base64
import hashlib
import os
from pathlib import Path
import sqlite3
import threading

import anyio
import httpx2
import pytest

from tracegraph.api import create_app
from tracegraph.core.contracts import (
    DEFAULT_WORKSPACE_ADAPTER_ID,
    IngestionStatus,
    Workspace,
)
from tracegraph.ingestion.lifecycle import DocumentLifecycleService
from tracegraph.ingestion.service import OriginalConflictError, TextIngestionService
from tracegraph.retrieval.keyword import KeywordRetriever
from tracegraph.storage.memory import InMemoryDocumentRepository
from tracegraph.storage.originals import (
    FileSystemOriginalStore,
    UnsafeStoragePathError,
)
from tracegraph.storage.sqlite import SQLiteDocumentRepository

_GUIDELINE = "# 高血压\n\n患者应定期监测血压。"
_OTHER = "# 高血压\n\n应避免自行调整降压药物。"
# 与 _GUIDELINE 解码后完全相同，原始字节却不同：BOM 只影响字节，不影响正文。
_GUIDELINE_WITH_BOM = b"\xef\xbb\xbf" + _GUIDELINE.encode("utf-8")


@pytest.fixture(params=["sqlite", "memory"])
def repository(request, tmp_path):
    """同一套用例分别跑在两种仓储实现上，保证行为一致。"""
    if request.param == "sqlite":
        with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as sqlite_repository:
            yield sqlite_repository
    else:
        yield InMemoryDocumentRepository()


def _store(tmp_path) -> FileSystemOriginalStore:
    return FileSystemOriginalStore(tmp_path / "originals")


def _workspace(workspace_id: str) -> Workspace:
    return Workspace(
        id=workspace_id,
        name=workspace_id,
        adapter_id=DEFAULT_WORKSPACE_ADAPTER_ID,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _files_under(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    )


class _PausingOriginalStore(FileSystemOriginalStore):
    """第一份原件写完后暂停，让第二个上传请求确定地撞上事务窗口。"""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls = 0
        self.first_saved = threading.Event()
        self.second_save_started = threading.Event()
        self.release_first = threading.Event()

    def save(self, **kwargs: object) -> str:
        self.calls += 1
        call_number = self.calls
        if call_number > 1:
            self.second_save_started.set()
        stored_path = super().save(**kwargs)
        if call_number == 1:
            self.first_saved.set()
            assert self.release_first.wait(timeout=10)
        return stored_path


class _IngestionWorker:
    def __init__(self, action) -> None:
        self.action = action
        self.started = threading.Event()
        self.result = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run)

    def _run(self) -> None:
        self.started.set()
        try:
            self.result = self.action()
        except BaseException as error:  # 测试必须留住线程里的任何失败
            self.error = error

    def start(self) -> "_IngestionWorker":
        self.thread.start()
        return self

    def join(self) -> "_IngestionWorker":
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "并发入库没有结束（疑似死锁）"
        return self


async def _post(application, path: str, payload: dict[str, object]) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=payload)


async def _upload(application, filename: str, raw: bytes, workspace_id: str | None = None):
    payload: dict[str, object] = {
        "filename": filename,
        "content_base64": base64.b64encode(raw).decode("ascii"),
    }
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    return await _post(application, "/ingestions/file", payload)


async def _get(application, path: str) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=application)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def test_uploaded_original_is_stored_on_disk_and_tracked(tmp_path) -> None:
    store = _store(tmp_path)
    raw = _GUIDELINE.encode("utf-8")
    database = tmp_path / "tracegraph.db"

    with SQLiteDocumentRepository(database) as repository:
        result = TextIngestionService(repository, original_store=store).ingest_bytes(
            "指南.md", raw
        )
        version = result.version

        assert version.original_size == len(raw)
        assert version.original_sha256 == hashlib.sha256(raw).hexdigest()
        assert version.original_filename == "指南.md"
        # 存的是相对路径，数据目录搬家后仍然可解析。
        assert not Path(version.stored_path).is_absolute()
        assert Path(version.stored_path).parts == (
            "ws-default",
            "documents",
            result.document.id,
            version.id,
            "original.md",
        )
        assert store.read(version.stored_path) == raw

    # 重新打开数据库，原件的位置、哈希、大小和文件名都还在。
    with SQLiteDocumentRepository(database) as repository:
        assert repository.get_version(version.id) == version
        assert repository.list_versions(result.document.id) == (version,)


def test_original_backed_chunks_participate_in_keyword_retrieval(tmp_path) -> None:
    store = _store(tmp_path)
    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        result = TextIngestionService(repository, original_store=store).ingest_bytes(
            "指南.md", _GUIDELINE.encode("utf-8")
        )

        evidences = KeywordRetriever(repository).retrieve("高血压")
        assert evidences
        assert {evidence.chunk_id for evidence in evidences} <= {
            chunk.id for chunk in result.chunks
        }


def test_reuploading_same_original_does_not_create_another_version(tmp_path) -> None:
    store = _store(tmp_path)
    raw = _GUIDELINE.encode("utf-8")

    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        service = TextIngestionService(repository, original_store=store)
        first = service.ingest_bytes("指南.md", raw)
        second = service.ingest_bytes("指南.md", raw)

        assert second.job.status is IngestionStatus.SKIPPED
        assert second.version == first.version
        assert second.chunks == first.chunks
        # 原件哈希一致，已记录的原件不会被重写。
        assert second.version.original_sha256 == first.version.original_sha256
        assert second.version.stored_path == first.version.stored_path
        assert repository.list_versions(first.document.id) == (first.version,)
        # 磁盘上也只留一份原件，没有多出一层版本目录。
        assert _files_under(store.root / first.version.stored_path.rsplit("/", 1)[0]) == [
            "original.md"
        ]


def test_version_without_original_keeps_original_fields_empty(tmp_path) -> None:
    database = tmp_path / "tracegraph.db"
    with SQLiteDocumentRepository(database) as repository:
        # 未装配原件存储：只入库解析结果，四个原件字段整体为空。
        result = TextIngestionService(repository).ingest_bytes(
            "指南.md", _GUIDELINE.encode("utf-8")
        )
        assert result.version.original_sha256 is None
        assert result.version.original_size is None
        assert result.version.stored_path is None
        assert result.version.original_filename is None

    with SQLiteDocumentRepository(database) as repository:
        assert repository.get_version(result.version.id) == result.version


def test_failed_database_write_leaves_no_orphan_original(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        service = TextIngestionService(repository, original_store=store)

        def explode(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("数据库写入失败")

        monkeypatch.setattr(repository, "save_ingestion", explode)
        with pytest.raises(sqlite3.OperationalError):
            service.ingest_bytes("指南.md", _GUIDELINE.encode("utf-8"))

    assert _files_under(store.root) == []


def test_storage_rejects_paths_that_escape_its_root(tmp_path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("不该被动", encoding="utf-8")

    for segment in ("../outside", "..", "a/b", "", "C:\\Windows", ".hidden"):
        with pytest.raises(UnsafeStoragePathError):
            store.save(
                workspace_id=segment,
                document_id="doc-1",
                version_id="ver-1",
                suffix=".md",
                raw=b"x",
            )

    with pytest.raises(UnsafeStoragePathError):
        store.save(
            workspace_id="ws-default",
            document_id="..",
            version_id="ver-1",
            suffix=".md",
            raw=b"x",
        )
    # 扩展名同样只接受白名单形态，不接受路径成分。
    with pytest.raises(UnsafeStoragePathError):
        store.save(
            workspace_id="ws-default",
            document_id="doc-1",
            version_id="ver-1",
            suffix="/../x",
            raw=b"x",
        )

    for stored_path in ("../outside/keep.txt", "", "ws-default/../../outside/keep.txt"):
        with pytest.raises(UnsafeStoragePathError):
            store.read(stored_path)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "不该被动"


def test_unknown_workspace_upload_writes_nothing(tmp_path) -> None:
    store = _store(tmp_path)
    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        service = TextIngestionService(repository, original_store=store)

        with pytest.raises(ValueError, match="ws-missing"):
            service.ingest_bytes("指南.md", _GUIDELINE.encode("utf-8"), "ws-missing")

        assert repository.list_documents() == ()

    assert _files_under(store.root) == []


def test_deleting_document_removes_only_its_own_original(tmp_path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("不该被动", encoding="utf-8")

    with SQLiteDocumentRepository(tmp_path / "tracegraph.db") as repository:
        repository.save_workspace(_workspace("ws-cardio"))
        service = TextIngestionService(repository, original_store=store)
        doomed = service.ingest_bytes("指南.md", _GUIDELINE.encode("utf-8"))
        survivor = service.ingest_bytes(
            "指南.md", _OTHER.encode("utf-8"), "ws-cardio"
        )

        DocumentLifecycleService(repository, originals=store).delete(
            doomed.document.id
        )

        # 被删文档的原件连同它的目录一起消失。
        assert store.read(doomed.version.stored_path) is None
        assert not (store.root / "ws-default" / "documents" / doomed.document.id).exists()
        # 另一个 Workspace 的同名文档原件原样保留。
        assert store.read(survivor.version.stored_path) == _OTHER.encode("utf-8")
        assert repository.get_version(survivor.version.id) == survivor.version

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "不该被动"


def test_upload_endpoint_stores_original_and_exposes_its_metadata(tmp_path) -> None:
    store = _store(tmp_path)
    application = create_app(InMemoryDocumentRepository(), original_store=store)
    raw = _GUIDELINE.encode("utf-8")

    response = anyio.run(_upload, application, "指南.md", raw)
    assert response.status_code == 200
    body = response.json()
    version = body["version"]
    assert version["original_filename"] == "指南.md"
    assert version["original_sha256"] == hashlib.sha256(raw).hexdigest()
    assert version["original_size"] == len(raw)
    assert store.read(version["stored_path"]) == raw

    described = anyio.run(
        _get,
        application,
        f"/documents/{body['document']['id']}/versions/{version['id']}/original",
    )
    assert described.status_code == 200
    assert described.json() == {
        "document_id": body["document"]["id"],
        "version_id": version["id"],
        "original_filename": "指南.md",
        "original_sha256": hashlib.sha256(raw).hexdigest(),
        "original_size": len(raw),
        "stored_path": version["stored_path"],
        "available": True,
    }


def test_original_endpoint_reports_missing_original(tmp_path) -> None:
    store = _store(tmp_path)
    application = create_app(InMemoryDocumentRepository(), original_store=store)

    # 文本入库没有原件，因此没有原件元信息可报。
    ingested = anyio.run(_upload, application, "指南.md", _GUIDELINE.encode("utf-8"))
    assert ingested.status_code == 200

    response = anyio.run(
        _get, application, "/documents/doc-missing/versions/ver-missing/original"
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found"


def test_upload_backfills_original_for_a_text_only_version(tmp_path) -> None:
    store = _store(tmp_path)
    application = create_app(InMemoryDocumentRepository(), original_store=store)
    raw = _GUIDELINE.encode("utf-8")

    # 先用文本入库造一条只有解析结果的版本。
    text_only = anyio.run(
        _post,
        application,
        "/ingestions",
        {"source_name": "指南.md", "content": _GUIDELINE},
    )
    assert text_only.status_code == 200
    assert text_only.json()["version"]["stored_path"] is None
    version_id = text_only.json()["version"]["id"]

    uploaded = anyio.run(_upload, application, "指南.md", raw)

    assert uploaded.status_code == 200
    assert uploaded.json()["job"]["status"] == "skipped"
    version = uploaded.json()["version"]
    assert version["id"] == version_id
    assert version["original_sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.read(version["stored_path"]) == raw


def test_conflicting_original_is_reported_by_the_endpoint(tmp_path) -> None:
    store = _store(tmp_path)
    application = create_app(InMemoryDocumentRepository(), original_store=store)

    first = anyio.run(_upload, application, "指南.md", _GUIDELINE.encode("utf-8"))
    assert first.status_code == 200

    conflict = anyio.run(_upload, application, "指南.md", _GUIDELINE_WITH_BOM)

    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "original_conflict"
    # 冲突不动已经存好的原件。
    assert store.read(first.json()["version"]["stored_path"]) == _GUIDELINE.encode(
        "utf-8"
    )


def test_upload_to_named_workspace_lands_under_that_workspace(tmp_path) -> None:
    store = _store(tmp_path)
    repository = InMemoryDocumentRepository()
    repository.save_workspace(_workspace("ws-cardio"))
    application = create_app(repository, original_store=store)
    raw = _GUIDELINE.encode("utf-8")

    response = anyio.run(_upload, application, "指南.md", raw, "ws-cardio")
    assert response.status_code == 200
    stored_path = response.json()["version"]["stored_path"]
    assert stored_path.startswith("ws-cardio/documents/")
    assert store.read(stored_path) == raw


def test_later_upload_backfills_original_for_a_text_only_version(tmp_path, repository) -> None:
    store = _store(tmp_path)
    raw = _GUIDELINE.encode("utf-8")

    # 先经文本入库：这条历史版本只有解析结果，没有原件。
    text_only = TextIngestionService(repository).ingest_bytes("指南.md", raw)
    assert text_only.version.stored_path is None

    service = TextIngestionService(repository, original_store=store)
    result = service.ingest_bytes("指南.md", raw)

    assert result.job.status is IngestionStatus.SKIPPED
    assert result.version.id == text_only.version.id
    assert result.version.original_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.version.original_size == len(raw)
    assert result.version.original_filename == "指南.md"
    assert store.read(result.version.stored_path) == raw
    # 补存不新增版本，也不新增 Chunk。
    assert repository.list_versions(result.document.id) == (result.version,)
    assert tuple(chunk.id for chunk in result.chunks) == tuple(
        chunk.id for chunk in text_only.chunks
    )
    assert repository.get_version(result.version.id) == result.version


def test_attach_original_only_applies_to_versions_without_one(
    tmp_path, repository
) -> None:
    store = _store(tmp_path)
    result = TextIngestionService(repository, original_store=store).ingest_bytes(
        "指南.md", _GUIDELINE.encode("utf-8")
    )

    with pytest.raises(ValueError, match="还没有原件"):
        repository.attach_original(
            result.version.id,
            original_sha256="0" * 64,
            original_size=1,
            stored_path="ws-default/documents/x/y/original.md",
            original_filename="其他.md",
        )
    with pytest.raises(ValueError, match="还没有原件"):
        repository.attach_original(
            "ver-missing",
            original_sha256="0" * 64,
            original_size=1,
            stored_path="ws-default/documents/x/y/original.md",
            original_filename="其他.md",
        )

    # 已有原件原样保留，没有被条件更新碰到。
    assert repository.get_version(result.version.id) == result.version


def test_same_text_with_different_bytes_never_replaces_the_stored_original(
    tmp_path, repository
) -> None:
    store = _store(tmp_path)
    service = TextIngestionService(repository, original_store=store)
    first = service.ingest_bytes("指南.md", _GUIDELINE.encode("utf-8"))

    # 解析文本相同（正文哈希一致），原始字节不同：不能静默覆盖。
    with pytest.raises(OriginalConflictError, match="原件"):
        service.ingest_bytes("指南.md", _GUIDELINE_WITH_BOM)

    assert repository.get_version(first.version.id) == first.version
    assert repository.list_versions(first.document.id) == (first.version,)
    assert store.read(first.version.stored_path) == _GUIDELINE.encode("utf-8")
    assert _files_under(store.root) == [first.version.stored_path]


def test_concurrent_first_uploads_keep_the_committed_original(tmp_path) -> None:
    """同一新版本的两个上传不能让失败清理删掉成功请求的原件。"""
    repository = InMemoryDocumentRepository()
    store = _PausingOriginalStore(tmp_path / "originals")
    service = TextIngestionService(repository, original_store=store)
    raw = _GUIDELINE.encode("utf-8")

    first = _IngestionWorker(lambda: service.ingest_bytes("指南.md", raw)).start()
    assert store.first_saved.wait(timeout=10)
    second = _IngestionWorker(lambda: service.ingest_bytes("指南.md", raw)).start()
    assert second.started.wait(timeout=10)

    # 第一条事务尚未完成时，第二条不能进入原件写入阶段。
    assert not store.second_save_started.wait(timeout=0.1)
    store.release_first.set()
    first.join()
    second.join()

    assert first.error is None and second.error is None
    assert first.result is not None and second.result is not None
    assert {first.result.job.status, second.result.job.status} == {
        IngestionStatus.SUCCEEDED,
        IngestionStatus.SKIPPED,
    }
    assert store.calls == 1
    committed = repository.list_versions(first.result.document.id)
    assert len(committed) == 1
    assert committed[0].stored_path is not None
    assert store.read(committed[0].stored_path) == raw


def test_concurrent_backfills_keep_the_attached_original(tmp_path) -> None:
    """历史版本补存原件时，两个请求也只能有一个真正写文件和认领记录。"""
    repository = InMemoryDocumentRepository()
    raw = _GUIDELINE.encode("utf-8")
    text_only = TextIngestionService(repository).ingest_bytes("指南.md", raw)
    assert text_only.version.stored_path is None

    store = _PausingOriginalStore(tmp_path / "originals")
    service = TextIngestionService(repository, original_store=store)
    first = _IngestionWorker(lambda: service.ingest_bytes("指南.md", raw)).start()
    assert store.first_saved.wait(timeout=10)
    second = _IngestionWorker(lambda: service.ingest_bytes("指南.md", raw)).start()
    assert second.started.wait(timeout=10)

    assert not store.second_save_started.wait(timeout=0.1)
    store.release_first.set()
    first.join()
    second.join()

    assert first.error is None and second.error is None
    assert first.result is not None and second.result is not None
    assert first.result.job.status is IngestionStatus.SKIPPED
    assert second.result.job.status is IngestionStatus.SKIPPED
    assert store.calls == 1
    committed = repository.get_version(text_only.version.id)
    assert committed is not None and committed.stored_path is not None
    assert store.read(committed.stored_path) == raw


def test_failed_backfill_leaves_no_original_behind(
    tmp_path, repository, monkeypatch
) -> None:
    store = _store(tmp_path)
    raw = _GUIDELINE.encode("utf-8")
    text_only = TextIngestionService(repository).ingest_bytes("指南.md", raw)

    def explode(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("数据库写入失败")

    monkeypatch.setattr(repository, "attach_original", explode)
    with pytest.raises(sqlite3.OperationalError):
        TextIngestionService(repository, original_store=store).ingest_bytes(
            "指南.md", raw
        )

    assert _files_under(store.root) == []
    assert repository.get_version(text_only.version.id) == text_only.version


def test_ingest_file_saves_the_original(tmp_path, repository) -> None:
    store = _store(tmp_path)
    raw = _GUIDELINE.encode("utf-8")
    path = tmp_path / "指南.md"
    path.write_bytes(raw)

    result = TextIngestionService(repository, original_store=store).ingest_file(path)

    assert result.version.original_filename == "指南.md"
    assert result.version.original_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.version.original_size == len(raw)
    assert store.read(result.version.stored_path) == raw


def test_ingest_file_honours_workspace_and_missing_store(tmp_path, repository) -> None:
    store = _store(tmp_path)
    repository.save_workspace(_workspace("ws-cardio"))
    path = tmp_path / "指南.md"
    path.write_bytes(_GUIDELINE.encode("utf-8"))

    scoped = TextIngestionService(repository, original_store=store).ingest_file(
        path, "ws-cardio"
    )
    assert scoped.document.workspace_id == "ws-cardio"
    assert scoped.version.stored_path.startswith("ws-cardio/documents/")

    # 未装配原件存储时行为与之前一致：只入库解析结果。
    plain = TextIngestionService(repository).ingest_file(path)
    assert plain.version.stored_path is None


def test_failed_file_write_leaves_no_temporary_file(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("原子替换失败")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        store.save(
            workspace_id="ws-default",
            document_id="doc-1",
            version_id="ver-1",
            suffix=".md",
            raw=b"x",
        )

    # 临时文件不能留在磁盘上。
    assert _files_under(store.root) == []


def test_upload_with_unsupported_suffix_is_rejected_without_touching_disk(tmp_path) -> None:
    store = _store(tmp_path)
    application = create_app(InMemoryDocumentRepository(), original_store=store)

    response = anyio.run(_upload, application, "payload.exe", b"MZ\x90\x00")

    assert response.status_code == 400
    assert response.json()["error_code"] == "unsupported_document"
    assert _files_under(store.root) == []


def test_upload_over_the_size_limit_is_rejected_without_touching_disk(
    tmp_path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("TRACEGRAPH_MAX_UPLOAD_MB", "0.001")
    application = create_app(InMemoryDocumentRepository(), original_store=store)

    response = anyio.run(_upload, application, "指南.md", b"a" * 4096)

    assert response.status_code == 413
    assert response.json()["error_code"] == "file_too_large"
    assert _files_under(store.root) == []
