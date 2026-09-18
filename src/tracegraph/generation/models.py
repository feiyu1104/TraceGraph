"""模型注册表：把「有哪些模型可选」与「密钥是什么」彻底分开。

配置文件只保存 API Key 对应的**环境变量名**，密钥本身只存在于 `.env` 或
系统环境变量。`ModelEntry.describe()` 是唯一的对外描述，它刻意不含
`base_url` —— 自建网关常把凭证写在地址上，连地址也不透出。

模型选择是每次请求的事：`resolve()` 只返回一个生成器实例，不改动任何
进程级状态，因此并发用户之间不会互相影响。
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from threading import Lock

from tracegraph.generation.config import (
    DEFAULT_TIMEOUT,
    GENERATOR_EXTRACTIVE,
    GENERATOR_OPENAI,
    GenerationConfigurationError,
    load_generation_config,
)
from tracegraph.generation.providers import (
    AnswerGenerator,
    ExtractiveAnswerGenerator,
    OpenAICompatibleAnswerGenerator,
)


KIND_EXTRACTIVE = "extractive"
KIND_OPENAI = "openai-compatible"

# `extractive` 是内置的离线选项，任何时候都可以被选中，且不能被配置覆盖。
EXTRACTIVE_LABEL = "离线摘录（不调用任何模型）"

CONFIG_PATH_ENV = "TRACEGRAPH_MODELS_CONFIG"
DEFAULT_CONFIG_PATH = Path("config/models.local.json")

# 条目不可用时对外显示的原因前缀；原因里只出现变量名，绝不出现取值。
_MISSING_KEY_REASON = "环境变量 {name} 未设置"


class UnknownGeneratorError(ValueError):
    """请求的 `generator_id` 不在注册表里。"""

    error_code = "invalid_generator"


class UnavailableGeneratorError(ValueError):
    """模型存在，但当前缺地址或缺密钥，因此不能使用。"""

    error_code = "generator_unavailable"

    def __init__(self, entry: "ModelEntry") -> None:
        super().__init__(f"模型 {entry.id} 当前不可用：{entry.reason}。")
        self.entry = entry


@dataclass(frozen=True, slots=True)
class ModelEntry:
    id: str
    label: str
    kind: str
    model: str = ""
    available: bool = True
    reason: str = ""

    def describe(self) -> dict[str, object]:
        """浏览器可见的全部字段；不含 base_url，也不含任何密钥。"""
        return {
            "id": self.id,
            "label": self.label,
            "model": self.model,
            "available": self.available,
            "kind": self.kind,
            "reason": self.reason,
        }


class ModelRegistry:
    """一次装配好的模型清单。

    不可用的条目**仍然出现在清单里**并带着原因，这样前端能解释「为什么这个
    模型选不了」，而不是让它凭空消失。
    """

    def __init__(
        self,
        default_id: str,
        entries: tuple[ModelEntry, ...],
        generators: Mapping[str, AnswerGenerator],
    ) -> None:
        self.default_id = default_id
        self.entries = tuple(entries)
        self._generators = dict(generators)
        # 先出现者优先：重复 ID 的后续条目在装配时就已被标记为不可用，
        # 查找时不能再让它覆盖真正生效的那一条。
        self._by_id: dict[str, ModelEntry] = {}
        for entry in self.entries:
            self._by_id.setdefault(entry.id, entry)

    @property
    def current(self) -> "ModelRegistry":
        """本次调用应当使用的那一份快照。

        注册表自身就是一份不可变快照，因此这里返回自己；可刷新的注册表
        （`RefreshingModelRegistry`）返回当下生效的那一份。服务对象按这份
        快照做每一步推导，两种注册表因此可以互换着装配。
        """
        return self

    @property
    def default_entry(self) -> ModelEntry:
        return self._by_id[self.default_id]

    @property
    def default_generator(self) -> AnswerGenerator:
        return self._generators[self.default_id]

    def generator_map(self) -> Mapping[str, AnswerGenerator]:
        """按 ID 索引的生成器副本；合并运行时模型时要用到基础模型的生成器。"""
        return dict(self._generators)

    def entry(self, generator_id: str) -> ModelEntry | None:
        return self._by_id.get(generator_id)

    def describe(self) -> dict[str, object]:
        """`GET /models` 的响应体。"""
        return {
            "default": self.default_id,
            "models": [entry.describe() for entry in self.entries],
        }

    def system_status(self, fallback: str) -> dict[str, str]:
        """`/system` 的安全摘要：只报默认模型与降级策略。

        单个模型名、密钥与网关地址一概不出现在这里。
        """
        entry = self.default_entry
        return {
            "llm_configured": "true" if entry.kind == KIND_OPENAI else "false",
            "llm_model": entry.model,
            "llm_fallback": fallback,
        }

    def resolve(self, generator_id: str) -> AnswerGenerator:
        """按 ID 取生成器；未知与不可用是两种不同的失败，分别对应 400 与 503。"""
        entry = self._by_id.get(generator_id)
        if entry is None:
            raise UnknownGeneratorError(f"未知的模型 ID：{generator_id}")
        if not entry.available:
            raise UnavailableGeneratorError(entry)
        return self._generators[generator_id]

    def resolve_selection(
        self, requested_id: str | None = None
    ) -> tuple[str, AnswerGenerator]:
        """一次调用同时给出「按哪个 ID 报告」与「用哪个生成器」。

        两者必须来自同一份快照：分两次取会在注册表被替换的瞬间错配 ——
        ID 来自旧表、生成器来自新表，metrics 于是报出一个跟实际产物对不上
        的名字。返回的生成器由调用方一直用到本次请求结束。
        """
        model_id = requested_id or self.default_id
        return model_id, self.resolve(model_id)

    def id_of(self, generator: AnswerGenerator) -> str:
        """反查生成器实例的模型 ID，供 metrics 报告「这次真正用了谁」。

        不能用生成器类的 `name`：同一个类可以挂在多个 ID 下，而界面是按 ID
        去清单里取显示名称的。清单外的生成器（例如显式降级用的离线摘录）
        只能退回它的 `name`。
        """
        for model_id, candidate in self._generators.items():
            if candidate is generator:
                return model_id
        return getattr(generator, "name", "custom")


class RefreshingModelRegistry:
    """可整体替换的注册表句柄，供运行时增删模型使用。

    服务对象长期持有这个句柄而不是某一份快照，因此刷新之后不会有谁还在用
    旧表。快照本身不可变，替换就是一次引用赋值：读取不加锁 —— 读到的要么
    是替换前的整份表、要么是替换后的整份表，不存在改了一半的表；写入加锁
    是为了标明「这是一次整体替换」，而不是逐字段修改。

    已经在跑的请求拿着它自己解析出的生成器继续跑完，不受替换影响。
    """

    def __init__(self, initial: ModelRegistry) -> None:
        self._snapshot = initial
        self._lock = Lock()

    @property
    def current(self) -> ModelRegistry:
        return self._snapshot

    def replace(self, registry: ModelRegistry) -> None:
        with self._lock:
            self._snapshot = registry


# 服务对象接受的两种注册表：一份固定快照，或一个可刷新的快照句柄。
RegistrySource = ModelRegistry | RefreshingModelRegistry


def single_generator_registry(generator: AnswerGenerator) -> ModelRegistry:
    """没有模型配置时的退化形态：当前生成器就是唯一可选项。"""
    name = getattr(generator, "name", "custom")
    kind = KIND_EXTRACTIVE if name == GENERATOR_EXTRACTIVE else KIND_OPENAI
    label = EXTRACTIVE_LABEL if kind == KIND_EXTRACTIVE else name
    entry = ModelEntry(
        id=name,
        label=label,
        kind=kind,
        model=getattr(generator, "model", "") or "",
    )
    return ModelRegistry(name, (entry,), {name: generator})


def load_model_registry(
    environ: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> ModelRegistry:
    """模型配置文件优先；没有配置文件时退回环境变量单模型配置。

    非默认条目的配置错误只会让它自己被标记为不可用 —— 一个写坏的次要模型
    不应该让离线模式起不来。默认条目出错则直接报错：装作没事会让用户以为
    自己正在用某个模型，实际没有。
    """
    env = os.environ if environ is None else environ
    path = _config_path(env, config_path)
    if path.is_file():
        return _from_file(path, env)
    return _from_env(env)


def _config_path(env: Mapping[str, str], config_path: str | Path | None) -> Path:
    if config_path is not None:
        return Path(config_path)
    override = (env.get(CONFIG_PATH_ENV) or "").strip()
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _from_env(env: Mapping[str, str]) -> ModelRegistry:
    """改造前的单模型环境变量配置，行为逐位保留。"""
    config = load_generation_config(env)
    entries = [_extractive_entry()]
    generators: dict[str, AnswerGenerator] = {
        GENERATOR_EXTRACTIVE: ExtractiveAnswerGenerator()
    }
    if not config.uses_llm:
        return ModelRegistry(GENERATOR_EXTRACTIVE, tuple(entries), generators)

    entries.append(
        ModelEntry(
            id=GENERATOR_OPENAI,
            label="环境变量配置的模型",
            kind=KIND_OPENAI,
            model=config.model,
        )
    )
    generators[GENERATOR_OPENAI] = OpenAICompatibleAnswerGenerator(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        timeout=config.timeout,
    )
    return ModelRegistry(GENERATOR_OPENAI, tuple(entries), generators)


def _from_file(path: Path, env: Mapping[str, str]) -> ModelRegistry:
    data = _read_config(path)
    default_id = data.get("default")
    if not isinstance(default_id, str) or not default_id.strip():
        raise GenerationConfigurationError(
            f"模型配置文件 {path} 必须给出非空的 default。"
        )
    default_id = default_id.strip()
    raw_models = data.get("models")
    if not isinstance(raw_models, list):
        raise GenerationConfigurationError(
            f"模型配置文件 {path} 的 models 必须是数组。"
        )

    entries = [_extractive_entry()]
    generators: dict[str, AnswerGenerator] = {
        GENERATOR_EXTRACTIVE: ExtractiveAnswerGenerator()
    }
    declared = {GENERATOR_EXTRACTIVE}
    for index, raw in enumerate(raw_models, start=1):
        entry, generator = _build_entry(raw, index, env)
        if entry.id in declared:
            # ID 必须唯一。冲突的条目退化为「不可用」而不是让整个配置失败，
            # 先出现的那一条继续生效。
            entry = replace(
                entry, available=False, reason="模型 ID 与其他条目重复"
            )
            generator = None
        declared.add(entry.id)
        entries.append(entry)
        if generator is not None:
            generators[entry.id] = generator

    registry = ModelRegistry(default_id, tuple(entries), generators)
    default_entry = registry.entry(default_id)
    if default_entry is None:
        raise GenerationConfigurationError(
            f"模型配置文件 {path} 的 default={default_id} 没有对应的模型条目。"
        )
    if not default_entry.available:
        raise GenerationConfigurationError(
            f"默认模型 {default_id} 不可用：{default_entry.reason}。"
            f"请修正配置，或把 default 设为 {GENERATOR_EXTRACTIVE}。"
        )
    return registry


def _read_config(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GenerationConfigurationError(
            f"模型配置文件 {path} 无法解析为 JSON。"
        ) from error
    if not isinstance(data, dict):
        raise GenerationConfigurationError(
            f"模型配置文件 {path} 的顶层必须是对象。"
        )
    return data


def _extractive_entry() -> ModelEntry:
    return ModelEntry(
        id=GENERATOR_EXTRACTIVE, label=EXTRACTIVE_LABEL, kind=KIND_EXTRACTIVE
    )


def _build_entry(
    raw: object, index: int, env: Mapping[str, str]
) -> tuple[ModelEntry, AnswerGenerator | None]:
    if not isinstance(raw, dict):
        raise GenerationConfigurationError(f"models 第 {index} 项必须是对象。")

    model_id = _text(raw.get("id"))
    if not model_id:
        raise GenerationConfigurationError(f"models 第 {index} 项缺少 id。")

    label = _text(raw.get("label")) or model_id
    model = _text(raw.get("model"))
    base_url = _text(raw.get("base_url"))
    api_key_env = _text(raw.get("api_key_env"))
    timeout, timeout_error = _timeout(raw.get("timeout"))

    reason = timeout_error or _unavailable_reason(
        model, base_url, api_key_env, env
    )
    if reason:
        return (
            ModelEntry(
                id=model_id,
                label=label,
                kind=KIND_OPENAI,
                model=model,
                available=False,
                reason=reason,
            ),
            None,
        )

    return (
        ModelEntry(id=model_id, label=label, kind=KIND_OPENAI, model=model),
        OpenAICompatibleAnswerGenerator(
            base_url=base_url,
            api_key=(env.get(api_key_env) or "").strip(),
            model=model,
            timeout=timeout,
        ),
    )


def _unavailable_reason(
    model: str, base_url: str, api_key_env: str, env: Mapping[str, str]
) -> str:
    """不可用的第一条原因；空字符串表示这个条目可用。

    只回变量名与结构问题，不回显任何取值。
    """
    if not base_url:
        return "未配置 base_url"
    if not base_url.startswith(("http://", "https://")):
        return "base_url 必须以 http:// 或 https:// 开头"
    if not model:
        return "未配置 model"
    if not api_key_env:
        return "未配置 api_key_env"
    if not (env.get(api_key_env) or "").strip():
        return _MISSING_KEY_REASON.format(name=api_key_env)
    return ""


def _timeout(value: object) -> tuple[float, str]:
    """返回 (秒数, 不可用原因)；原因非空时秒数无意义。"""
    if value is None:
        return DEFAULT_TIMEOUT, ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT, "timeout 必须是秒数"
    if value <= 0:
        return DEFAULT_TIMEOUT, "timeout 必须大于 0"
    return float(value), ""


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
