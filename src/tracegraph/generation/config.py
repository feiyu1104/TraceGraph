"""生成器配置：显式选择离线摘录，或 OpenAI-compatible 模型。

配置错误必须在启动时报出来，而不是等到第一次提问才失败；报错信息里只出现
变量名，绝不出现变量取值 —— 否则密钥会顺着异常日志泄露出去。
"""

from collections.abc import Mapping
from dataclasses import dataclass
import os


GENERATOR_EXTRACTIVE = "extractive"
GENERATOR_OPENAI = "openai-compatible"
GENERATORS = (GENERATOR_EXTRACTIVE, GENERATOR_OPENAI)

FALLBACK_NONE = "none"
FALLBACK_EXTRACTIVE = "extractive"
FALLBACKS = (FALLBACK_NONE, FALLBACK_EXTRACTIVE)

DEFAULT_TIMEOUT = 45.0


class GenerationConfigurationError(ValueError):
    """生成器配置缺失或取值非法。"""


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    generator: str = GENERATOR_EXTRACTIVE
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    fallback: str = FALLBACK_NONE

    @property
    def uses_llm(self) -> bool:
        return self.generator == GENERATOR_OPENAI


def load_generation_fallback(environ: Mapping[str, str] | None = None) -> str:
    """降级策略单独可读：模型列表改由配置文件决定时，它仍然只来自环境变量。"""
    env = os.environ if environ is None else environ
    fallback = _read(env, "TRACEGRAPH_LLM_FALLBACK") or FALLBACK_NONE
    if fallback not in FALLBACKS:
        raise GenerationConfigurationError(
            f"TRACEGRAPH_LLM_FALLBACK 只能是 {' 或 '.join(FALLBACKS)}，当前取值无法识别。"
        )
    return fallback


def load_generation_config(environ: Mapping[str, str] | None = None) -> GenerationConfig:
    env = os.environ if environ is None else environ
    generator = _read(env, "TRACEGRAPH_GENERATOR") or GENERATOR_EXTRACTIVE
    if generator not in GENERATORS:
        raise GenerationConfigurationError(
            f"TRACEGRAPH_GENERATOR 只能是 {' 或 '.join(GENERATORS)}，当前取值无法识别。"
        )
    fallback = load_generation_fallback(env)
    timeout = _read_timeout(env)
    if generator == GENERATOR_EXTRACTIVE:
        return GenerationConfig(fallback=fallback, timeout=timeout)

    values = {
        "TRACEGRAPH_LLM_BASE_URL": _read(env, "TRACEGRAPH_LLM_BASE_URL"),
        "TRACEGRAPH_LLM_API_KEY": _read(env, "TRACEGRAPH_LLM_API_KEY"),
        "TRACEGRAPH_LLM_MODEL": _read(env, "TRACEGRAPH_LLM_MODEL"),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise GenerationConfigurationError(
            f"TRACEGRAPH_GENERATOR={GENERATOR_OPENAI} 需要同时设置："
            f"{'、'.join(missing)}。若只想离线运行，请设为 {GENERATOR_EXTRACTIVE}。"
        )
    return GenerationConfig(
        generator=GENERATOR_OPENAI,
        base_url=values["TRACEGRAPH_LLM_BASE_URL"],
        api_key=values["TRACEGRAPH_LLM_API_KEY"],
        model=values["TRACEGRAPH_LLM_MODEL"],
        timeout=timeout,
        fallback=fallback,
    )


def _read(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def _read_timeout(env: Mapping[str, str]) -> float:
    raw = _read(env, "TRACEGRAPH_LLM_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        timeout = float(raw)
    except ValueError as error:
        raise GenerationConfigurationError(
            "TRACEGRAPH_LLM_TIMEOUT 必须是秒数。"
        ) from error
    if timeout <= 0:
        raise GenerationConfigurationError("TRACEGRAPH_LLM_TIMEOUT 必须大于 0。")
    return timeout
