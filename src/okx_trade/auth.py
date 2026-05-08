"""OKX HMAC-SHA256 签名（纯函数，无 IO）。

OKX v5 文档规定：
    sign = base64( HMAC_SHA256( secret, timestamp + method + requestPath + body ) )

- ``timestamp`` 用 ISO-8601 UTC 字符串（``2024-04-19T12:34:56.789Z``）。
- ``method`` 必须大写（GET/POST）。
- ``requestPath`` 包含 query string，例如 ``/api/v5/market/candles?instId=BTC-USDT``。
- GET 请求的 ``body`` 传空字符串。

WebSocket login 帧用同一签名规则，但固定参数：``method="GET"``、
``request_path="/users/self/verify"``、``body=""``。

把签名抽成纯函数的好处：
1. 无 httpx/websockets 依赖，单测无需任何 IO；
2. REST 与 WS 共用同一函数，避免双份维护；
3. 用 OKX 官方测试向量（如有）或自洽 round-trip 验证即可。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Final

from .utils.time import iso_utc_now

# WS login 用的固定路径；常量化避免拼写错。
_WS_LOGIN_METHOD: Final = "GET"
_WS_LOGIN_PATH: Final = "/users/self/verify"


def sign(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: str = "",
) -> str:
    """生成 OKX 签名。所有参数均为字符串；返回 base64 编码字符串。"""
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_rest_headers(
    api_key: str,
    secret: str,
    passphrase: str,
    method: str,
    request_path: str,
    body: str = "",
    *,
    is_demo: bool = False,
    timestamp: str | None = None,
) -> dict[str, str]:
    """组装 REST 请求所需的鉴权 header。

    ``timestamp`` 留空时用 ``iso_utc_now()`` 自动生成；测试时传固定值便于断言。
    模拟盘需要追加 ``x-simulated-trading: 1`` header（live 不带）。
    """
    ts = timestamp if timestamp is not None else iso_utc_now()
    signature = sign(secret, ts, method, request_path, body)
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    if is_demo:
        headers["x-simulated-trading"] = "1"
    return headers


def build_ws_login_args(
    api_key: str,
    secret: str,
    passphrase: str,
    *,
    timestamp: str | None = None,
) -> dict[str, str]:
    """生成 WS login 帧的 ``args[0]`` 字典，调用方组装 ``op=login`` 帧后发出。

    OKX WS login 用 Unix 秒（带小数）作为 timestamp，**不是 ISO 字符串**。
    这与 REST 不同，要小心——本函数自动生成正确格式。
    """
    if timestamp is None:
        # WS login 用 Unix 秒（字符串），可带小数毫秒。
        import time
        timestamp = f"{time.time():.3f}"
    signature = sign(secret, timestamp, _WS_LOGIN_METHOD, _WS_LOGIN_PATH, "")
    return {
        "apiKey": api_key,
        "passphrase": passphrase,
        "timestamp": timestamp,
        "sign": signature,
    }


__all__ = ["build_rest_headers", "build_ws_login_args", "sign"]
