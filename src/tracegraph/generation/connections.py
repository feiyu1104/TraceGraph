"""本地模型连接：服务端自己保存的模型地址、密钥与可选模型。

与 `models.py` 的分工：那一份配置只保存 API Key 对应的**环境变量名**，密钥
留在进程环境里；这一份保存的是用户在管理接口里填的连接本身，因此它必须待在
已经被 Git 忽略的运行数据目录，并且只能由环回客户端通过管理接口读写。

**这个文件不是加密保险箱。** 它只保证三件事：不进版本库、不被任何查询接口
回显、不进日志与错误信息。能读到这台机器上这个文件的人，就能读到里面的密钥。

运行时连接是叠加在基础配置之上的**追加**：基础条目的 ID 一个都不许被覆盖，
所以网页上的一次保存改不动 `config/models.local.json` 或环境变量里的模型。
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
from urllib.parse import urlsplit

from tracegraph.generation.config import DEFAULT_TIMEOUT, GENERATOR_EXTRACTIVE
from tracegraph.generation.discovery import discover_models
from tracegraph.generation.models import (
    KIND_OPENAI,
    ModelEntry,
    ModelRegistry,
    RefreshingModelRegistry,
    UnknownGeneratorError,
    UnavailableGeneratorError,
)
from tracegraph.generation.providers import OpenAICompatibleAnswerGenerator


CONNECTIONS_PATH_ENV = "TRACEGRAPH_MODEL_CONNECTIONS"
DEFAULT_CONNECTIONS_PATH = Path("data/local/model-connections.json")

# 文件格式版本；将来结构变化时靠它给出「这个文件我不认识」而不是猜着读。
_FILE_VERSION = 1

# `extractive` 是内置模型，连接与连接里的模型都不能占用这个 ID。
RESERVED_MODEL_IDS = frozenset({GENERATOR_EXTRACTIVE})

MAX_ID_LENGTH = 64
MAX_LABEL_LENGTH = 80
MAX_MODEL_NAME_LENGTH = 200
MAX_API_KEY_LENGTH = 4096
MAX_BASE_URL_LENGTH = 2048
MAX_CONNECTIONS = 20
MAX_MODELS_PER_CONNECTION = 50

MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 60.0

# 本地 ID 的字符集：它会进 URL 路径、进 JSON 键、进界面，因此收得比远端
# 模型 ID 紧。远端模型 ID 只进请求体，用另一套更宽松的规则。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

# 发现器的形状：`discover_models` 是默认实现，测试传假的替身。
Discoverer = Callable[[str, str, float], tuple[str, ...]]


class ModelConnectionError(Exception):
    """模型连接配置错误；子类各自带上接口层要用的状态码与错误码。"""

    error_code = "model_connection_error"
    status_code = 400


class InvalidConnectionError(ModelConnectionError):
    """输入字段不合法。错误信息只描述规则，不回显取值。"""

    error_code = "invalid_connection"
    status_code = 400


class InvalidBaseUrlError(InvalidConnectionError):
    """地址不合法；单独一个错误码，客户端据此把提示落到地址输入框上。"""

    error_code = "invalid_base_url"
    status_code = 400


class ConnectionNotFoundError(ModelConnectionError):
    error_code = "connection_not_found"
    status_code = 404


class ConnectionConflictError(ModelConnectionError):
    """ID 冲突，或请求的配置与当前配置状态对不上。"""

    error_code = "connection_conflict"
    status_code = 409


class ConnectionFileError(ModelConnectionError):
    """连接文件不可用：结构不对、字段不合法，或与基础模型冲突。"""

    error_code = "connection_file_invalid"
    status_code = 409


@dataclass(frozen=True, slots=True)
class ConnectionModel:
    """连接里的一个可选模型：本地 ID、显示名、远端真实模型 ID。"""

    id: str
    label: str
    model: str

    def describe(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "model": self.model}


@dataclass(frozen=True, slots=True, repr=False)
class ModelConnection:
    """一条模型连接。

    `api_key` 只在这里保存与使用。`repr` 被显式改写：默认的 dataclass repr
    会把密钥原样打出来，而日志、异常和调试输出到处都是 repr。
    """

    id: str
    label: str
    base_url: str
    api_key: str
    timeout: float
    models: tuple[ConnectionModel, ...]

    def __repr__(self) -> str:
        return (
            f"ModelConnection(id={self.id!r}, label={self.label!r}, "
            f"base_url={self.base_url!r}, timeout={self.timeout!r}, "
            f"models={len(self.models)}, api_key=<已隐藏>)"
        )

    def describe(self) -> dict[str, object]:
        """管理接口可见的安全元数据：只报告「有没有密钥」。"""
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "has_api_key": bool(self.api_key),
            "models": [model.describe() for model in self.models],
        }


@dataclass(frozen=True, slots=True)
class ConnectionFile:
    """连接文件的全部内容。

    `default_id` 为 None 表示这里从没选过默认模型：此时沿用基础配置的默认
    模型，因此第一次运行、还没有这个文件时行为与改造前完全一致。
    """

    default_id: str | None
    connections: tuple[ModelConnection, ...] = ()

    @staticmethod
    def empty() -> "ConnectionFile":
        return ConnectionFile(None, ())


def load_connections_path(
    environ: Mapping[str, str] | None = None, path: str | Path | None = None
) -> Path:
    """连接文件的位置；`TRACEGRAPH_MODEL_CONNECTIONS` 可以把它挪到别处。"""
    if path is not None:
        return Path(path)
    env = os.environ if environ is None else environ
    override = (env.get(CONNECTIONS_PATH_ENV) or "").strip()
    return Path(override) if override else DEFAULT_CONNECTIONS_PATH


def normalize_id(value: object, *, field: str, allow_reserved: bool = False) -> str:
    """连接 ID 与模型 ID 的公共校验。

    `allow_reserved` 只给「选择默认模型」用：把默认模型切回内置的
    `extractive` 是正常操作，而连接与连接里的模型都不许占用这个 ID。
    """
    text = _text(value)
    if not text:
        raise InvalidConnectionError(f"{field} 不能为空。")
    if len(text) > MAX_ID_LENGTH:
        raise InvalidConnectionError(f"{field} 不能超过 {MAX_ID_LENGTH} 个字符。")
    if not _ID_PATTERN.match(text):
        raise InvalidConnectionError(
            f"{field} 只能包含字母、数字与 . _ : -，且必须以字母或数字开头。"
        )
    if not allow_reserved and text in RESERVED_MODEL_IDS:
        raise InvalidConnectionError(
            f"{field} 不能使用内置保留 ID：{GENERATOR_EXTRACTIVE}。"
        )
    return text


def normalize_label(value: object, *, field: str) -> str:
    text = _text(value)
    if len(text) > MAX_LABEL_LENGTH:
        raise InvalidConnectionError(f"{field} 不能超过 {MAX_LABEL_LENGTH} 个字符。")
    if any(not character.isprintable() for character in text):
        raise InvalidConnectionError(f"{field} 不能包含控制字符。")
    return text


def normalize_model_name(value: object) -> str:
    """远端真实模型 ID；它只进请求体，因此字符集比本地 ID 宽松。"""
    text = _text(value)
    if not text:
        raise InvalidConnectionError("模型条目缺少 model（远端模型 ID）。")
    if len(text) > MAX_MODEL_NAME_LENGTH:
        raise InvalidConnectionError(
            f"远端模型 ID 不能超过 {MAX_MODEL_NAME_LENGTH} 个字符。"
        )
    if any(character.isspace() or not character.isprintable() for character in text):
        raise InvalidConnectionError("远端模型 ID 不能包含空白或控制字符。")
    return text


def normalize_base_url(value: object) -> str:
    """校验并规范化地址，保存的是去掉末尾 `/` 之后的形式。

    拒绝用户名、密码、query 与 fragment：自建网关常把凭证写在地址上，那种
    地址会连同密钥一起进配置文件、日志与错误信息。**内网地址是允许的** ——
    本地 Ollama、LM Studio 正是靠它接入，这是本机工具的产品需求；安全边界
    因此不落在地址上，而落在「模型管理接口只允许环回客户端调用」。
    """
    text = _text(value)
    if not text:
        raise InvalidBaseUrlError("base_url 不能为空。")
    if len(text) > MAX_BASE_URL_LENGTH:
        raise InvalidBaseUrlError("base_url 过长。")
    if any(character.isspace() or not character.isprintable() for character in text):
        raise InvalidBaseUrlError("base_url 不能包含空白或控制字符。")
    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        raise InvalidBaseUrlError("base_url 必须以 http:// 或 https:// 开头。")
    if not parsed.netloc:
        raise InvalidBaseUrlError("base_url 缺少主机名。")
    if parsed.username or parsed.password:
        raise InvalidBaseUrlError("base_url 不能包含用户名或密码，请改用 api_key 字段。")
    if parsed.query or parsed.fragment:
        raise InvalidBaseUrlError("base_url 不能带查询参数或片段。")
    return text.rstrip("/")


def normalize_timeout(value: object) -> float:
    """缺省用配置里的默认超时；超出区间一律拒绝，不悄悄夹紧。"""
    if value is None or value == "":
        return DEFAULT_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidConnectionError("timeout 必须是秒数。")
    seconds = float(value)
    if not MIN_TIMEOUT <= seconds <= MAX_TIMEOUT:
        raise InvalidConnectionError(
            f"timeout 必须在 {MIN_TIMEOUT:g} 到 {MAX_TIMEOUT:g} 秒之间。"
        )
    return seconds


def normalize_api_key(value: object) -> str:
    """密钥校验：只挡不住的字符，不回显取值。

    换行会把 Authorization 请求头拆开，这是请求头注入而不只是「密钥写错了」，
    因此空白与控制字符一律拒绝。
    """
    text = _text(value)
    if not text:
        raise InvalidConnectionError("api_key 不能为空。")
    if len(text) > MAX_API_KEY_LENGTH:
        raise InvalidConnectionError(f"api_key 不能超过 {MAX_API_KEY_LENGTH} 个字符。")
    if any(character.isspace() or not character.isprintable() for character in text):
        raise InvalidConnectionError("api_key 不能包含空白或控制字符。")
    return text


def parse_connection(raw: object, *, where: str) -> ModelConnection:
    """把一份原始结构变成连接对象，顺带完成全部字段校验。

    文件读取与管理接口写入共用这一条校验路径：两边接受的字段与边界完全
    相同，不会出现「接口能存的、启动时读不出来」这种自相矛盾的状态。
    """
    if not isinstance(raw, dict):
        raise InvalidConnectionError(f"{where}必须是对象。")
    connection_id = normalize_id(raw.get("id"), field=f"{where}的 id")
    label = normalize_label(raw.get("label"), field=f"{where}的 label")
    base_url = normalize_base_url(raw.get("base_url"))
    api_key = normalize_api_key(raw.get("api_key"))
    timeout = normalize_timeout(raw.get("timeout"))

    raw_models = raw.get("models", [])
    if not isinstance(raw_models, list):
        raise InvalidConnectionError(f"{where}的 models 必须是数组。")
    if len(raw_models) > MAX_MODELS_PER_CONNECTION:
        raise InvalidConnectionError(
            f"{where}的 models 最多 {MAX_MODELS_PER_CONNECTION} 个。"
        )
    models: list[ConnectionModel] = []
    seen: set[str] = set()
    for position, item in enumerate(raw_models, start=1):
        model = _parse_model(item, f"{where}的 models 第 {position} 项")
        if model.id in seen:
            raise InvalidConnectionError(f"{where}的模型 ID 重复：{model.id}。")
        seen.add(model.id)
        models.append(model)

    return ModelConnection(
        id=connection_id,
        label=label,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        models=tuple(models),
    )


def _parse_model(raw: object, where: str) -> ConnectionModel:
    if not isinstance(raw, dict):
        raise InvalidConnectionError(f"{where}必须是对象。")
    return ConnectionModel(
        id=normalize_id(raw.get("id"), field=f"{where}的模型 ID"),
        label=normalize_label(raw.get("label"), field=f"{where}的 label"),
        model=normalize_model_name(raw.get("model")),
    )


class ConnectionStore:
    """连接文件的读写。

    写入是「同目录临时文件 + fsync + os.replace」：这个路径上任何时刻要么是
    旧内容、要么是完整的新内容，读不到半截文件；写入失败时旧文件原样保留。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> ConnectionFile:
        if not self.path.is_file():
            # 第一次运行还没有这个文件：与「没有模型连接功能」完全一致。
            return ConnectionFile.empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            # 只报「解析不了」，不带文件内容：里面的 api_key 就在那段文本里。
            raise ConnectionFileError(
                f"模型连接文件 {self.path} 无法解析为 JSON。"
            ) from error
        try:
            return _parse_file(raw)
        except InvalidConnectionError as error:
            # 位置与规则都在错误里，取值一个都不带。
            raise ConnectionFileError(
                f"模型连接文件 {self.path} 不合法：{error}"
            ) from error

    def save(self, data: ConnectionFile) -> None:
        payload = json.dumps(
            {
                "version": _FILE_VERSION,
                "default": data.default_id,
                "connections": [
                    _connection_payload(item) for item in data.connections
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        _write_atomically(self.path, payload + "\n")


def _parse_file(raw: object) -> ConnectionFile:
    if not isinstance(raw, dict):
        raise InvalidConnectionError("顶层必须是对象。")
    version = raw.get("version", _FILE_VERSION)
    if version != _FILE_VERSION:
        raise InvalidConnectionError(f"version 只支持 {_FILE_VERSION}。")

    default_id = raw.get("default")
    if default_id is not None:
        # 默认模型可以是内置的 extractive，因此这里放行保留 ID。
        default_id = normalize_id(default_id, field="default", allow_reserved=True)

    declared = raw.get("connections", [])
    if not isinstance(declared, list):
        raise InvalidConnectionError("connections 必须是数组。")
    if len(declared) > MAX_CONNECTIONS:
        raise InvalidConnectionError(f"connections 最多 {MAX_CONNECTIONS} 条。")

    connections: list[ModelConnection] = []
    seen: set[str] = set()
    for index, item in enumerate(declared, start=1):
        connection = parse_connection(item, where=f"connections 第 {index} 项")
        if connection.id in seen:
            raise InvalidConnectionError(
                f"connections 第 {index} 项的 id 与前面的连接重复。"
            )
        seen.add(connection.id)
        connections.append(connection)
    return ConnectionFile(default_id, tuple(connections))


def _connection_payload(connection: ModelConnection) -> dict[str, object]:
    return {
        "id": connection.id,
        "label": connection.label,
        "base_url": connection.base_url,
        "api_key": connection.api_key,
        "timeout": connection.timeout,
        "models": [model.describe() for model in connection.models],
    }


def _write_atomically(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _restrict_permissions(temp)
        os.replace(temp, path)
    except BaseException:
        # 失败就删掉临时文件：旧文件仍在原位，读者看到的还是上一份完整配置。
        temp.unlink(missing_ok=True)
        raise


def _restrict_permissions(path: Path) -> None:
    """尽力把文件收紧到「只有当前用户可读写」。

    POSIX 上是 0600（mkstemp 建出来本来就是这个权限，这里是显式声明）；Windows
    上 chmod 只影响只读位，访问控制来自目录 ACL。这只是纵深防御的一道，不是
    把文件变成保险箱 —— 主要边界是模型管理接口只接受环回客户端。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def build_registry(base: ModelRegistry, data: ConnectionFile) -> ModelRegistry:
    """把运行时连接叠加到基础模型清单之上，产出一份新快照。

    叠加过程中任何一处不成立就整体失败，调用方因此可以「先装配、后落盘」：
    装配成功说明这份配置能用，落盘失败也不会留下半份生效的配置。
    """
    entries = list(base.entries)
    generators = base.generator_map()
    declared = {entry.id for entry in entries}
    for connection in data.connections:
        for model in connection.models:
            if model.id in declared:
                raise ConnectionConflictError(
                    f"模型 ID {model.id} 与已有模型或同一文件里的另一条连接冲突。"
                )
            declared.add(model.id)
            entries.append(
                ModelEntry(
                    id=model.id,
                    label=model.label or model.id,
                    kind=KIND_OPENAI,
                    model=model.model,
                )
            )
            generators[model.id] = OpenAICompatibleAnswerGenerator(
                base_url=connection.base_url,
                api_key=connection.api_key,
                model=model.model,
                timeout=connection.timeout,
            )

    default_id = data.default_id or base.default_id
    registry = ModelRegistry(default_id, tuple(entries), generators)
    entry = registry.entry(default_id)
    if entry is None:
        raise ConnectionConflictError(f"默认模型 {default_id} 不在当前模型清单里。")
    if not entry.available:
        raise ConnectionConflictError(f"默认模型 {default_id} 不可用：{entry.reason}。")
    return registry


class ModelConnectionService:
    """模型连接的读写与运行时装配。

    它是唯一改动连接文件的地方，也是唯一替换运行注册表的地方。每次写操作都是
    一条**完整的配置事务**：

        读取当前配置 → 校验并构造候选配置 → 装配新注册表 → 原子落盘 → 替换快照

    这条事务由 `_lock` 整体串行化。只在落盘那一步加锁是不够的：候选配置是在
    读取之后、落盘之前从旧配置上构造出来的，两个并发请求若都读到同一份旧配置，
    后写入的那一份就会把前一份的改动整条抹掉 —— 两个 upsert 都返回成功，磁盘上
    却只剩一条连接。于是校验失败时磁盘与内存都不动；落盘失败时运行中的注册表
    仍是上一份完整的表。

    锁只覆盖配置事务，不覆盖网络：`discover_connection()` 在锁内固定一份不可变
    的连接快照，出了锁再请求远端 `/models`，一个慢网关不会挡住本机的连接增删改。
    只读的高频接口（`/models`、`/system`）读的是不可变注册表快照，不走这把锁。

    这是**进程内**锁：连接文件的读改写改在单进程内串行，多进程（多个 uvicorn
    worker 或多台机器）同时写同一个文件不在本批的保护范围内。
    """

    def __init__(
        self,
        base: ModelRegistry,
        store: ConnectionStore,
        *,
        discoverer: Discoverer = discover_models,
    ) -> None:
        self.base = base
        self.store = store
        self.registry = RefreshingModelRegistry(_initial_registry(base, store))
        self._discoverer = discoverer
        # 用 RLock 而不是 Lock：事务里的内部函数可以复用同一把锁而不自锁。
        # 两把锁的获取顺序始终是「先事务锁、后注册表锁」，不存在环。
        self._lock = RLock()

    def describe(self) -> dict[str, object]:
        """`GET /model-connections` 的响应体；任何情况下都不含 API Key。

        文件与注册表在同一把锁内读取：并发修改时不会把上一代的连接清单和
        这一代的默认模型拼进同一个响应里。
        """
        with self._lock:
            data = self.store.load()
            return {
                "default": self.registry.current.default_id,
                "connections": [item.describe() for item in data.connections],
            }

    def discover(
        self, *, base_url: object, api_key: object, timeout: object = None
    ) -> dict[str, object]:
        """用临时凭证试一次发现：既不落盘，也不建连接。

        它既不读也不写连接存储，因此不属于配置事务，也就不需要那把锁。
        """
        url = normalize_base_url(base_url)
        key = normalize_api_key(api_key)
        seconds = normalize_timeout(timeout)
        return {"base_url": url, "models": list(self._discoverer(url, key, seconds))}

    def discover_connection(self, connection_id: object) -> dict[str, object]:
        """用已保存的地址与密钥重新发现；客户端不必再提交一次密钥。

        锁内只做一件事：取一份不可变的连接快照。远程请求在锁外进行，一次
        几十秒的发现不会把连接的增删改挡在门外。连接随后被改动也不影响本次
        结果 —— 用的是开始时取到的那一份，密钥同理。
        """
        with self._lock:
            connection = self._find_in(self.store.load(), connection_id)
        models = self._discoverer(
            connection.base_url, connection.api_key, connection.timeout
        )
        return {"base_url": connection.base_url, "models": list(models)}

    def upsert(
        self,
        connection_id: object,
        *,
        base_url: object,
        label: object = "",
        api_key: object | None = None,
        timeout: object = None,
        models: Sequence[object] = (),
    ) -> dict[str, object]:
        """新增或整体替换一条连接。

        `api_key` 省略表示沿用已保存的密钥；显式给出空字符串一律报错，绝不
        让它悄悄把原密钥覆盖成空。

        「读旧配置 → 定密钥 → 改清单 → 落盘 → 替换」整条事务在锁内完成，
        并且只读一次文件：另一次并发写在事务开始前就已被挡在锁外。
        """
        with self._lock:
            data = self.store.load()
            existing = self._find_in(data, connection_id, required=False)
            if api_key is None:
                if existing is None:
                    raise InvalidConnectionError(
                        "创建连接时必须提供 api_key；更新时省略该字段表示沿用原密钥。"
                    )
                key = existing.api_key
            else:
                key = normalize_api_key(api_key)

            connection = parse_connection(
                {
                    "id": connection_id,
                    "label": label,
                    "base_url": base_url,
                    "api_key": key,
                    "timeout": timeout,
                    "models": list(models),
                },
                where="请求体",
            )
            connections = list(data.connections)
            index = next(
                (
                    position
                    for position, item in enumerate(connections)
                    if item.id == connection.id
                ),
                None,
            )
            if index is None:
                connections.append(connection)
            else:
                connections[index] = connection
            self._commit(ConnectionFile(data.default_id, tuple(connections)))
            return connection.describe()

    def delete(self, connection_id: object) -> dict[str, object]:
        """删除连接及它注册的模型；默认模型属于它时自动回到内置离线选项。"""
        with self._lock:
            if isinstance(connection_id, str) and connection_id in RESERVED_MODEL_IDS:
                raise InvalidConnectionError(
                    f"{GENERATOR_EXTRACTIVE} 是内置模型，不是可以删除的连接。"
                )
            data = self.store.load()
            connection = self._find_in(data, connection_id)
            remaining = tuple(
                item for item in data.connections if item.id != connection.id
            )
            removed = {model.id for model in connection.models}
            default_id = data.default_id
            if default_id in removed:
                # 默认模型跟着连接一起消失：回到内置离线选项，而不是留一个
                # 指向空处的默认值，让后续请求全部失败。
                default_id = GENERATOR_EXTRACTIVE
            self._commit(ConnectionFile(default_id, remaining))
            return {"id": connection.id, "default": self.registry.current.default_id}

    def set_default(self, model_id: object) -> dict[str, object]:
        """改默认模型；返回与 `GET /models` 同形的清单，界面一次刷新到位。

        选中的模型来自注册表快照，与随后写入的清单出自同一代配置。
        """
        with self._lock:
            # 内置的 extractive 可以选：把默认模型切回离线是正常操作。
            normalized = normalize_id(model_id, field="模型 ID", allow_reserved=True)
            entry = self.registry.current.entry(normalized)
            if entry is None:
                # 未知与不可用沿用既有的错误语义，与 /query、/extractions 一致。
                raise UnknownGeneratorError(f"未知的模型 ID：{normalized}")
            if not entry.available:
                raise UnavailableGeneratorError(entry)
            data = self.store.load()
            self._commit(ConnectionFile(normalized, data.connections))
            return self.registry.current.describe()

    def _commit(self, candidate: ConnectionFile) -> None:
        """校验 → 落盘 → 替换。三步的顺序就是这个功能的一致性边界。

        调用方必须持有 `_lock`：候选配置是在此之前从旧配置上构造出来的，
        只锁这三步挡不住并发写各自基于同一份旧配置构造候选。
        """
        snapshot = build_registry(self.base, candidate)
        self.store.save(candidate)
        self.registry.replace(snapshot)

    def _find_in(
        self, data: ConnectionFile, connection_id: object, *, required: bool = True
    ) -> ModelConnection | None:
        """在**已经读到的**那份配置里找连接。

        查找不重新读文件：一次事务只加载一次配置，读到的就是这次事务认定的
        那一代状态，中途被别的写操作改写也不会让同一次事务前后看到两份配置。
        """
        text = connection_id if isinstance(connection_id, str) else ""
        for connection in data.connections:
            if connection.id == text:
                return connection
        if required:
            raise ConnectionNotFoundError(f"未找到模型连接：{text}。")
        return None


def _initial_registry(base: ModelRegistry, store: ConnectionStore) -> ModelRegistry:
    """启动时装配一次；连接文件有问题就地报错，不带着半份配置继续跑。"""
    data = store.load()
    try:
        return build_registry(base, data)
    except ModelConnectionError as error:
        raise ConnectionFileError(
            f"模型连接文件 {store.path} 不可用：{error}"
        ) from error


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
