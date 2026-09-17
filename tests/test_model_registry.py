import json
from pathlib import Path

import pytest

from tracegraph.generation.config import GenerationConfigurationError
from tracegraph.generation.models import (
    UnknownGeneratorError,
    UnavailableGeneratorError,
    load_model_registry,
)

_MAIN_KEY = "TRACEGRAPH_MAIN_MODEL_API_KEY"
_FAST_KEY = "TRACEGRAPH_FAST_MODEL_API_KEY"

_ENV = {_MAIN_KEY: "sk-main", _FAST_KEY: "sk-fast"}


def _config(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "models.local.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _model(model_id: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": model_id,
        "label": model_id,
        "base_url": "https://gateway.example.com/v1",
        "model": f"{model_id}-name",
        "api_key_env": _MAIN_KEY,
        "timeout": 30,
    }
    entry.update(overrides)
    return entry


def test_without_a_config_file_the_environment_still_decides(tmp_path: Path) -> None:
    # 改造前的单模型环境变量配置必须继续可用。
    absent = tmp_path / "missing.json"

    registry = load_model_registry({}, absent)

    assert registry.default_id == "extractive"
    assert [entry.id for entry in registry.entries] == ["extractive"]


def test_environment_configured_model_is_still_reachable(tmp_path: Path) -> None:
    registry = load_model_registry(
        {
            "TRACEGRAPH_GENERATOR": "openai-compatible",
            "TRACEGRAPH_LLM_BASE_URL": "https://gateway.example.com/v1",
            "TRACEGRAPH_LLM_API_KEY": "sk-secret",
            "TRACEGRAPH_LLM_MODEL": "some-model",
        },
        tmp_path / "missing.json",
    )

    assert registry.default_id == "openai-compatible"
    assert registry.resolve("openai-compatible").model == "some-model"
    # 离线摘录始终可选。
    assert registry.resolve("extractive").name == "extractive"


def test_config_file_defaults_to_the_named_model(tmp_path: Path) -> None:
    path = _config(tmp_path, {"default": "main", "models": [_model("main")]})

    registry = load_model_registry(_ENV, path)

    assert registry.default_id == "main"
    assert registry.resolve("main").name == "openai-compatible"


def test_extractive_is_always_offered_and_cannot_be_redefined(
    tmp_path: Path,
) -> None:
    path = _config(
        tmp_path,
        {"default": "main", "models": [_model("main"), _model("extractive")]},
    )

    registry = load_model_registry(_ENV, path)

    assert [entry.id for entry in registry.entries] == ["extractive", "main", "extractive"]
    assert registry.resolve("extractive").name == "extractive"


def test_listing_never_carries_the_endpoint_or_the_key(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        {
            "default": "main",
            "models": [
                _model("main", base_url="https://gateway.example.com/v1?token=sekrit")
            ],
        },
    )

    listing = load_model_registry(_ENV, path).describe()

    assert listing["default"] == "main"
    assert listing["models"] == [
        {
            "id": "extractive",
            "label": "离线摘录（不调用任何模型）",
            "model": "",
            "available": True,
            "kind": "extractive",
            "reason": "",
        },
        {
            "id": "main",
            "label": "main",
            "model": "main-name",
            "available": True,
            "kind": "openai-compatible",
            "reason": "",
        },
    ]
    rendered = json.dumps(listing, ensure_ascii=False)
    assert "sekrit" not in rendered
    assert "gateway.example.com" not in rendered
    assert "sk-main" not in rendered


def test_system_status_reports_only_the_default_model(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        {"default": "main", "models": [_model("main"), _model("fast")]},
    )

    status = load_model_registry(_ENV, path).system_status("extractive")

    assert status == {
        "llm_configured": "true",
        "llm_model": "main-name",
        "llm_fallback": "extractive",
    }


def test_missing_key_marks_that_model_unavailable_without_failing_startup(
    tmp_path: Path,
) -> None:
    # 非默认模型配错了，也不该让离线模式起不来。
    path = _config(
        tmp_path,
        {
            "default": "main",
            "models": [_model("main"), _model("fast", api_key_env=_FAST_KEY)],
        },
    )

    registry = load_model_registry({_MAIN_KEY: "sk-main"}, path)

    fast = registry.entry("fast")
    assert fast is not None
    assert fast.available is False
    assert _FAST_KEY in fast.reason
    assert "sk-" not in fast.reason
    with pytest.raises(UnavailableGeneratorError) as error:
        registry.resolve("fast")
    assert error.value.error_code == "generator_unavailable"
    # 可用的模型不受影响。
    assert registry.resolve("main").name == "openai-compatible"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"base_url": ""}, "未配置 base_url"),
        ({"base_url": "ftp://gateway.example.com/v1"}, "http"),
        ({"model": ""}, "未配置 model"),
        ({"api_key_env": ""}, "未配置 api_key_env"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": "soon"}, "timeout"),
    ],
)
def test_each_broken_field_has_its_own_reason(
    tmp_path: Path, overrides: dict[str, object], expected: str
) -> None:
    path = _config(
        tmp_path,
        {"default": "extractive", "models": [_model("broken", **overrides)]},
    )

    entry = load_model_registry(_ENV, path).entry("broken")

    assert entry is not None
    assert entry.available is False
    assert expected in entry.reason


def test_unknown_generator_id_is_reported_separately(tmp_path: Path) -> None:
    path = _config(tmp_path, {"default": "main", "models": [_model("main")]})

    with pytest.raises(UnknownGeneratorError) as error:
        load_model_registry(_ENV, path).resolve("nope")

    assert error.value.error_code == "invalid_generator"


def test_duplicate_ids_do_not_break_the_first_entry(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        {"default": "main", "models": [_model("main"), _model("main")]},
    )

    registry = load_model_registry(_ENV, path)

    assert registry.resolve("main").name == "openai-compatible"
    assert [entry.available for entry in registry.entries] == [True, True, False]


def test_missing_default_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, {"models": [_model("main")]})

    with pytest.raises(GenerationConfigurationError) as error:
        load_model_registry(_ENV, path)

    assert "default" in str(error.value)


def test_default_pointing_nowhere_is_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, {"default": "ghost", "models": [_model("main")]})

    with pytest.raises(GenerationConfigurationError) as error:
        load_model_registry(_ENV, path)

    assert "ghost" in str(error.value)


def test_unavailable_default_is_rejected_loudly(tmp_path: Path) -> None:
    # 默认模型坏了必须报错：装作没事会让人以为自己正在用某个模型。
    path = _config(tmp_path, {"default": "main", "models": [_model("main")]})

    with pytest.raises(GenerationConfigurationError) as error:
        load_model_registry({}, path)

    assert _MAIN_KEY in str(error.value)
    assert "extractive" in str(error.value)


def test_broken_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "models.local.json"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(GenerationConfigurationError):
        load_model_registry(_ENV, path)


def test_reason_never_leaks_a_key_value(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        {"default": "extractive", "models": [_model("broken")]},
    )

    entry = load_model_registry({}, path).entry("broken")

    assert entry is not None
    assert "sk-" not in entry.reason
    assert "gateway.example.com" not in entry.reason
