"""WSConnection 测试：用本地 websockets server 做 FakeWSServer。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve

from okx_trade.exceptions import OKXWSConnectionError
from okx_trade.ws.connection import WSConnection
from okx_trade.ws.subscription import SubscriptionKey

# ---------------------------------------------------------------------------
# Fake server helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def fake_ws_server(
    handler: Callable[[ServerConnection], Any],
) -> AsyncIterator[str]:
    """启动本地 ws server，yield 出 ws://host:port/ url。"""
    server = await serve(handler, "127.0.0.1", 0)
    sock = next(iter(server.sockets))
    port = sock.getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _drain_on_message(_: dict[str, Any]) -> None:
    """default no-op handler."""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBasicConnect:
    async def test_connect_and_receive(self) -> None:
        received: list[dict[str, Any]] = []

        async def on_msg(frame: dict[str, Any]) -> None:
            received.append(frame)

        async def server_handler(ws: ServerConnection) -> None:
            await ws.send(json.dumps({"arg": {"channel": "tickers", "instId": "X"}, "data": [{"x": 1}]}))
            # 保持连接打开一会儿
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, on_msg)
            await conn.start()
            try:
                await conn.wait_connected(timeout=2.0)
                # 给 server 发送一点时间
                for _ in range(20):
                    if received:
                        break
                    await asyncio.sleep(0.05)
                assert len(received) == 1
                assert received[0]["data"] == [{"x": 1}]
            finally:
                await conn.stop()

    async def test_wait_connected_timeout(self) -> None:
        # 连一个不存在的端口
        conn = WSConnection("ws://127.0.0.1:1", _drain_on_message)
        await conn.start()
        try:
            with pytest.raises(OKXWSConnectionError):
                await conn.wait_connected(timeout=0.5)
        finally:
            await conn.stop()


class TestLogin:
    async def test_login_frame_sent_and_acked(self) -> None:
        login_received: list[dict[str, Any]] = []

        async def server_handler(ws: ServerConnection) -> None:
            raw = await ws.recv()
            frame = json.loads(raw)
            login_received.append(frame)
            # 模拟 OKX login 成功响应
            await ws.send(json.dumps({"event": "login", "code": "0"}))
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(
                url,
                _drain_on_message,
                login_creds=("ak", "sk", "pp"),
            )
            await conn.start()
            try:
                await conn.wait_connected(timeout=2.0)
                assert len(login_received) == 1
                assert login_received[0]["op"] == "login"
                args = login_received[0]["args"][0]
                assert args["apiKey"] == "ak"
                assert args["passphrase"] == "pp"
                assert "sign" in args and "timestamp" in args
            finally:
                await conn.stop()

    async def test_login_failure_triggers_reconnect(self) -> None:
        attempt_count = 0

        async def server_handler(ws: ServerConnection) -> None:
            nonlocal attempt_count
            attempt_count += 1
            await ws.recv()
            # 拒绝 login
            await ws.send(json.dumps({"event": "login", "code": "60008", "msg": "bad creds"}))
            await ws.close()

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, _drain_on_message, login_creds=("ak", "sk", "pp"))
            await conn.start()
            try:
                # 等让它尝试至少 2 次（含一次 backoff）
                await asyncio.sleep(1.5)
                assert attempt_count >= 2
            finally:
                await conn.stop()


