"""``OKXLiveExecutionClient`` reconciliation 报告生成器单测。

覆盖三类东西：

- ``parsing.py`` 新增的纯翻译函数（``okx_state_to_order_status`` 等）；
- ``rest/trade.py`` 新增的查询端点（mock httpx）；
- ``OKXLiveExecutionClient.generate_*_reports``（mock REST + 真 NT cache，
  验证 OKX dict → NT report 对象的字段对应）。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pandas as pd
import pytest
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.cache.cache import Cache
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
)
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    TraderId,
    VenueOrderId,
)

from okx_trade import OKXRestClient, OKXSettings
from okx_trade.adapter.constants import OKX_CLIENT_ID, OKX_VENUE
from okx_trade.adapter.execution import OKXLiveExecutionClient
from okx_trade.adapter.parsing import (
    okx_exec_type_to_liquidity,
    okx_ord_type_to_nt,
    okx_pos_to_position_side,
    okx_state_to_order_status,
    to_instrument_id,
)
from okx_trade.enums import TdMode
from okx_trade.models.common import Instrument as OKXInstrument
from okx_trade.adapter.parsing import parse_okx_instrument
from okx_trade.rest.ratelimit import RateLimiter
from okx_trade.rest.transport import Transport


# ---------------------------------------------------------------------------
# 纯函数 parsing 测试
# ---------------------------------------------------------------------------


class TestOkxStateToOrderStatus:
    @pytest.mark.parametrize("okx,expected", [
        ("live", OrderStatus.ACCEPTED),
        ("partially_filled", OrderStatus.PARTIALLY_FILLED),
        ("filled", OrderStatus.FILLED),
        ("canceled", OrderStatus.CANCELED),
        ("mmp_canceled", OrderStatus.CANCELED),
    ])
    def test_known_states(self, okx: str, expected: OrderStatus) -> None:
        assert okx_state_to_order_status(okx) == expected

    def test_unknown_state_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported OKX order state"):
            okx_state_to_order_status("totally_made_up")


class TestOkxOrdTypeToNt:
    @pytest.mark.parametrize("okx,ot,tif,po", [
        ("market", OrderType.MARKET, TimeInForce.IOC, False),
        ("limit", OrderType.LIMIT, TimeInForce.GTC, False),
        ("post_only", OrderType.LIMIT, TimeInForce.GTC, True),
        ("fok", OrderType.LIMIT, TimeInForce.FOK, False),
        ("ioc", OrderType.LIMIT, TimeInForce.IOC, False),
        ("optimal_limit_ioc", OrderType.LIMIT, TimeInForce.IOC, False),
    ])
    def test_known_types(self, okx: str, ot: OrderType, tif: TimeInForce, po: bool) -> None:
        assert okx_ord_type_to_nt(okx) == (ot, tif, po)

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported OKX ordType"):
            okx_ord_type_to_nt("trigger")


class TestOkxPosToPositionSide:
    @pytest.mark.parametrize("pos,pos_side,expected", [
        # net 模式：靠正负号
        (Decimal("0"), "net", PositionSide.FLAT),
        (Decimal("5"), "net", PositionSide.LONG),
        (Decimal("-3"), "net", PositionSide.SHORT),
        # long_short 模式：posSide 字段说了算
        (Decimal("5"), "long", PositionSide.LONG),
        (Decimal("3"), "short", PositionSide.SHORT),
        # pos==0 永远 FLAT，无视 pos_side
        (Decimal("0"), "long", PositionSide.FLAT),
    ])
    def test_routing(self, pos: Decimal, pos_side: str, expected: PositionSide) -> None:
        assert okx_pos_to_position_side(pos, pos_side) == expected


class TestOkxExecTypeToLiquidity:
    def test_maker(self) -> None:
        assert okx_exec_type_to_liquidity("M") == LiquiditySide.MAKER

    def test_taker(self) -> None:
        assert okx_exec_type_to_liquidity("T") == LiquiditySide.TAKER

    def test_unknown_no_side(self) -> None:
        assert okx_exec_type_to_liquidity("") == LiquiditySide.NO_LIQUIDITY_SIDE


# ---------------------------------------------------------------------------
# REST 端点测试（trade.get_pending_orders / get_orders_history / get_order / get_fills）
# ---------------------------------------------------------------------------


def _rest_client(handler: Callable[[httpx.Request], httpx.Response]) -> OKXRestClient:
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


class TestTradeReportEndpoints:
    async def test_get_pending_orders_passes_filters(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"code": "0", "data": [
                {"instId": "BTC-USDT-SWAP", "ordId": "1"},
            ]})

        async with _rest_client(handler) as c:
            rows = await c.trade.get_pending_orders(inst_type="SWAP", inst_id="BTC-USDT-SWAP")

        assert rows == [{"instId": "BTC-USDT-SWAP", "ordId": "1"}]
        assert "instType=SWAP" in captured["url"]
        assert "instId=BTC-USDT-SWAP" in captured["url"]
        assert "/orders-pending" in captured["url"]

    async def test_get_orders_history_passes_begin_end(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"code": "0", "data": []})

        async with _rest_client(handler) as c:
            await c.trade.get_orders_history(
                "SWAP", begin_ms=1_700_000_000_000, end_ms=1_700_000_999_999,
            )

        assert "instType=SWAP" in captured["url"]
        assert "begin=1700000000000" in captured["url"]
        assert "end=1700000999999" in captured["url"]
        assert "/orders-history-archive" in captured["url"]

    async def test_get_order_requires_id(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not be called")

        async with _rest_client(handler) as c:
            with pytest.raises(ValueError):
                await c.trade.get_order("BTC-USDT-SWAP")

    async def test_get_order_returns_none_when_empty(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "0", "data": []})

        async with _rest_client(handler) as c:
            res = await c.trade.get_order("BTC-USDT-SWAP", ord_id="999")
        assert res is None

    async def test_get_fills_with_inst_id(self) -> None:
        captured: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"code": "0", "data": [
                {"instId": "BTC-USDT-SWAP", "tradeId": "T1", "ordId": "O1"},
            ]})

        async with _rest_client(handler) as c:
            rows = await c.trade.get_fills(inst_type="SWAP", inst_id="BTC-USDT-SWAP")

        assert rows[0]["tradeId"] == "T1"
        assert "instType=SWAP" in captured["url"]
        assert "/fills" in captured["url"]


# ---------------------------------------------------------------------------
# OKXLiveExecutionClient.generate_* 端到端测试
# ---------------------------------------------------------------------------


def _make_okx_swap_inst(inst_id: str = "BTC-USDT-SWAP") -> OKXInstrument:
    return OKXInstrument.model_validate({
        "instId": inst_id, "instType": "SWAP",
        "baseCcy": "BTC", "quoteCcy": "USDT", "settleCcy": "USDT",
        "ctVal": "0.01", "ctValCcy": "BTC",
        "tickSz": "0.1", "lotSz": "1", "minSz": "1", "state": "live",
    })


def _build_exec_client_with_mocks(monkeypatch: pytest.MonkeyPatch) -> OKXLiveExecutionClient:
    """构造一个挂着 mock REST 的 OKXLiveExecutionClient。

    - 真用 NT 的 Cache / MessageBus / LiveClock；
    - REST 是 ``MagicMock`` 链：``client._rest.trade.get_pending_orders = AsyncMock(...)``；
    - InstrumentProvider 用 ``MagicMock``（generator 不直接调用它）；
    - 通过 ``_set_account_id`` 提前绑定 account（reconciliation 需要 self.account_id）。
    """
    from okx_trade.adapter.instrument_provider import OKXInstrumentProvider

    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = Cache(database=None)

    # provider 不被 generator 用到（_build_*_report 只查 cache）；占位即可
    provider = MagicMock(spec=OKXInstrumentProvider)

    settings = OKXSettings(
        _env_file=None,
        api_key="key", secret_key="secret", passphrase="pass",
        is_demo=True, max_retries=0,
    )
    loop = asyncio.new_event_loop()

    client = OKXLiveExecutionClient(
        loop=loop,
        client_id=OKX_CLIENT_ID,
        venue=OKX_VENUE,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        settings=settings,
        td_mode=TdMode.CROSS,
        pos_side_mode="net",
    )
    # account_id 手动绑：reconciliation 不会真去 _connect。
    client._set_account_id(AccountId(f"{OKX_CLIENT_ID.value}-master"))

    # cache 里塞 instrument，generator 才能拿到 precision
    inst = parse_okx_instrument(_make_okx_swap_inst(), ts_init=clock.timestamp_ns())
    cache.add_instrument(inst)

    # mock REST：每个端点都给 AsyncMock，测试用例按需 monkeypatch return_value
    rest = MagicMock()
    rest.trade = MagicMock()
    rest.trade.get_pending_orders = AsyncMock(return_value=[])
    rest.trade.get_orders_history = AsyncMock(return_value=[])
    rest.trade.get_order = AsyncMock(return_value=None)
    rest.trade.get_fills = AsyncMock(return_value=[])
    rest.account = MagicMock()
    rest.account.get_positions = AsyncMock(return_value=[])
    client._rest = rest
    return client


def _ts_init() -> int:
    return 1_700_000_000_000_000_000


def _now() -> pd.Timestamp:
    return pd.Timestamp("2026-05-08T00:00:00Z")


class TestGenerateOrderStatusReport:
    async def test_returns_none_when_rest_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _build_exec_client_with_mocks(monkeypatch)
        client._rest.trade.get_order.return_value = None

        cmd = GenerateOrderStatusReport(
            instrument_id=to_instrument_id("BTC-USDT-SWAP"),
            client_order_id=ClientOrderId("CL1"),
            venue_order_id=None,
            command_id=UUID4(),
            ts_init=_ts_init(),
        )
        report = await client.generate_order_status_report(cmd)
        assert report is None

    async def test_translates_pending_order_dict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _build_exec_client_with_mocks(monkeypatch)
        client._rest.trade.get_order.return_value = {
            "instId": "BTC-USDT-SWAP",
            "ordId": "777",
            "clOrdId": "CL777",
            "side": "buy",
            "ordType": "limit",
            "state": "live",
            "sz": "5",
            "accFillSz": "0",
            "px": "30000.5",
            "avgPx": "",
            "cTime": "1714000000000",
            "uTime": "1714000010000",
            "reduceOnly": "false",
        }

        cmd = GenerateOrderStatusReport(
            instrument_id=to_instrument_id("BTC-USDT-SWAP"),
            client_order_id=ClientOrderId("CL777"),
            venue_order_id=None,
            command_id=UUID4(),
            ts_init=_ts_init(),
        )
        report = await client.generate_order_status_report(cmd)

        assert report is not None
        assert report.account_id == AccountId("OKX-master")
        assert report.venue_order_id == VenueOrderId("777")
        assert report.client_order_id == ClientOrderId("CL777")
        assert report.order_side == OrderSide.BUY
        assert report.order_type == OrderType.LIMIT
        assert report.time_in_force == TimeInForce.GTC
        assert report.order_status == OrderStatus.ACCEPTED
        assert str(report.quantity) == "5"
        assert str(report.filled_qty) == "0"
        assert str(report.price) == "30000.5"
        # OKX cTime 是毫秒；NT ts_accepted 是纳秒
        assert report.ts_accepted == 1_714_000_000_000 * 1_000_000
        assert report.ts_last == 1_714_000_010_000 * 1_000_000


class TestGenerateOrderStatusReports:
    async def test_pending_only_with_open_only(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _build_exec_client_with_mocks(monkeypatch)
        client._rest.trade.get_pending_orders.return_value = [
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "1",
                "clOrdId": "C1",
                "side": "buy",
                "ordType": "limit",
                "state": "live",
                "sz": "10",
                "accFillSz": "0",
                "px": "30000",
                "cTime": "1714000000000",
                "uTime": "1714000000000",
            },
        ]
        # history 不该被调用：open_only=True
        client._rest.trade.get_orders_history.side_effect = AssertionError("must not call")

        cmd = GenerateOrderStatusReports(
            instrument_id=None,
            start=None,
            end=None,
            open_only=True,
            command_id=UUID4(),
            ts_init=_ts_init(),
        )
        reports = await client.generate_order_status_reports(cmd)

        # SWAP + SPOT 两次，但只有 SWAP 那次返回数据
        assert len(reports) == 2  # mock 对 SPOT 也返回同一条
        # 真实场景按 instType 区分；这里 mock 没区分但行为正确
        assert all(r.order_status == OrderStatus.ACCEPTED for r in reports)

    async def test_includes_history_when_not_open_only(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _build_exec_client_with_mocks(monkeypatch)
        client._rest.trade.get_pending_orders.return_value = []
        client._rest.trade.get_orders_history.return_value = [
            {
                "instId": "BTC-USDT-SWAP",
                "ordId": "2",
                "clOrdId": "C2",
                "side": "sell",
                "ordType": "limit",
                "state": "filled",
                "sz": "3",
                "accFillSz": "3",
                "px": "31000",
                "avgPx": "31000",
                "cTime": "1713999000000",
                "uTime": "1714000000000",
            },
        ]

        cmd = GenerateOrderStatusReports(
            instrument_id=None,
            start=_now() - pd.Timedelta(days=1),
            end=_now(),
            open_only=False,
            command_id=UUID4(),
            ts_init=_ts_init(),
        )
        reports = await client.generate_order_status_reports(cmd)

        # SWAP + SPOT 两轮，每轮一条：共 2
        assert len(reports) == 2
        assert all(r.order_status == OrderStatus.FILLED for r in reports)
        assert all(r.order_side == OrderSide.SELL for r in reports)
        # history 端点被调用过（SWAP + SPOT）
        assert client._rest.trade.get_orders_history.call_count == 2


class TestGenerateFillReports:
    async def test_translates_fill_dict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _build_exec_client_with_mocks(monkeypatch)
        client._rest.trade.get_fills.return_value = [
            {
                "instId": "BTC-USDT-SWAP",
                "tradeId": "TR1",
                "ordId": "ORD1",
                "clOrdId": "CL1",
                "side": "buy",
                "execType": "M",
                "fillPx": "30000.5",
                "fillSz": "2",
                "fee": "-0.05",
                "feeCcy": "USDT",
                "ts": "1714000000000",
            },
        ]

        cmd = GenerateFillReports(
            instrument_id=to_instrument_id("BTC-USDT-SWAP"),
            venue_order_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=_ts_init(),
        )
        reports = await client.generate_fill_reports(cmd)

        assert len(reports) == 1
        r = reports[0]
        assert r.trade_id.value == "TR1"
        assert r.venue_order_id.value == "ORD1"
        assert r.client_order_id.value == "CL1"
        assert r.order_side == OrderSide.BUY
        assert r.liquidity_side == LiquiditySide.MAKER
        assert str(r.last_qty) == "2"
        assert str(r.last_px) == "30000.5"
        # fee 取绝对值（NT Money 用 currency 精度补零）
        assert r.commission.as_decimal() == Decimal("0.05")
        assert r.commission.currency.code == "USDT"


class TestGeneratePositionStatusReports:
    async def test_long_position(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from okx_trade.models.account import Position
        client = _build_exec_client_with_mocks(monkeypatch)
        client._rest.account.get_positions.return_value = [
            Position.model_validate({
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "mgnMode": "cross",
                "posSide": "net",
                "pos": "5",
                "avgPx": "30000",
                "ts": "1714000000000",
            }),
        ]

        cmd = GeneratePositionStatusReports(
            instrument_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=_ts_init(),
        )
        reports = await client.generate_position_status_reports(cmd)

        assert len(reports) == 1
        r = reports[0]
        assert r.position_side == PositionSide.LONG
        assert str(r.quantity) == "5"
        assert r.avg_px_open == Decimal("30000")

    async def test_flat_position_returns_flat_report(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from okx_trade.models.account import Position
        client = _build_exec_client_with_mocks(monkeypatch)
        client._rest.account.get_positions.return_value = [
            Position.model_validate({
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "mgnMode": "cross",
                "posSide": "net",
                "pos": "0",
                "avgPx": "0",
                "ts": "1714000000000",
            }),
        ]

        cmd = GeneratePositionStatusReports(
            instrument_id=None,
            start=None,
            end=None,
            command_id=UUID4(),
            ts_init=_ts_init(),
        )
        reports = await client.generate_position_status_reports(cmd)

        assert len(reports) == 1
        assert reports[0].position_side == PositionSide.FLAT
