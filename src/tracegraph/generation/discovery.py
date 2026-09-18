"""远端模型发现：读一次 OpenAI 兼容的 `GET {base_url}/models`。

这是模型连接功能里唯一会向服务端之外发请求的地方，因此它格外小：一个可
注入的 opener（测试用假的，不碰网络）、一份响应体大小上限、一次严格的清单
清洗。

上游的响应体与异常原文一律不外传：错误信息只描述我们这一侧看到的事实
（HTTP 状态、格式不符、连不上），永远不含密钥、Authorization 或上游正文。
"""

from collections.abc import Callable
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# 响应体上限：模型清单只有几 KB，2 MiB 已经远超任何正常网关。
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# 清单条数上限：一个超大清单既撑界面也撑注册表，超出部分直接丢弃。
MAX_MODELS = 200
MAX_MODEL_NAME_LENGTH = 200

# opener 的形状：接受 (request, timeout=...)，返回可当上下文管理器用的响应。
Opener = Callable[..., Any]


class ModelDiscoveryError(Exception):
    """模型发现失败；状态码与错误码由接口层直接采用。"""

    error_code = "discovery_failed"
    status_code = 502


class DiscoveryInvalidUrlError(ModelDiscoveryError):
    """地址不是 http(s)。调用方本应先做完整校验，这里是最后一道防线。"""

    error_code = "invalid_base_url"
    status_code = 400


class DiscoveryInvalidResponseError(ModelDiscoveryError):
    """远端响应不是 OpenAI 兼容的模型清单，或者超出大小上限。"""

    error_code = "discovery_invalid_response"
    status_code = 400


class DiscoveryUnauthorizedError(ModelDiscoveryError):
    """远端拒绝了这次请求（HTTP 401/403）：密钥不对，或没有访问权限。"""

    error_code = "discovery_unauthorized"
    status_code = 401


class DiscoveryUnreachableError(ModelDiscoveryError):
    """连不上远端：地址不对、网络不通或超时。"""

    error_code = "discovery_unreachable"
    status_code = 502


class DiscoveryUpstreamError(ModelDiscoveryError):
    """上游可达但报了错（非 2xx，且不是鉴权失败）。"""

    error_code = "discovery_upstream_error"
    status_code = 502


class _RejectRedirects(HTTPRedirectHandler):
    """不跟随跳转。

    urllib 会把 Authorization 原样带进跳转目标：一个被改过的网关只要回一个
    302，就能把密钥引到别的地址上。跳转因此被当成上游错误报出去。
    """

    def redirect_request(  # type: ignore[override]
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


_DEFAULT_OPENER: Opener = build_opener(_RejectRedirects).open


def discover_models(
    base_url: str,
    api_key: str,
    timeout: float,
    *,
    opener: Opener = _DEFAULT_OPENER,
) -> tuple[str, ...]:
    """请求 `{base_url}/models`，返回清洗过的远端模型 ID 清单。

    `opener` 就是测试替身的位置：接一个 `Request` 与 `timeout`，返回一个
    支持 `with` 的响应对象即可（`read(size)` 会被调用一次）。
    """
    request = Request(
        _models_url(base_url),
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            # 多读一个字节：读满上限才说明响应被截断了。
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        # 刻意不链上原异常：HTTPError 的 repr 里带着请求头，也就是密钥。
        raise _http_error(status) from None
    except OSError as error:
        # URLError 与超时都是 OSError；它们的 repr 里只有原因，没有请求头。
        raise DiscoveryUnreachableError(
            "无法连接远端模型服务，请检查地址与网络是否可达。"
        ) from error
    return _parse_models(body)


def _http_error(status: int) -> ModelDiscoveryError:
    if status in (401, 403):
        return DiscoveryUnauthorizedError(
            f"远端模型服务拒绝了这次请求（HTTP {status}），请检查 API Key 与访问权限。"
        )
    return DiscoveryUpstreamError(f"远端模型服务返回 HTTP {status}。")


def _models_url(base_url: str) -> str:
    text = base_url.strip().rstrip("/")
    if urlsplit(text).scheme not in ("http", "https"):
        raise DiscoveryInvalidUrlError("模型地址必须以 http:// 或 https:// 开头。")
    return f"{text}/models"


def _parse_models(body: bytes) -> tuple[str, ...]:
    if len(body) > MAX_RESPONSE_BYTES:
        limit = MAX_RESPONSE_BYTES // (1024 * 1024)
        raise DiscoveryInvalidResponseError(
            f"远端模型清单超过 {limit} MiB 上限，已拒绝解析。"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryInvalidResponseError(
            "远端模型清单不是 UTF-8 编码的 JSON。"
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DiscoveryInvalidResponseError(
            "远端响应不是 OpenAI 兼容格式：缺少 data 数组。"
        )

    models: list[str] = []
    seen: set[str] = set()
    for item in payload["data"]:
        name = _clean_model_name(item)
        if name is None or name in seen:
            continue
        seen.add(name)
        models.append(name)
        if len(models) >= MAX_MODELS:
            break
    return tuple(models)


def _clean_model_name(item: object) -> str | None:
    """取 `data[].id` 并清洗；拿不到可用的 ID 时返回 None（跳过这一条）。"""
    if not isinstance(item, dict):
        return None
    raw = item.get("id")
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or len(name) > MAX_MODEL_NAME_LENGTH:
        return None
    if not name.isprintable() or any(character.isspace() for character in name):
        return None
    return name
