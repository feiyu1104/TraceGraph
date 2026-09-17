import pytest

from tracegraph.generation.config import (
    DEFAULT_TIMEOUT,
    GenerationConfigurationError,
    load_generation_config,
    load_generation_fallback,
)


def test_absent_configuration_falls_back_to_offline_extraction() -> None:
    config = load_generation_config({})

    assert config.generator == "extractive"
    assert config.uses_llm is False
    assert config.timeout == DEFAULT_TIMEOUT
    assert config.fallback == "none"


def test_openai_compatible_requires_url_key_and_model() -> None:
    with pytest.raises(GenerationConfigurationError) as error:
        load_generation_config(
            {
                "TRACEGRAPH_GENERATOR": "openai-compatible",
                "TRACEGRAPH_LLM_BASE_URL": "https://gateway.example.com/v1",
            }
        )

    # 报错只点名缺了哪些变量，绝不回显任何取值。
    message = str(error.value)
    assert "TRACEGRAPH_LLM_API_KEY" in message
    assert "TRACEGRAPH_LLM_MODEL" in message
    assert "TRACEGRAPH_LLM_BASE_URL" not in message
    assert "gateway.example.com" not in message


def test_blank_values_count_as_missing() -> None:
    with pytest.raises(GenerationConfigurationError):
        load_generation_config(
            {
                "TRACEGRAPH_GENERATOR": "openai-compatible",
                "TRACEGRAPH_LLM_BASE_URL": "https://gateway.example.com/v1",
                "TRACEGRAPH_LLM_API_KEY": "   ",
                "TRACEGRAPH_LLM_MODEL": "some-model",
            }
        )


def test_complete_configuration_is_accepted() -> None:
    config = load_generation_config(
        {
            "TRACEGRAPH_GENERATOR": "openai-compatible",
            "TRACEGRAPH_LLM_BASE_URL": "https://gateway.example.com/v1",
            "TRACEGRAPH_LLM_API_KEY": "sk-secret",
            "TRACEGRAPH_LLM_MODEL": "some-model",
            "TRACEGRAPH_LLM_TIMEOUT": "12.5",
            "TRACEGRAPH_LLM_FALLBACK": "extractive",
        }
    )

    assert config.uses_llm is True
    assert config.model == "some-model"
    assert config.timeout == 12.5
    assert config.fallback == "extractive"


def test_unknown_generator_is_rejected() -> None:
    with pytest.raises(GenerationConfigurationError):
        load_generation_config({"TRACEGRAPH_GENERATOR": "langchain"})


def test_unknown_fallback_is_rejected() -> None:
    with pytest.raises(GenerationConfigurationError):
        load_generation_config({"TRACEGRAPH_LLM_FALLBACK": "silent"})


@pytest.mark.parametrize("value", ["abc", "0", "-3"])
def test_timeout_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(GenerationConfigurationError):
        load_generation_config({"TRACEGRAPH_LLM_TIMEOUT": value})


def test_fallback_is_readable_on_its_own() -> None:
    # 模型清单改由配置文件决定后，降级策略仍然可以单独读到。
    assert load_generation_fallback({"TRACEGRAPH_LLM_FALLBACK": "extractive"}) == (
        "extractive"
    )
    assert load_generation_fallback({}) == "none"


def test_unknown_fallback_is_rejected_on_its_own() -> None:
    with pytest.raises(GenerationConfigurationError):
        load_generation_fallback({"TRACEGRAPH_LLM_FALLBACK": "silent"})
