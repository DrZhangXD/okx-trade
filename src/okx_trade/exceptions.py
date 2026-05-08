"""OKX exception hierarchy.

设计原则
--------
- ``OKXError`` 为所有异常根，用户可以一把 ``except OKXError`` 兜底。
- 网络层错误（``OKXNetworkError``、``OKXRateLimitError``）由 transport 层判定后抛出，
  装饰器会重试它们。
- 业务错误（``OKXAPIError`` 及子类）来自 OKX 返回 ``code != "0"``，**不重试**——
  与 scalp 项目语义保持一致：业务错误重试只会浪费配额。
- 常见业务码单独子类化（如 ``OKXAuthError``、``OKXInsufficientBalance``），便于上层
  ``except`` 精确捕获，无需自己 match 错误码字符串。
"""
from __future__ import annotations

from typing import Any


class OKXError(Exception):
    """所有 OKX 相关异常的根。"""


# ---------------------------------------------------------------------------
# 传输层错误（可重试）
# ---------------------------------------------------------------------------

class OKXNetworkError(OKXError):
    """底层网络问题：超时、连接 reset、DNS 失败等。重试装饰器会重试。"""


class OKXRateLimitError(OKXError):
    """触发 OKX 限频（HTTP 429 或业务码 50011/50013）。

    携带 ``retry_after`` 提示等待秒数；transport 收到后会先等待再重试。
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# 业务错误（不重试）
# ---------------------------------------------------------------------------

class OKXAPIError(OKXError):
    """OKX 返回 code != "0" 的业务错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        data: Any | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        self.endpoint = endpoint
        prefix = f"[{endpoint}] " if endpoint else ""
        super().__init__(f"{prefix}OKX API error code={code}: {message}")


class OKXAuthError(OKXAPIError):
    """鉴权相关错误（API key 失效、签名错、IP 不在白名单等）。

    OKX 错误码示例：50100, 50101, 50102, 50103, 50105, 50111, 50112, 50113, 50114。
    """


class OKXInsufficientBalance(OKXAPIError):
    """余额不足（OKX code=51008 等）。"""


class OKXOrderNotFound(OKXAPIError):
    """订单不存在（OKX code=51400 等）。"""


# ---------------------------------------------------------------------------
# WebSocket 错误
# ---------------------------------------------------------------------------

class OKXWSError(OKXError):
    """WebSocket 相关错误根。"""


class OKXWSConnectionError(OKXWSError):
    """WS 连接握手失败、断开、重连超出次数等。"""


class OKXSubscriptionError(OKXWSError):
    """订阅失败（频道不存在、参数错、未鉴权访问私有频道等）。"""


# ---------------------------------------------------------------------------
# 错误码 → 异常类映射
# ---------------------------------------------------------------------------

# 部分 OKX 业务错误码 → 专用异常类的映射。Transport 层统一查这张表，命中后
# 抛子类，否则抛 OKXAPIError。新业务码按需扩充即可。
_AUTH_CODES = frozenset({
    "50100", "50101", "50102", "50103", "50104", "50105",
    "50111", "50112", "50113", "50114",
})
_RATE_LIMIT_CODES = frozenset({"50011", "50013"})
_INSUFFICIENT_BALANCE_CODES = frozenset({"51008"})
_ORDER_NOT_FOUND_CODES = frozenset({"51400", "51401", "51402"})


def classify_business_error(
    code: str,
    message: str,
    data: Any | None = None,
    endpoint: str | None = None,
) -> OKXAPIError:
    """根据 OKX 业务错误码返回对应的异常实例。

    限频码（50011/50013）单独抛 ``OKXRateLimitError``，由 transport 处理重试。
    其他码按映射表抛子类，未命中抛通用 ``OKXAPIError``。
    """
    if code in _RATE_LIMIT_CODES:
        # 限频是可重试的，单独走另一个分支
        raise OKXRateLimitError(f"OKX rate limited: code={code}, msg={message}")
    if code in _AUTH_CODES:
        return OKXAuthError(code, message, data, endpoint)
    if code in _INSUFFICIENT_BALANCE_CODES:
        return OKXInsufficientBalance(code, message, data, endpoint)
    if code in _ORDER_NOT_FOUND_CODES:
        return OKXOrderNotFound(code, message, data, endpoint)
    return OKXAPIError(code, message, data, endpoint)


__all__ = [
    "OKXAPIError",
    "OKXAuthError",
    "OKXError",
    "OKXInsufficientBalance",
    "OKXNetworkError",
    "OKXOrderNotFound",
    "OKXRateLimitError",
    "OKXSubscriptionError",
    "OKXWSConnectionError",
    "OKXWSError",
    "classify_business_error",
]
