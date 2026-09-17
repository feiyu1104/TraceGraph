from dataclasses import fields
import json
from urllib.error import HTTPError, URLError

import pytest

from tracegraph.core.contracts import Evidence
from tracegraph.generation import providers
from tracegraph.generation.providers import (
    GenerationNetworkError,
    GenerationResponseError,
    OpenAICompatibleAnswerGenerator,
)


_API_KEY = "sk-test-key"
_QUESTION = "百日咳用什么药"


def _evidence(evidence_id: str = "ev-1") -> Evidence:
    return Evidence(
        id=evidence_id,
        content="百日咳的推荐药物包括琥乙红霉素片。",
        document_id="doc-1",
        document_version="ver-1",
        source_name="dutmed-百日咳-推荐药物.md",
        locator="推荐药物",
        chunk_id="c1",
        retrieval_method="graph",
        retrieval_score=1.0,
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _generator() -> OpenAICompatibleAnswerGenerator:
    return OpenAICompatibleAnswerGenerator(
        base_url="https://gateway.example.com/v1/",
        api_key=_API_KEY,
        model="some-model",
    )


def _answer(
    text: str = "百日咳的推荐药物包括琥乙红霉素片。",
    claims: object = None,
) -> str:
    """模型响应里始终夹带一段 text —— 它必须被完全无视。"""
    if claims is None:
        claims = [{"text": text, "evidence_ids": ["ev-1"]}]
    return json.dumps({"text": text, "claims": claims}, ensure_ascii=False)


def _reply(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
    monkeypatch.setattr(
        providers, "urlopen", lambda request, timeout: _FakeResponse(body)
    )


def _fail(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    def raise_error(request, timeout):
        raise error

    monkeypatch.setattr(providers, "urlopen", raise_error)


def test_valid_response_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, _answer())

    generated = _generator().generate(_QUESTION, (_evidence(),))

    assert generated.claims[0].evidence_ids == ("ev-1",)
    assert "琥乙红霉素片" in generated.claims[0].text


def test_model_free_text_never_reaches_the_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 模型自带正文这件事本身就不被契约承认 —— 连字段都不存在，
    # 因此它没有任何渠道能进入 answer.text。
    _reply(
        monkeypatch,
        json.dumps(
            {
                "text": "百日咳应当立即使用琥乙红霉素片治疗。",
                "claims": [
                    {"text": "资料列出的推荐药物是琥乙红霉素片。", "evidence_ids": ["ev-1"]}
                ],
            },
            ensure_ascii=False,
        ),
    )

    generated = _generator().generate(_QUESTION, (_evidence(),))

    assert [field.name for field in fields(generated)] == ["claims"]
    assert "立即使用" not in generated.claims[0].text


def test_markdown_fenced_json_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, f"```json\n{_answer()}\n```")

    assert _generator().generate(_QUESTION, (_evidence(),)).claims


def test_fence_without_a_body_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, "```json")

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_content_that_is_not_json_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, "百日咳可以用琥乙红霉素片。")

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_response_body_that_is_not_json_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers, "urlopen", lambda request, timeout: _FakeResponse(b"<html>502</html>")
    )

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_response_without_choices_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        providers,
        "urlopen",
        lambda request, timeout: _FakeResponse(json.dumps({"error": "nope"}).encode()),
    )

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_unknown_evidence_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, _answer(claims=[{"text": "某结论", "evidence_ids": ["ev-999"]}]))

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_claim_without_evidence_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, _answer(claims=[{"text": "某结论", "evidence_ids": []}]))

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_empty_claims_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(monkeypatch, _answer(claims=[]))

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_one_invalid_claim_discards_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 部分答案会把未经验证的结论送上前端，因此整条回答作废。
    _reply(
        monkeypatch,
        _answer(
            claims=[
                {"text": "合法结论", "evidence_ids": ["ev-1"]},
                {"text": "编造结论", "evidence_ids": ["ev-999"]},
            ]
        ),
    )

    with pytest.raises(GenerationResponseError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_unknown_fields_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _reply(
        monkeypatch,
        json.dumps(
            {
                "text": "百日咳的推荐药物包括琥乙红霉素片。",
                "claims": [
                    {
                        "text": "百日咳的推荐药物包括琥乙红霉素片。",
                        "evidence_ids": ["ev-1", "ev-1"],
                        "confidence": 0.9,
                    }
                ],
                "graph_path": {"nodes": ["百日咳", "假节点"]},
                "diagnosis": "百日咳",
            },
            ensure_ascii=False,
        ),
    )

    generated = _generator().generate(_QUESTION, (_evidence(),))

    # 重复 ID 收敛，未知字段不进入契约。
    assert generated.claims[0].evidence_ids == ("ev-1",)
    assert [field.name for field in fields(generated.claims[0])] == [
        "text",
        "evidence_ids",
    ]


def test_http_error_is_reported_as_a_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail(
        monkeypatch,
        HTTPError(
            "https://gateway.example.com/v1/chat/completions?token=sekrit",
            401,
            "Unauthorized",
            None,
            None,
        ),
    )

    with pytest.raises(GenerationNetworkError) as error:
        _generator().generate(_QUESTION, (_evidence(),))

    assert error.value.error_code == "generation_network_error"
    assert "401" in str(error.value)
    # 端点里可能带凭证，错误消息不得回显地址。
    assert "sekrit" not in str(error.value)
    assert "gateway.example.com" not in str(error.value)


def test_connection_failure_is_reported_as_a_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail(monkeypatch, URLError(ConnectionRefusedError("refused")))

    with pytest.raises(GenerationNetworkError) as error:
        _generator().generate(_QUESTION, (_evidence(),))

    assert error.value.error_code == "generation_network_error"


def test_timeout_is_reported_as_a_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail(monkeypatch, TimeoutError("timed out"))

    with pytest.raises(GenerationNetworkError):
        _generator().generate(_QUESTION, (_evidence(),))


def test_request_keeps_the_key_out_of_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[providers.Request] = []

    def capture(request, timeout):
        captured.append(request)
        return _FakeResponse(
            json.dumps({"choices": [{"message": {"content": _answer()}}]}).encode("utf-8")
        )

    monkeypatch.setattr(providers, "urlopen", capture)
    _generator().generate(_QUESTION, (_evidence(),))

    request = captured[0]
    body = request.data.decode("utf-8")
    assert request.get_header("Authorization") == f"Bearer {_API_KEY}"
    assert _API_KEY not in body
    payload = json.loads(body)
    assert payload["model"] == "some-model"
    assert payload["response_format"] == {"type": "json_object"}


def test_prompt_marks_retrieved_text_as_untrusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[providers.Request] = []

    def capture(request, timeout):
        captured.append(request)
        return _FakeResponse(
            json.dumps({"choices": [{"message": {"content": _answer()}}]}).encode("utf-8")
        )

    monkeypatch.setattr(providers, "urlopen", capture)
    _generator().generate(_QUESTION, (_evidence(),))

    payload = json.loads(captured[0].data.decode("utf-8"))
    system = payload["messages"][0]["content"]
    assert "不可信" in system
    assert "不得补充常识性医学结论" in system
    assert "不得给出诊断" in system
    assert "禁止创造 ID" in system
    assert "不得输出任何图路径" in system

    user = payload["messages"][1]["content"]
    assert _QUESTION in user
    assert "ev-1" in user
    assert "百日咳的推荐药物包括琥乙红霉素片。" in user


def test_only_the_supplied_evidence_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[providers.Request] = []

    def capture(request, timeout):
        captured.append(request)
        return _FakeResponse(
            json.dumps({"choices": [{"message": {"content": _answer()}}]}).encode("utf-8")
        )

    monkeypatch.setattr(providers, "urlopen", capture)
    _generator().generate(_QUESTION, (_evidence(),))

    user = json.loads(captured[0].data.decode("utf-8"))["messages"][1]["content"]
    assert "ev-2" not in user
