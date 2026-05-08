"""WS trade（下单/撤单/改单）测试。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from okx_trade.config import OKXSettings
from okx_trade.enums import OrdType, PosSide, Side, TdMode
from okx_trade.exceptions import OKXAPIError, OKXWSError
from okx_trade.models.trade import OrderRequest
from okx_trade.ws.client import OKXWSClient


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


def _settings(private_url: str) -> OKXSettings:
    return OKXSettings(
        api_key="ak",          # type: ignore[arg-type]
        secret_key="sk",       # type: ignore[arg-type]
        passphrase="pp",       # type: ignore[arg-type]
        is_demo=True,
        ws_private_url=private_url,
        request_timeout_sec=2.0,
    )


def _make_req() -> OrderRequest:
    return OrderRequest(
        instId="BTC-USDT-SWAP",       # type: ignore[call-arg]
        tdMode=TdMode.CROSS,           # type: ignore[call-arg]
        side=Side.BUY,
        ordType=OrdType.LIMIT,         # type: ignore[call-arg]
        sz="1",
        px="20000",
        posSide=PosSide.LONG,          # type: ignore[call-arg]
    )


# ---------------------------------------------------------------------------


class TestPlaceOrder:
    async def test_place_order_success(self) -> None:
        request_seen: list[dict[str, Any]] = []

        async def handler(ws: ServerConnection) -> None:
            try:
                # login
                await ws.recv()
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "order":
                        request_seen.append(f)
                        await ws.send(json.dumps({
                            "id": f["id"], "op": "order", "code": "0", "msg": "",
                            "data": [{"sCode": "0", "ordId": "OID-1", "clOrdId": ""}],
                        }))
            except Exception:
                return

        async with fake_ws(handler) as url:
            async with OKXWSClient(_settings(url)) as ws:
                res = await ws.trade.place_order(_make_req())
                assert res.ok
                assert res.ord_id == "OID-1"
                assert len(request_seen) == 1
                arg = request_seen[0]["args"][0]
                assert arg["instId"] == "BTC-USDT-SWAP"
                assert arg["px"] == "20000"
                assert "id" in request_seen[0] and len(request_seen[0]["id"]) > 0

    async def test_place_order_business_error_raises(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            try:
                await ws.recv()
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "order":
                        # 单笔失败：sCode != "0"
                        await ws.send(json.dumps({
                            "id": f["id"], "op": "order", "code": "0", "msg": "",
                            "data": [{"sCode": "51000", "sMsg": "param err", "ordId": ""}],
                        }))
            except Exception:
                return

        async with fake_ws(handler) as url:
            async with OKXWSClient(_settings(url)) as ws:
                with pytest.raises(OKXAPIError) as ei:
                    await ws.trade.place_order(_make_req())
                assert ei.value.code == "51000"

    async def test_place_order_top_level_failure_raises(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            try:
                await ws.recv()
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "order":
                        # 顶层 code != "0"
                        await ws.send(json.dumps({
                            "id": f["id"], "op": "order",
                            "code": "60012", "msg": "invalid request", "data": [],
                        }))
            except Exception:
                return

        async with fake_ws(handler) as url:
            async with OKXWSClient(_settings(url)) as ws:
                with pytest.raises(OKXWSError, match="60012"):
                    await ws.trade.place_order(_make_req())

    async def test_timeout(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            try:
                await ws.recv()
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                # 收到请求不响应
                async for raw in ws:
                    if raw == "ping":
                        continue
            except Exception:
                return

        async with fake_ws(handler) as url:
            async with OKXWSClient(_settings(url)) as ws:
                with pytest.raises(OKXWSError, match="timeout"):
                    await ws.trade.place_order(_make_req(), timeout=0.3)


# ---------------------------------------------------------------------------


class TestCancelAmend:
    async def test_cancel_order_success(self) -> None:
        async def handler(ws: ServerConnection) -> None:
            try:
                await ws.recv()
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "cancel-order":
                        await ws.send(json.dumps({
                            "id": f["id"], "op": "cancel-order", "code": "0", "msg": "",
                            "data": [{"sCode": "0", "ordId": f["args"][0]["ordId"]}],
                        }))
            except Exception:
                return

        async with fake_ws(handler) as url:
            async with OKXWSClient(_settings(url)) as ws:
                res = await ws.trade.cancel_order("BTC-USDT-SWAP", ord_id="X")
                assert res.ord_id == "X"
                assert res.ok

    async def test_cancel_order_requires_id(self) -> None:
        # 不需要 server——参数校验在客户端就抛
        async with OKXWSClient(_settings("ws://127.0.0.1:1")) as ws:
            with pytest.raises(ValueError):
                await ws.trade.cancel_order("BTC-USDT-SWAP")

    async def test_amend_order_success(self) -> None:
        seen: list[dict[str, Any]] = []

        async def handler(ws: ServerConnection) -> None:
            try:
                await ws.recv()
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "amend-order":
                        seen.append(f)
                        await ws.send(json.dumps({
                            "id": f["id"], "op": "amend-order", "code": "0", "msg": "",
                            "data": [{"sCode": "0", "ordId": f["args"][0]["ordId"]}],
                        }))
            except Exception:
                return

        async with fake_ws(handler) as url:
            async with OKXWSClient(_settings(url)) as ws:
                res = await ws.trade.amend_order("BTC-USDT-SWAP", ord_id="X", new_px="21000")
                assert res.ok
                assert seen[0]["args"][0]["newPx"] == "21000"

    async def test_amend_order_requires_change(self) -> None:
        async with OKXWSClient(_settings("ws://127.0.0.1:1")) as ws:
            with pytest.raises(ValueError):
                await ws.trade.amend_order("BTC-USDT-SWAP", ord_id="X")


# ---------------------------------------------------------------------------


class TestConcurrent:
    async def test_two_concurrent_orders(self) -> None:
        """两个 in-flight 请求应能各自拿到自己的响应。"""

        async def handler(ws: ServerConnection) -> None:
            try:
                await ws.recv()
                await ws.send(json.dumps({"event": "login", "code": "0"}))
                async for raw in ws:
                    if raw == "ping":
                        continue
                    f = json.loads(raw)
                    if f.get("op") == "order":
                        # echo back ordId 以便区分
                        ord_id = "OID-" + f["args"][0]["px"]
                        await ws.send(json.dumps({
                            "id": f["id"], "op": "order", "code": "0", "msg": "",
                            "data": [{"sCode": "0", "ordId": ord_id}],
                        }))
            except Exception:
                return

        async with fake_ws(handler) as url:
            async with OKXWSClient(_settings(url)) as ws:
                req_a = _make_req()
                req_b = OrderRequest(
                    instId="BTC-USDT-SWAP",       # type: ignore[call-arg]
                    tdMode=TdMode.CROSS,           # type: ignore[call-arg]
                    side=Side.BUY,
                    ordType=OrdType.LIMIT,         # type: ignore[call-arg]
                    sz="1",
                    px="21000",
                    posSide=PosSide.LONG,          # type: ignore[call-arg]
                )
                res_a, res_b = await asyncio.gather(
                    ws.trade.place_order(req_a),
                    ws.trade.place_order(req_b),
                )
                assert res_a.ord_id == "OID-20000"
                assert res_b.ord_id == "OID-21000"
