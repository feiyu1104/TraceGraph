"""上传原件的文件系统存储。

目录布局固定为：

    {root}/{workspace_id}/documents/{document_id}/{version_id}/original{suffix}

三段目录全部由服务端生成的 ID 拼成，用户文件名只贡献一个白名单内的后缀，
因此服务器目录结构不受上传者控制。所有对外方法只接收 ID 或本存储自己
返回过的相对路径，递归删除的落点永远由 ID 现推，不读外部传入的路径。
"""

from collections.abc import Mapping
import os
from pathlib import Path
import re
import shutil

ORIGINALS_DIR_ENV = "TRACEGRAPH_ORIGINALS_DIR"
DEFAULT_ORIGINALS_DIR = "data/workspaces"

_ORIGINAL_STEM = "original"
# 写入过程中的临时文件后缀，只在 save 内部出现，不会成为任何正式落点。
_TEMPORARY_SUFFIX = ".part"
# 路径片段白名单：ID 形如 ws-default / doc-<hex> / ver-<hex>，都不含分隔符，
# 也不可能等于 "." 或 ".."。
_SEGMENT_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SUFFIX_PATTERN = re.compile(r"\A\.[a-z0-9]{1,8}\Z")


class UnsafeStoragePathError(ValueError):
    """路径片段或最终落点越出了原件存储根目录。"""


def load_original_store_root(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = (env.get(ORIGINALS_DIR_ENV) or "").strip()
    return Path(configured) if configured else Path(DEFAULT_ORIGINALS_DIR)


class FileSystemOriginalStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def save(
        self,
        *,
        workspace_id: str,
        document_id: str,
        version_id: str,
        suffix: str,
        raw: bytes,
    ) -> str:
        directory = self._version_directory(workspace_id, document_id, version_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = self._contained(directory / f"{_ORIGINAL_STEM}{_checked_suffix(suffix)}")
        # 先写临时文件再原子替换：正式文件要么不存在，要么就是完整的一份，
        # 不会留下读到一半的内容。临时文件名是模块常量，不含用户输入，
        # 与目标同目录，因此替换不会跨卷。
        temporary = target.with_name(f"{_ORIGINAL_STEM}{_TEMPORARY_SUFFIX}")
        try:
            with open(temporary, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return target.relative_to(self._root).as_posix()

    def read(self, stored_path: str) -> bytes | None:
        target = self._resolve_stored(stored_path)
        return target.read_bytes() if target.is_file() else None

    def exists(self, stored_path: str) -> bool:
        return self._resolve_stored(stored_path).is_file()

    def remove_version(
        self, *, workspace_id: str, document_id: str, version_id: str
    ) -> None:
        directory = self._version_directory(workspace_id, document_id, version_id)
        # 清理路径要尽力而为：它多半是在数据库失败后调用的，这里再抛异常
        # 只会把真正的失败原因盖掉。
        shutil.rmtree(directory, ignore_errors=True)
        self._prune_empty(directory.parent)

    def remove_document(self, *, workspace_id: str, document_id: str) -> None:
        directory = self._document_directory(workspace_id, document_id)
        if directory.is_dir():
            shutil.rmtree(directory)

    def _version_directory(
        self, workspace_id: str, document_id: str, version_id: str
    ) -> Path:
        return self._contained(
            self._document_directory(workspace_id, document_id)
            / _checked_segment(version_id, "version_id")
        )

    def _document_directory(self, workspace_id: str, document_id: str) -> Path:
        # 先逐段校验再拼接：任何一段不合法都在碰到文件系统之前就失败，
        # 因此不存在"先 mkdir 到目录外面、再发现不对"的窗口。
        return self._contained(
            self._root
            / _checked_segment(workspace_id, "workspace_id")
            / "documents"
            / _checked_segment(document_id, "document_id")
        )

    def _resolve_stored(self, stored_path: str) -> Path:
        if not stored_path.strip():
            raise UnsafeStoragePathError("原件路径不能为空")
        # 数据库里存的是相对路径；即便它被改坏，_contained 也会拦住越界。
        return self._contained(self._root / Path(stored_path.replace("\\", "/")))

    def _contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self._root):
            raise UnsafeStoragePathError(f"路径越出原件存储根目录：{path}")
        return resolved

    def _prune_empty(self, directory: Path) -> None:
        try:
            directory.rmdir()
        except OSError:
            # 还有别的版本在用这个文档目录，留着是对的。
            pass


def _checked_segment(value: str, field: str) -> str:
    if not _SEGMENT_PATTERN.match(value):
        raise UnsafeStoragePathError(f"{field} 不是合法的路径片段：{value!r}")
    return value


def _checked_suffix(suffix: str) -> str:
    if not _SUFFIX_PATTERN.match(suffix):
        raise UnsafeStoragePathError(f"扩展名不合法：{suffix!r}")
    return suffix
