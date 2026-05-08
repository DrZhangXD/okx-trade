"""REST 业务端点测试，用 httpx.MockTransport 注入响应。

不重复测 Transport 的签名/重试逻辑（已在 test_rest_transport.py 覆盖），只测：
- 端点 URL / params / body 拼装是否正确
- 响应解析为模型是否正确
- 业务级错误（如下单 sCode != "0"）是否被识别
"""
from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from okx_trade import (
    BarSize,
    InstType,
    OKXAPIError,
    OKXRestClient,
    OKXSettings,
    OrdType,
    PosSide,
    Side,
    TdMode,
)
from okx_trade.models.trade import OrderRequest
from okx_trade.rest.ratelimit import RateLimiter
from okx_trade.rest.transport import Transport


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OKXRestClient:
    settings = OKXSettings(
        _env_file=None,
        api_key="key", secret_key="secret", passphrase="pass",
        is_demo=True, max_retries=0,
    )
    http = httpx.AsyncClient(
        base_url=settings.effective_rest_base_url,
        transport=httpx.MockTransport(handler),
    )
    transport = Transport(settings, rate_limiter=RateLimiter(), client=http)
    return OKXRestClient(transport=transport)


# ============================================================================
# Public endpoints
# ============================================================================

class TestPublic:
    async def test_get_instrument(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"code": "0", "data": [{
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "ctVal": "0.01",
                "ctValCcy": "BTC",
                "tickSz": "0.1",
                "lotSz": "1",
                "minSz": "1",
                "state": "live",
            }]})

        async with _client(handler) as c:
            inst = await c.public.get_instrument(InstType.SWAP, "BTC-USDT-SWAP")

        assert inst.inst_id == "BTC-USDT-SWAP"
        assert inst.tick_sz == Decimal("0.1")
        assert inst.ct_val == Decimal("0.01")
        assert "instType=SWAP" in captured["url"]
        assert "instId=BTC-USDT-SWAP" in captured["url"]

    async def test_get_instrument_not_found(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": []})

        async with _client(handler) as c:
            with pytest.raises(OKXAPIError, match="not found"):
                await c.public.get_instrument(InstType.SWAP, "FAKE-USDT-SWAP")

    async def test_get_funding_rate(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"code": "0", "data": [{
                "instType": "SWAP",
                "instId": "BTC-USDT-SWAP",
                "fundingRate": "0.00012",
                "nextFundingRate": "0.00010",
                "fundingTime": "1700000000000",
                "nextFundingTime": "1700028800000",
            }]})

        async with _client(handler) as c:
            fr = await c.public.get_funding_rate("BTC-USDT-SWAP")

        assert fr.inst_id == "BTC-USDT-SWAP"
        assert fr.funding_rate == Decimal("0.00012")
        assert fr.next_funding_rate == Decimal("0.00010")
        assert fr.funding_time == 1700000000000
        # APR ≈ 0.00012 × 3 × 365 = 0.1314 ≈ 13.14%
        assert abs(float(fr.apr) - 0.1314) < 1e-6
        assert "instId=BTC-USDT-SWAP" in captured["url"]
        assert "/api/v5/public/funding-rate" in captured["url"]

    async def test_get_funding_rate_empty_raises(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": []})

        async with _client(handler) as c:
            with pytest.raises(OKXAPIError, match="no funding rate"):
                await c.public.get_funding_rate("FAKE-USDT-SWAP")


# ============================================================================
# Market endpoints
# ============================================================================

class TestMarket:
    async def test_get_candles_parses_array(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"code": "0", "data": [
                # OKX 倒序，方法应自动正序
                ["1700000060000", "20100", "20200", "20050", "20150", "10", "200000", "201000", "1"],
                ["1700000000000", "20000", "20100", "19950", "20100", "12", "240000", "241000", "1"],
            ]})

        async with _client(handler) as c:
            candles = await c.market.get_candles("BTC-USDT-SWAP", BarSize.M1, limit=2)

        assert len(candles) == 2
        # 已正序
        assert candles[0].ts < candles[1].ts
        assert candles[0].open == Decimal("20000")
        assert candles[0].close == Decimal("20100")
        assert candles[0].confirm is True
        # URL 校验
        assert "bar=1m" in captured["url"]
        assert "limit=2" in captured["url"]

    async def test_get_ticker(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": [{
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "last": "20123.5",
                "lastSz": "1",
                "askPx": "20123.6",
                "askSz": "5",
                "bidPx": "20123.4",
                "bidSz": "3",
                "open24h": "20000",
                "high24h": "20200",
                "low24h": "19900",
                "vol24h": "12345",
                "volCcy24h": "247000000",
                "ts": "1700000000000",
            }]})

        async with _client(handler) as c:
            t = await c.market.get_ticker("BTC-USDT-SWAP")

        assert t.last == Decimal("20123.5")
        assert t.bid_px == Decimal("20123.4")
        assert t.ts == 1700000000000

    async def test_get_books(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": [{
                "asks": [["20100", "1", "0", "1"], ["20101", "2", "0", "2"]],
                "bids": [["20099", "3", "0", "1"], ["20098", "4", "0", "2"]],
                "ts": "1700000000000",
            }]})

        async with _client(handler) as c:
            book = await c.market.get_books("BTC-USDT-SWAP", sz=2)

        assert len(book.asks) == 2
        assert len(book.bids) == 2
        assert book.asks[0].price == Decimal("20100")
        assert book.bids[0].size == Decimal("3")