class TestSubscribe:
    async def test_subscribe_sends_frame(self) -> None:
        received: list[dict[str, Any]] = []

        async def server_handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    if raw == "ping":
                        await ws.send("pong")
                        continue
                    received.append(json.loads(raw))
            except websockets.ConnectionClosed:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, _drain_on_message)
            await conn.start()
            try:
                await conn.wait_connected(timeout=2.0)
                k = SubscriptionKey.make("tickers", instId="BTC-USDT")
                await conn.subscribe([k])
                # 等 server 收到
                for _ in range(20):
                    if received:
                        break
                    await asyncio.sleep(0.05)
                subs = [f for f in received if f.get("op") == "subscribe"]
                assert len(subs) == 1
                assert subs[0]["args"][0]["channel"] == "tickers"
            finally:
                await conn.stop()

    async def test_subscribe_idempotent(self) -> None:
        received: list[dict[str, Any]] = []

        async def server_handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    if raw == "ping":
                        continue
                    received.append(json.loads(raw))
            except websockets.ConnectionClosed:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, _drain_on_message)
            await conn.start()
            try:
                await conn.wait_connected(timeout=2.0)
                k = SubscriptionKey.make("tickers", instId="X")
                await conn.subscribe([k])
                await conn.subscribe([k])  # 第二次不应再发
                await conn.subscribe([k])
                await asyncio.sleep(0.2)
                subs = [f for f in received if f.get("op") == "subscribe"]
                assert len(subs) == 1
                assert conn.registry.count(k) == 3
            finally:
                await conn.stop()

    async def test_unsubscribe_only_when_refcount_zero(self) -> None:
        received: list[dict[str, Any]] = []

        async def server_handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    if raw == "ping":
                        continue
                    received.append(json.loads(raw))
            except websockets.ConnectionClosed:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, _drain_on_message)
            await conn.start()
            try:
                await conn.wait_connected(timeout=2.0)
                k = SubscriptionKey.make("tickers", instId="Y")
                await conn.subscribe([k])
                await conn.subscribe([k])
                await conn.unsubscribe([k])  # 还有引用，不发
                await asyncio.sleep(0.1)
                unsubs = [f for f in received if f.get("op") == "unsubscribe"]
                assert len(unsubs) == 0
                await conn.unsubscribe([k])  # 归零，发
                await asyncio.sleep(0.2)
                unsubs = [f for f in received if f.get("op") == "unsubscribe"]
                assert len(unsubs) == 1
            finally:
                await conn.stop()


class TestReconnect:
    async def test_resubscribe_on_reconnect(self) -> None:
        # 思路：第一次 connect 时让 server 立刻关闭；第二次记录订阅帧
        attempts: list[list[dict[str, Any]]] = []

        async def server_handler(ws: ServerConnection) -> None:
            this_attempt: list[dict[str, Any]] = []
            attempts.append(this_attempt)
            n = len(attempts)
            try:
                if n == 1:
                    # 第一次：等接收到一个非 ping 的帧（要么 subscribe）后立刻断
                    async for raw in ws:
                        if raw == "ping":
                            continue
                        this_attempt.append(json.loads(raw))
                        break
                    await ws.close()
                    return
                # 第二次及之后：保持开
                async for raw in ws:
                    if raw == "ping":
                        continue
                    this_attempt.append(json.loads(raw))
            except websockets.ConnectionClosed:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, _drain_on_message)
            await conn.start()
            try:
                await conn.wait_connected(timeout=2.0)
                k = SubscriptionKey.make("tickers", instId="Z")
                await conn.subscribe([k])
                # 等首次断 + 重连
                for _ in range(40):
                    if len(attempts) >= 2 and attempts[1]:
                        break
                    await asyncio.sleep(0.1)
                assert len(attempts) >= 2
                # 第二次连接收到的第一帧应是 subscribe
                assert any(f.get("op") == "subscribe" for f in attempts[1])
            finally:
                await conn.stop()


class TestHeartbeat:
    async def test_ping_text_frame_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 把心跳间隔降到 0.1s 让测试快速跑完
        from okx_trade.ws import connection as conn_mod

        monkeypatch.setattr(conn_mod, "_PING_INTERVAL_SEC", 0.1)

        received: list[Any] = []

        async def server_handler(ws: ServerConnection) -> None:
            try:
                async for raw in ws:
                    received.append(raw)
                    if raw == "ping":
                        await ws.send("pong")
            except websockets.ConnectionClosed:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, _drain_on_message)
            await conn.start()
            try:
                await conn.wait_connected(timeout=2.0)
                # 等几个 ping cycle
                await asyncio.sleep(0.4)
                pings = [r for r in received if r == "ping"]
                assert len(pings) >= 2
            finally:
                await conn.stop()


class TestStop:
    async def test_stop_is_idempotent(self) -> None:
        async def server_handler(ws: ServerConnection) -> None:
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                return

        async with fake_ws_server(server_handler) as url:
            conn = WSConnection(url, _drain_on_message)
            await conn.start()
            await conn.wait_connected(timeout=2.0)
            await conn.stop()
            await conn.stop()  # 不应抛
            assert conn.is_connected is False
