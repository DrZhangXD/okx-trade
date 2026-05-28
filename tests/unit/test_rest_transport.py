"""Transport 层测试，用 httpx.MockTransport 注入响应。

覆盖：
- 公共接口（无签名 header）
- 私有接口（带签名 header + demo header）
- 业务错误（code != "0"）抛 OKXAPIError，不重试
- 限频码 50011 抛 OKXRateLimitError，会重试
- HTTP 429 → 重试
- HTTP 500 → 重试
- 网络超时 → 重试
- 重试次数耗尽 → 向上抛
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from okx_trade.config import OKXSettings
from okx_trade.exceptions import (
    OKXAPIError,
    OKXAuthError,
    OKXNetworkError,
)
from okx_trade.rest.ratelimit import RateLimiter
from okx_trade.rest.transport import Transport


def _make_settings(**overrides: Any) -> OKXSettings:
    base = dict(
        api_key="key",
        secret_key="secret",
        passphrase="pass",
        is_demo=True,
        max_retries=2,
        request_timeout_sec=2.0,
    )
    base.update(overrides)
    return OKXSettings(_env_file=None, **base)


def _make_transport(handler: httpx.MockTransport, **settings_overrides: Any) -> Transport:
    settings = _make_settings(**settings_overrides)
    client = httpx.AsyncClient(
        base_url=settings.effective_rest_base_url,
        transport=handler,
    )
    # 不限频 → 测试不会被限频影响时序
    return Transport(settings, rate_limiter=RateLimiter(), client=client)


# ============================================================================
# 成功路径
# ============================================================================

class TestSuccessPath:
    async def test_public_request_no_auth_header(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(req.headers)
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"a": 1}]})

        async with _make_transport(httpx.MockTransport(handler)) as t:
            data = await t.request("GET", "/api/v5/market/candles", params={"instId": "BTC-USDT"})

        assert data == [{"a": 1}]
        # 公共接口不应该带签名 header
        assert "OK-ACCESS-SIGN" not in captured["headers"]
        # demo 模式应该带 simulated-trading header
        assert captured["headers"].get("x-simulated-trading") == "1"
        # query 应该 sorted
        assert "instId=BTC-USDT" in captured["url"]

    async def test_private_request_has_signature(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(req.headers)
            return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"bal": "100"}]})

        async with _make_transport(httpx.MockTransport(handler)) as t:
            data = await t.request("GET", "/api/v5/account/balance", private=True)

        assert data == [{"bal": "100"}]
        assert captured["headers"].get("ok-access-key") == "key"
        assert captured["headers"].get("ok-access-passphrase") == "pass"
        assert captured["headers"].get("ok-access-sign")
        assert captured["headers"].get("ok-access-timestamp")

    async def test_post_with_json_body(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = req.content.decode()
            return httpx.Response(200, json={"code": "0", "data": [{"ordId": "123"}]})

        async with _make_transport(httpx.MockTransport(handler)) as t:
            data = await t.request(
                "POST",
                "/api/v5/trade/order",
                json_body={"instId": "BTC-USDT-SWAP", "sz": "1"},
                private=True,
            )

        assert data == [{"ordId": "123"}]
        body = json.loads(captured["body"])
        assert body == {"instId": "BTC-USDT-SWAP", "sz": "1"}


# ============================================================================
# 业务错误（不重试）
# ============================================================================

class TestBusinessErrors:
    async def test_business_error_not_retried(self) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"code": "51000", "msg": "param error", "data": []})

        async with _make_transport(httpx.MockTransport(handler)) as t:
            with pytest.raises(OKXAPIError) as excinfo:
                await t.request("POST", "/api/v5/trade/order", private=True)

        assert excinfo.value.code == "51000"
        assert call_count == 1, "business errors must not be retried"

    async def test_auth_error_subclass(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "50111", "msg": "Invalid sign", "data": []})

        async with _make_transport(httpx.MockTransport(handler)) as t:
            with pytest.raises(OKXAuthError):
                await t.request("GET", "/api/v5/account/balance", private=True)

    async def test_missing_credentials_for_private(self) -> None:
        # 无凭证时调私有接口应直接抛，不应该发请求
        def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应该被调用
            raise AssertionError("should not be called")

        settings = OKXSettings(_env_file=None, is_demo=True)  # 无凭证
        client = httpx.AsyncClient(base_url=settings.effective_rest_base_url, transport=httpx.MockTransport(handler))
        async with Transport(settings, rate_limiter=RateLimiter(), client=client) as t:
            with pytest.raises(OKXAuthError, match="not configured"):
                await t.request("GET", "/api/v5/account/balance", private=True)


# ============================================================================
# 限频与重试
# ============================================================================

class TestRetry:
    async def test_rate_limit_code_retried(self) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json={"code": "50011", "msg": "rate limit", "data": []})
            return httpx.Response(200, json={"code": "0", "msg": "", "data": [{"ok": True}]})

        # 把退避改短以加快测试（settings.max_retries 控制次数，但 backoff 是 2*2^attempt）
        # 这里默认 max_retries=2，backoff_attempt0=2.0s 太长——直接 patch
        async with _make_transport(httpx.MockTransport(handler), max_retries=3) as t:
            # 用 monkeypatch 把退避缩到 0
            import okx_trade.rest.transport as tx
            orig = tx._backoff_delay
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            try:
                data = await t.request("GET", "/api/v5/market/tickers")
            finally:
                tx._backoff_delay = orig

        assert call_count == 2
        assert data == [{"ok": True}]

    async def test_transient_50004_retried(self) -> None:
        """50004 (API endpoint request timeout) — OKX 网关瞬时不可用，
        重启时段 / 资费整点高频出现。必须重试，否则上层会误判永久错误。"""
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json={
                    "code": "50004", "msg": "API endpoint request timeout.", "data": [],
                })
            return httpx.Response(200, json={"code": "0", "data": [{"ok": True}]})

        async with _make_transport(httpx.MockTransport(handler), max_retries=3) as t:
            import okx_trade.rest.transport as tx
            orig = tx._backoff_delay
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            try:
                data = await t.request("POST", "/api/v5/account/set-leverage", private=True)
            finally:
                tx._backoff_delay = orig

        assert call_count == 2
        assert data == [{"ok": True}]

    async def test_transient_51290_retried(self) -> None:
        """51290 (Trading bot engine currently upgrading) — OKX 滚动升级窗口，
        几秒到几十秒恢复。重试覆盖窗口。"""
        from okx_trade.exceptions import OKXTransientError
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={
                "code": "51290",
                "msg": "Trading bot engine currently upgrading. Try again later.",
                "data": [],
            })

        async with _make_transport(
            httpx.MockTransport(handler),
            max_retries=2, max_transient_retries=2,
        ) as t:
            import okx_trade.rest.transport as tx
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            with pytest.raises(OKXTransientError):
                await t.request("POST", "/api/v5/account/set-leverage", private=True)

        # max_transient_retries=2 → 3 次调用（初始 + 2 次 retry）
        assert call_count == 3

    async def test_transient_uses_separate_budget_from_network(self) -> None:
        """OKXTransientError 用 max_transient_retries（默认 5），跟 max_retries
        分开计数。即使 max_retries=0（禁止网络重试），transient 仍能重试，
        因为它们是 OKX 服务端瞬时故障，跟我们的 burst 无关。"""
        from okx_trade.exceptions import OKXTransientError
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return httpx.Response(200, json={
                    "code": "51290", "msg": "engine upgrading", "data": [],
                })
            return httpx.Response(200, json={"code": "0", "data": [{"ok": True}]})

        # max_retries=0 (zero retries for network) but max_transient_retries=5
        # → 3 transient failures should still recover.
        async with _make_transport(
            httpx.MockTransport(handler),
            max_retries=0, max_transient_retries=5,
        ) as t:
            import okx_trade.rest.transport as tx
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            data = await t.request("POST", "/api/v5/account/set-leverage", private=True)

        assert call_count == 4  # 3 transient + 1 success
        assert data == [{"ok": True}]

    async def test_http_429_retried(self) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, headers={"Retry-After": "0.01"})
            return httpx.Response(200, json={"code": "0", "data": [{"ok": True}]})

        async with _make_transport(httpx.MockTransport(handler)) as t:
            import okx_trade.rest.transport as tx
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            data = await t.request("GET", "/api/v5/market/tickers")

        assert call_count == 2
        assert data == [{"ok": True}]

    async def test_http_500_retried(self) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return httpx.Response(500, text="server error")
            return httpx.Response(200, json={"code": "0", "data": [{"ok": True}]})

        async with _make_transport(httpx.MockTransport(handler), max_retries=3) as t:
            import okx_trade.rest.transport as tx
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            data = await t.request("GET", "/api/v5/market/tickers")

        assert call_count == 3
        assert data == [{"ok": True}]

    async def test_4xx_not_retried(self) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(400, text="bad request")

        async with _make_transport(httpx.MockTransport(handler)) as t:
            with pytest.raises(OKXAPIError) as excinfo:
                await t.request("GET", "/api/v5/market/tickers")

        assert excinfo.value.code == "400"
        assert call_count == 1, "4xx (non-429) must not be retried"

    async def test_retries_exhausted(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="always fails")

        async with _make_transport(httpx.MockTransport(handler), max_retries=2) as t:
            import okx_trade.rest.transport as tx
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            with pytest.raises(OKXNetworkError):
                await t.request("GET", "/api/v5/market/tickers")

    async def test_timeout_retried(self) -> None:
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ReadTimeout("simulated timeout")
            return httpx.Response(200, json={"code": "0", "data": [{"ok": True}]})

        async with _make_transport(httpx.MockTransport(handler)) as t:
            import okx_trade.rest.transport as tx
            tx._backoff_delay = lambda attempt, exc: 0.0  # type: ignore[assignment]
            data = await t.request("GET", "/api/v5/market/tickers")

        assert call_count == 2
        assert data == [{"ok": True}]


# ============================================================================
# 限频集成
# ============================================================================

class TestRateLimitIntegration:
    async def test_acquires_token_before_request(self) -> None:
        captured_groups: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": []})

        rl = RateLimiter()
        rl.register("trade.order", capacity=10, period=1.0)
        original_acquire = rl.acquire

        async def spy_acquire(group: str, n: int = 1) -> None:
            captured_groups.append(group)
            await original_acquire(group, n)

        rl.acquire = spy_acquire  # type: ignore[method-assign]

        settings = _make_settings()
        client = httpx.AsyncClient(base_url=settings.effective_rest_base_url, transport=httpx.MockTransport(handler))
        async with Transport(settings, rate_limiter=rl, client=client) as t:
            await t.request("POST", "/api/v5/trade/order", private=True, group="trade.order")

        assert captured_groups == ["trade.order"]
