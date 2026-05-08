"""``OKXLiveDataClient``：把 OKX WS 行情翻译为 NT data event。

订阅映射（NT 命令 → OKX 频道）::

    SubscribeQuoteTicks    → books5 频道（取首档 bid/ask）
    SubscribeTradeTicks    → trades 频道
    SubscribeBars          → candle{period} 频道（business endpoint）
    SubscribeOrderBook     → books5 / books / books-l2-tbt 之一（按 depth 选）
    SubscribeFundingRates  → funding-rate 频道
    SubscribeMarkPrices    → mark-price 频道

未实现的（M1 范围之外，留 TODO）::

    SubscribeIndexPrices, SubscribeInstrumentStatus, SubscribeOptionGreeks ...

收到推送 → ``_on_okx_<channel>(frame)`` → ``parse_*`` → ``self._handle_data(...)``。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nautilus_trader.live.data_client import LiveMarketDataClient

from ..config import OKXSettings
from ..rest.client import OKXRestClient
from ..utils.logging import get_logger
from ..ws.client import OKXWSClient
from .parsing import (
    bar_type_to_okx_period,
    from_instrument_id,
    parse_okx_book5_to_quote_tick,
    parse_okx_candle_to_bar,
    parse_okx_trade_to_trade_tick,
)

if TYPE_CHECKING:
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.data.messages import (
        SubscribeBars,
        SubscribeOrderBook,
        SubscribeQuoteTicks,
        SubscribeTradeTicks,
        UnsubscribeBars,
        UnsubscribeOrderBook,
        UnsubscribeQuoteTicks,
        UnsubscribeTradeTicks,
    )
    from nautilus_trader.model.identifiers import InstrumentId

    from .instrument_provider import OKXInstrumentProvider


log = get_logger(__name__)


class OKXLiveDataClient(LiveMarketDataClient):
    """OKX 行情数据客户端。

    构造由 ``OKXLiveDataClientFactory`` 完成，业务代码不直接 ``OKXLiveDataClient(...)``。

    内部维护一个 ``OKXWSClient`` 和一个 ``OKXRestClient``：
    - WS 用于实时订阅（QuoteTick / TradeTick / Bar 推送）；
    - REST 用于一次性请求（``_request_bars`` 拉历史 K 线、初始化 instruments）。
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id,
        venue,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: OKXInstrumentProvider,
        settings: OKXSettings,
        config=None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=venue,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._settings = settings
        self._rest: OKXRestClient | None = None
        self._ws: OKXWSClient | None = None
        # 跟踪每个 (channel, instId) 的精度，避免每次推送都查 cache
        self._inst_precision_cache: dict[str, tuple[int, int]] = {}
        # 记录已订阅的 (channel, instId)，便于 unsubscribe 时反查
        self._active_subs: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """NT 启动调用：建立 REST + WS 客户端，预加载 instruments。"""
        self._rest = OKXRestClient(self._settings)
        await self._rest.__aenter__()
        self._ws = OKXWSClient(self._settings)
        # 预加载 instruments（写到 NT cache，策略层就能找）
        await self._instrument_provider.initialize()
        for inst in self._instrument_provider.list_all():
            self._handle_data(inst)
        log.info("okx.data_client.connected", extra={"venue": str(self.venue)})

    async def _disconnect(self) -> None:
        """NT 停止调用：关闭 WS + REST。"""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._rest is not None:
            await self._rest.__aexit__(None, None, None)
            self._rest = None
        log.info("okx.data_client.disconnected")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_precision(self, instrument_id: InstrumentId) -> tuple[int, int]:
        """从 cache 取 (price_precision, size_precision)，缓存到本地以加速热路径。"""
        key = instrument_id.value
        if key in self._inst_precision_cache:
            return self._inst_precision_cache[key]
        inst = self._cache.instrument(instrument_id)
        if inst is None:
            # instrument 还没加载，用保守默认值（后续可改为抛错）
            return (8, 8)
        pair = (inst.price_precision, inst.size_precision)
        self._inst_precision_cache[key] = pair
        return pair

    async def _ensure_ws(self) -> OKXWSClient:
        if self._ws is None:
            raise RuntimeError("OKX WS client not connected; call _connect() first")
        return self._ws

    # ------------------------------------------------------------------
    # Subscribe: QuoteTicks → books5
    # ------------------------------------------------------------------

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        ws = await self._ensure_ws()
        inst_id = from_instrument_id(command.instrument_id)
        price_p, size_p = self._get_precision(command.instrument_id)

        async def handler(frame: dict[str, Any]) -> None:
            for data in frame.get("data", []):
                qt = parse_okx_book5_to_quote_tick(
                    data,
                    inst_id=inst_id,
                    price_precision=price_p,
                    size_precision=size_p,
                    ts_init=self._clock.timestamp_ns(),
                )
                if qt is not None:
                    self._handle_data(qt)

        ws.on("books5", handler, instId=inst_id)
        await ws.activate("books5", instId=inst_id)
        self._active_subs.add(("books5", inst_id))

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        ws = await self._ensure_ws()
        inst_id = from_instrument_id(command.instrument_id)
        await ws.deactivate("books5", instId=inst_id)
        self._active_subs.discard(("books5", inst_id))

    # ------------------------------------------------------------------
    # Subscribe: TradeTicks → trades
    # ------------------------------------------------------------------

    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        ws = await self._ensure_ws()
        inst_id = from_instrument_id(command.instrument_id)
        price_p, size_p = self._get_precision(command.instrument_id)

        async def handler(frame: dict[str, Any]) -> None:
            for data in frame.get("data", []):
                tt = parse_okx_trade_to_trade_tick(
                    data,
                    inst_id=inst_id,
                    price_precision=price_p,
                    size_precision=size_p,
                    ts_init=self._clock.timestamp_ns(),
                )
                self._handle_data(tt)

        ws.on("trades", handler, instId=inst_id)
        await ws.activate("trades", instId=inst_id)
        self._active_subs.add(("trades", inst_id))

    async def _unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None:
        ws = await self._ensure_ws()
        inst_id = from_instrument_id(command.instrument_id)
        await ws.deactivate("trades", instId=inst_id)
        self._active_subs.discard(("trades", inst_id))

    # ------------------------------------------------------------------
    # Subscribe: Bars → candle{period}
    # ------------------------------------------------------------------

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        ws = await self._ensure_ws()
        bar_type = command.bar_type
        inst_id = from_instrument_id(bar_type.instrument_id)
        period = bar_type_to_okx_period(bar_type)
        channel = f"candle{period}"
        price_p, size_p = self._get_precision(bar_type.instrument_id)

        async def handler(frame: dict[str, Any]) -> None:
            # OKX candle data: [ts, o, h, l, c, vol, volCcy, ...]，每条是字符串数组
            from ..models.market import Candle  # 局部 import 避免顶层耦合
            for arr in frame.get("data", []):
                if len(arr) < 6:
                    continue
                candle = Candle.from_array(arr)
                bar = parse_okx_candle_to_bar(
                    candle, bar_type=bar_type,
                    price_precision=price_p, size_precision=size_p,
                    ts_init=self._clock.timestamp_ns(),
                )
                self._handle_data(bar)

        ws.on(channel, handler, instId=inst_id)
        await ws.activate(channel, instId=inst_id)
        self._active_subs.add((channel, inst_id))

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        ws = await self._ensure_ws()
        bar_type = command.bar_type
        inst_id = from_instrument_id(bar_type.instrument_id)
        period = bar_type_to_okx_period(bar_type)
        channel = f"candle{period}"
        await ws.deactivate(channel, instId=inst_id)
        self._active_subs.discard((channel, inst_id))

    # ------------------------------------------------------------------
    # Subscribe: OrderBook
    # ------------------------------------------------------------------

    async def _subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None:
        # OKX books / books5 / books-l2-tbt（VIP 用户）三档深度可选；
        # M1 简化为 books5 + 一次性 snapshot；deltas 完整实现留 M2
        # 暂未实现完整 delta 流；策略需要深度时建议直接 subscribe books5 走 QuoteTick
        await self._ensure_ws()  # 仅校验连接已建立
        inst_id = from_instrument_id(command.instrument_id)
        log.warning(
            "okx.data_client.order_book_deltas_not_implemented",
            extra={"instrument_id": inst_id, "depth": command.depth},
        )

    async def _unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None:
        # 对应未实现，no-op
        return

    # ------------------------------------------------------------------
    # Request: 历史 K 线（NT _request_bars）
    # ------------------------------------------------------------------

    async def _request_bars(self, request) -> None:  # type: ignore[no-untyped-def]
        """NT 请求历史 K 线（一次性）→ 走 REST。"""
        if self._rest is None:
            raise RuntimeError("OKX REST client not connected")
        bar_type = request.bar_type
        inst_id = from_instrument_id(bar_type.instrument_id)
        period = bar_type_to_okx_period(bar_type)
        # OKX REST 一次最多 300 根
        limit = min(300, getattr(request, "limit", 100) or 100)
        candles = await self._rest.market.get_candles(inst_id, bar=period, limit=limit)
        price_p, size_p = self._get_precision(bar_type.instrument_id)
        ts_now = self._clock.timestamp_ns()
        bars = [
            parse_okx_candle_to_bar(
                c, bar_type=bar_type,
                price_precision=price_p, size_precision=size_p,
                ts_init=ts_now,
            )
            for c in candles
        ]
        self._handle_bars(bar_type, bars, partial=None, correlation_id=request.id)


__all__ = ["OKXLiveDataClient"]