# ============================================================================
# Account endpoints
# ============================================================================

class TestAccount:
    async def test_get_balance_with_details(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(req.headers)
            return httpx.Response(200, json={"code": "0", "data": [{
                "totalEq": "1000",
                "details": [
                    {"ccy": "USDT", "eq": "950", "cashBal": "950", "availEq": "900"},
                    {"ccy": "BTC", "eq": "0.01", "cashBal": "0.01"},
                ],
            }]})

        async with _client(handler) as c:
            bal = await c.account.get_balance("USDT")

        assert bal.total_eq == Decimal("1000")
        usdt = bal.get("USDT")
        assert usdt is not None and usdt.eq == Decimal("950")
        # 私有接口必须带签名 header
        assert captured["headers"].get("ok-access-sign")

    async def test_get_open_position_returns_none_when_no_pos(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": [{
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "mgnMode": "cross",
                "posSide": "long",
                "pos": "0",
            }]})

        async with _client(handler) as c:
            pos = await c.account.get_open_position("BTC-USDT-SWAP")
        assert pos is None

    async def test_get_open_position_returns_position(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": [{
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "mgnMode": "cross",
                "posSide": "long",
                "pos": "5",
                "avgPx": "20000",
            }]})

        async with _client(handler) as c:
            pos = await c.account.get_open_position("BTC-USDT-SWAP")

        assert pos is not None
        assert pos.is_open is True
        assert pos.pos == Decimal("5")

    async def test_set_leverage_post(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content) if req.content else None
            captured["method"] = req.method
            return httpx.Response(200, json={"code": "0", "data": [{}]})

        async with _client(handler) as c:
            await c.account.set_leverage("BTC-USDT-SWAP", 5, TdMode.CROSS)

        assert captured["method"] == "POST"
        assert captured["body"] == {
            "instId": "BTC-USDT-SWAP", "lever": "5", "mgnMode": "cross",
        }

    async def test_set_leverage_isolated_includes_pos_side(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, json={"code": "0", "data": [{}]})

        async with _client(handler) as c:
            await c.account.set_leverage(
                "BTC-USDT-SWAP", 10, TdMode.ISOLATED, pos_side=PosSide.LONG,
            )

        assert captured["body"]["posSide"] == "long"


# ============================================================================
# Trade endpoints
# ============================================================================

class TestTrade:
    async def test_place_order_serializes_correctly(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, json={"code": "0", "data": [{
                "ordId": "111", "clOrdId": "", "tag": "", "sCode": "0", "sMsg": "",
            }]})

        req = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            td_mode=TdMode.CROSS,
            side=Side.BUY,
            pos_side=PosSide.LONG,
            ord_type=OrdType.MARKET,
            sz="1",
        )
        async with _client(handler) as c:
            res = await c.trade.place_order(req)

        assert res.ord_id == "111"
        assert res.ok is True
        body = captured["body"]
        # 必须用 OKX camelCase
        assert body == {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "cross",
            "side": "buy",
            "posSide": "long",
            "ordType": "market",
            "sz": "1",
        }

    async def test_place_order_inner_failure_raises(self) -> None:
        # 外层 code=0 但 sCode != 0：单笔失败必须抛
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": [{
                "ordId": "", "sCode": "51008", "sMsg": "insufficient balance",
            }]})

        req = OrderRequest(
            inst_id="BTC-USDT-SWAP", td_mode=TdMode.CROSS, side=Side.BUY,
            ord_type=OrdType.MARKET, sz="1",
        )
        async with _client(handler) as c:
            with pytest.raises(OKXAPIError) as excinfo:
                await c.trade.place_order(req)
        assert excinfo.value.code == "51008"

    async def test_place_market_with_tp_sl(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            return httpx.Response(200, json={"code": "0", "data": [{
                "ordId": "999", "sCode": "0", "sMsg": "",
            }]})

        async with _client(handler) as c:
            res = await c.trade.place_market_with_tp_sl(
                "BTC-USDT-SWAP",
                side=Side.BUY,
                pos_side=PosSide.LONG,
                size_contracts="3",
                tp_price="20500.07",   # 应对齐 tick=0.1 → 20500.1
                sl_price="19500.04",   # 应对齐 tick=0.1 → 19500.0
                tick_sz="0.1",
                lot_sz="1",
            )

        assert res.ord_id == "999"
        body = captured["body"]
        assert body["instId"] == "BTC-USDT-SWAP"
        assert body["ordType"] == "market"
        assert body["sz"] == "3"
        # attachAlgoOrds
        attach = body["attachAlgoOrds"][0]
        # fmt_price 按 tick_sz=0.1 取 1 位小数
        assert attach["tpTriggerPx"] == "20500.1"
        assert attach["slTriggerPx"] == "19500.0"
        assert attach["tpOrdPx"] == "-1"

    async def test_cancel_order_requires_id(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not be called")

        async with _client(handler) as c:
            with pytest.raises(ValueError, match="ord_id or cl_ord_id"):
                await c.trade.cancel_order("BTC-USDT-SWAP")

    async def test_cancel_algo_orders_no_pending_returns_zero(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            # 第一次：列出挂着的算法单 → 空
            return httpx.Response(200, json={"code": "0", "data": []})

        async with _client(handler) as c:
            n = await c.trade.cancel_algo_orders("BTC-USDT-SWAP")
        assert n == 0
