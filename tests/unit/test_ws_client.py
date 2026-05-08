"""OKXWSClient 测试：路由 + 三连接懒启动 + 高层 API。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from okx_trade.config import OKXSettings
from okx_trade.exceptions import OKXWSConnectionError
from okx_trade.ws.client import OKXWSClient, resolve_endpoint

# ---------------------------------------------------------------------------
# Fake server
# ---------------------------------------------------------------------------


@asynccontextmanager
async def fake_ws(handler: Callable[[ServerConnection], Any]) -> AsyncIterator[str]:
    server = await serve(handler, "127.0.0.1", 0)
    sock = next(iter(server.sockets))
    port = sock.getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


def _settings(public_url: str = "", private_url: str = "", business_url: str = "",
              with_creds: bool = False) -> OKXSettings:
    """构造测试用 settings。"""
    s = OKXSettings(
        api_key="ak" if with_creds else "",      # type: ignore[arg-type]
        secret_key="sk" if with_creds else "",   # type: ignore[arg-type]
        passphrase="pp" if with_creds else "",   # type: ignore[arg-type]
        is_demo=True,
        ws_public_url=public_url or None,
        ws_private_url=private_url or None,
        ws_business_url=business_url or None,
        request_timeout_sec=2.0,
    )
    return s


# ---------------------------------------------------------------------------
# Endpoint routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_public_default(self) -> None:
        assert resolve_endpoint("tickers") == "public"
        assert resolve_endpoint("books5") == "public"
        assert resolve_endpoint("trades") == "public"
        assert resolve_endpoint("instruments") == "public"

    def test_private_channels(self) -> None:
        assert resolve_endpoint("account") == "private"
        assert resolve_endpoint("positions") == "private"
        assert resolve_endpoint("orders") == "private"
        assert resolve_endpoint("orders-algo") == "private"

    def test_business_candle(self) -> None:
        assert resolve_endpoint("candle1m") == "business"
        assert resolve_endpoint("candle1H") == "business"
        assert resolve_endpoint("mark-price-candle5m") == "business"
        assert resolve_endpoint("index-candle1D") == "business"

    def test_unknown_falls_back_public(self) -> None:
        assert resolve_endpoint("some-future-channel") == "public"


# ---------------------------------------------------------------------------
# High-level subscribe (public, no creds)
# ---------------------------------------------------------------------------


class TestSubscribePublic:
    async def test_subscribe_yields_events(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    if raw == "ping":
                        continue
                    frame = json.loads(raw)
                    if frame.get("op") == "subscribe":
                        arg = frame["args"][0]
                        # 推一条匹配的数据
                        await ws.send(json.dumps({"arg": arg, "data": [{"px": "100"}]}))
                        await ws.send(json.dumps({"arg": arg, "data": [{"px": "101"}]}))
            except Exception:
                return

        async with fake_ws(handler) as url:
            settings = _settings(public_url=url)
            async with OKXWSClient(settings) as ws:
                seen: list[dict[str, Any]] = []
                async with ws.subscribe("tickers", instId="BTC-USDT") as stream:
                    async for event in stream:
                        seen.append(event)
                        if len(seen) == 2:
                            break
                assert seen[0]["data"] == [{"px": "100"}]
                assert seen[1]["data"] == [{"px": "101"}]

    async def test_lazy_start_only_when_subscribed(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return

        async with fake_ws(handler) as url:
            settings = _settings(public_url=url)
            async with OKXWSClient(settings) as ws:
                # 还没 subscribe 时不应有任何连接
                assert ws._connections == {}  # type: ignore[reportPrivateUsage]
                async with ws.subscribe("tickers", instId="X"):
                    assert "public" in ws._connections  # type: ignore[reportPrivateUsage]
                    assert "private" not in ws._connections  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Private subscription requires credentials
# ---------------------------------------------------------------------------


class TestPrivateRequiresCreds:
    async def test_missing_creds_raises(self) -> None:
        # 不需要真起 server——有问题在凭证检查阶段就抛
        settings = _settings(with_creds=False)
        async with OKXWSClient(settings) as ws:
            with pytest.raises(OKXWSConnectionError, match="credentials"):
                async with ws.subscribe("account"):
                    pass

    async def test_private_login_then_subscribe(self) -> None:
        login_seen: list[dict[str, Any]] = []
        sub_seen: list[dict[str, Any]] = []

        async def handler(ws: ServerConnection) -> None:
            try:
                # 第一个帧应当是 login
                raw = await ws.recv()
                frame = json.loads(raw)
                login_seen.append(frame)
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "subscribe":
                        sub_seen.append(f)
                        await ws.send(json.dumps({"arg": f["args"][0], "data": [{"a": 1}]}))
            except Exception:
                return

        async with fake_ws(handler) as url:
            settings = _settings(private_url=url, with_creds=True)
            async with OKXWSClient(settings) as ws:
                async with ws.subscribe("account") as stream:
                    event = await asyncio.wait_for(anext(stream), timeout=2.0)
                    assert event["data"] == [{"a": 1}]
                assert len(login_seen) == 1
                assert login_seen[0]["op"] == "login"
                assert len(sub_seen) == 1


# ---------------------------------------------------------------------------
# Cleanup on exit
# ---------------------------------------------------------------------------


class TestCleanup:
    async def test_close_stops_all(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            try:
                async for _ in ws:
                    pass
            except Exception:
                return

        async with fake_ws(handler) as url:
            settings = _settings(public_url=url)
            ws = OKXWSClient(settings)
            async with ws.subscribe("tickers", instId="X"):
                pass
            await ws.close()
            assert ws._connections == {}  # type: ignore[reportPrivateUsage]

    async def test_unsubscribe_after_iter_break(self) -> None:
        sub_seen: list[dict[str, Any]] = []
        unsub_seen: list[dict[str, Any]] = []

        async def handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "subscribe":
                        sub_seen.append(f)
                        arg = f["args"][0]
                        for _ in range(5):
                            await ws.send(json.dumps({"arg": arg, "data": [{"x": 1}]}))
                            await asyncio.sleep(0.02)
                    elif f.get("op") == "unsubscribe":
                        unsub_seen.append(f)
            except Exception:
                return

        async with fake_ws(handler) as url:
            settings = _settings(public_url=url)
            async with OKXWSClient(settings) as ws:
                async with ws.subscribe("tickers", instId="X") as stream:
                    async for _ in stream:
                        break
                # 给 server 时间收 unsubscribe
                for _ in range(20):
                    if unsub_seen:
                        break
                    await asyncio.sleep(0.05)
                assert len(sub_seen) == 1
                assert len(unsub_seen) == 1
