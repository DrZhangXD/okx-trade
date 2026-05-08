"""REST 底层传输：签名注入 + 限频 + 重试 + 错误分类。

业务方法（market/public/account/trade）都通过 ``Transport.request()`` 发请求，
不直接碰 httpx。这样：
- 签名/限频/重试逻辑只写一份；
- 测试只需 mock httpx，业务模块的单测用 mock Transport 即可。

核心调用链：
    业务方法 →  transport.request(method, path, ..., group="trade.order")
        ↓
    rate_limiter.acquire(group)         # 限频
        ↓
    build_rest_headers(...)             # 签名（如果是私有接口）
        ↓
    httpx.AsyncClient.request(...)      # 发请求
        ↓
    解析 OKX 响应包（code/msg/data）   # 业务错误抛异常，否则返回 data
        ↓
    遇到网络错/限频 → 重试（指数退避）
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlencode

import httpx

from ..auth import build_rest_headers
from ..config import OKXSettings
from ..exceptions import (
    OKXAPIError,
    OKXNetworkError,
    OKXRateLimitError,
    classify_business_error,
)
from ..utils.logging import get_logger
from .ratelimit import RateLimiter, build_default_limiter

log = get_logger(__name__)

JsonDict = dict[str, Any]


class Transport:
    """异步 OKX REST 传输层。线程安全（asyncio task 安全）；可作为 ``async with``
    资源使用，退出时关闭底层 ``httpx.AsyncClient``。"""

    def __init__(
        self,
        settings: OKXSettings,
        *,
        rate_limiter: RateLimiter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.rate_limiter = rate_limiter if rate_limiter is not None else build_default_limiter()

        if client is None:
            # 代理：httpx 0.27+ 用 proxy= 参数（单数），同时影响 http/https。
            # 注意：.env 里写 `OKX_HTTP_PROXY=`（空串）也会被 pydantic 解成 ""，
            # 直接传给 httpx 会触发 "Unknown scheme" 报错——这里把空白值规整成 None。
            proxy = (settings.https_proxy or "").strip() or (settings.http_proxy or "").strip() or None
            self._client = httpx.AsyncClient(
                base_url=settings.effective_rest_base_url,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=settings.request_timeout_sec,
                    write=settings.request_timeout_sec,
                    pool=5.0,
                ),
                proxy=proxy,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    # ------------------------------------------------------------------
    # 资源管理
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Transport:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: JsonDict | list[JsonDict] | None = None,
        private: bool = False,
        group: str | None = None,
    ) -> list[JsonDict]:
        """发一次 OKX REST 请求并返回 ``data`` 字段。

        参数
        ----
        method
            "GET" / "POST" 等。
        path
            请求路径（含 ``/api/v5`` 前缀），如 ``"/api/v5/market/candles"``。
        params
            URL query 参数；GET 用。
        json_body
            POST body；自动 ``json.dumps``。
        private
            是否需要鉴权。私有接口前置检查凭证是否齐全。
        group
            限频分组名（参见 ``ratelimit.DEFAULT_OKX_LIMITS``）。``None`` 表示不限频。

        返回
        ----
        OKX 响应包的 ``data`` 字段（始终是 list[dict]）。

        异常
        ----
        - ``OKXAPIError``（及子类）：业务错误（code != "0"），不重试；
        - ``OKXRateLimitError``：限频，会在内部重试，超过 ``max_retries`` 后向上抛；
        - ``OKXNetworkError``：网络错误，会在内部重试，超过 ``max_retries`` 后向上抛。
        """
        if private and not self.settings.has_credentials():
            from ..exceptions import OKXAuthError
            raise OKXAuthError("missing", "OKX credentials not configured", endpoint=path)

        # 拼装完整请求路径（含 query string）——签名时必须用这个完整 path
        if params:
            # OKX 接受 sorted/unsorted；我们 sort 便于测试断言一致性
            qs = urlencode(sorted(params.items()))
            full_path = f"{path}?{qs}"
        else:
            full_path = path

        body_str = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""

        max_retries = self.settings.max_retries
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            # 限频
            if group:
                await self.rate_limiter.acquire(group)

            # 签名（每次重试都重新签，timestamp 必须新鲜）
            headers: dict[str, str]
            if private:
                api_key, secret, passphrase = self.settings.credentials()
                headers = build_rest_headers(
                    api_key=api_key,
                    secret=secret,
                    passphrase=passphrase,
                    method=method,
                    request_path=full_path,
                    body=body_str,
                    is_demo=self.settings.is_demo,
                )
            else:
                headers = {"Content-Type": "application/json"}
                if self.settings.is_demo:
                    headers["x-simulated-trading"] = "1"

            try:
                response = await self._client.request(
                    method=method.upper(),
                    url=full_path,
                    content=body_str if body_str else None,
                    headers=headers,
                )
            except httpx.TimeoutException as e:
                last_exc = OKXNetworkError(f"timeout: {e}")
            except httpx.HTTPError as e:
                last_exc = OKXNetworkError(f"http error: {type(e).__name__}: {e}")
            else:
                # 处理 HTTP 层错误
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "1"))
                    if group:
                        await self.rate_limiter.cool_down(group, retry_after)
                    last_exc = OKXRateLimitError(
                        f"HTTP 429 on {path}", retry_after=retry_after,
                    )
                elif 500 <= response.status_code < 600:
                    last_exc = OKXNetworkError(
                        f"HTTP {response.status_code} on {path}: {response.text[:200]}",
                    )
                elif response.status_code >= 400:
                    # 4xx（除 429）通常是请求构造错，重试无意义
                    raise OKXAPIError(
                        code=str(response.status_code),
                        message=response.text[:500],
                        endpoint=path,
                    )
                else:
                    # 解析业务码
                    try:
                        payload = response.json()
                    except json.JSONDecodeError as e:
                        raise OKXAPIError(
                            code="parse_error",
                            message=f"invalid JSON: {e}",
                            endpoint=path,
                        ) from e

                    code = str(payload.get("code", ""))
                    if code == "0":
                        return payload.get("data", []) or []

                    # 业务错误：限频码会被 classify 抛 OKXRateLimitError；其他不重试
                    try:
                        err = classify_business_error(
                            code=code,
                            message=payload.get("msg", ""),
                            data=payload.get("data"),
                            endpoint=path,
                        )
                    except OKXRateLimitError as rl:
                        last_exc = rl
                    else:
                        raise err

            # 走到这里说明 last_exc 已被赋值，需要决定是否重试
            if attempt < max_retries:
                wait = _backoff_delay(attempt, last_exc)
                log.warning(
                    "rest_retry",
                    path=path,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    error=type(last_exc).__name__,
                    wait_sec=wait,
                )
                await asyncio.sleep(wait)
            else:
                break

        assert last_exc is not None
        raise last_exc


def _backoff_delay(attempt: int, exc: Exception | None) -> float:
    """退避策略：限频用 retry_after，网络错用指数退避（2 → 4 → 8 → ...）。"""
    if isinstance(exc, OKXRateLimitError) and exc.retry_after:
        return exc.retry_after
    return 2.0 * (2 ** attempt)


__all__ = ["Transport"]
