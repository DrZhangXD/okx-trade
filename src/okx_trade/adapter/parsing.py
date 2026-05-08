"""OKX ↔ NautilusTrader 翻译层（纯函数）。

所有「OKX dict / Pydantic 模型 → NT 对象」和反向的转换都集中在这里，原则：
1. **纯函数**：无 IO，便于独立单测；
2. **接受 OKX 原始字段**（snake_case Pydantic 或 camelCase dict 都支持，因为 SDK 已经做了 alias）；
3. **返回 NT 不可变对象**（Bar / QuoteTick / Instrument 等都是 Cython frozen 对象）。

把翻译独立出来的好处：未来 OKX 改字段或 NT 升 API，只改这一个文件。
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PriceType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.instruments import CryptoPerpetual, CurrencyPair
from nautilus_trader.model.objects import Currency, Price, Quantity

from ..enums import InstType, OrdType, Side
from .constants import OKX_VENUE

if TYPE_CHECKING:
    from ..models.common import Instrument as OKXInstrument
    from ..models.market import Candle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimal_places(d: Decimal) -> int:
    """``Decimal('0.001')`` → 3, ``Decimal('1')`` → 0。

    与 ``okx_trade.utils.precision._decimal_places`` 同语义，复制到这里避免
    adapter 包对 utils 的循环依赖。
    """
    s = format(d.normalize(), "f")
    return len(s.split(".", 1)[1]) if "." in s else 0


def to_instrument_id(inst_id: str) -> InstrumentId:
    """``"BTC-USDT-SWAP"`` → ``InstrumentId("BTC-USDT-SWAP.OKX")``。"""
    return InstrumentId(symbol=Symbol(inst_id), venue=OKX_VENUE)


def from_instrument_id(instrument_id: InstrumentId) -> str:
    """反向：``InstrumentId("BTC-USDT-SWAP.OKX")`` → ``"BTC-USDT-SWAP"``。"""
    return instrument_id.symbol.value


def _ms_to_nanos(ms: int) -> int:
    """OKX 时间戳是毫秒，NT 内部用纳秒。"""
    return int(ms) * 1_000_000


_AGG_TO_MS: dict[BarAggregation, int] = {
    BarAggregation.MILLISECOND: 1,
    BarAggregation.SECOND: 1_000,
    BarAggregation.MINUTE: 60_000,
    BarAggregation.HOUR: 3_600_000,
    BarAggregation.DAY: 86_400_000,
    BarAggregation.WEEK: 604_800_000,
}


def bar_spec_period_ms(spec: BarSpecification) -> int:
    """``BarSpecification`` → 一根 bar 的时长（毫秒）。

    用于把 OKX candle 开盘时间换算成收盘时间，覆盖 NT 支持的全部 step+aggregation
    组合（包括 2D / 3D 这类 ``BarSize`` 暴露但 ``_PERIOD_MS`` 列举不全的情况）。
    """
    agg_ms = _AGG_TO_MS.get(spec.aggregation)
    if agg_ms is None:
        raise ValueError(f"unsupported bar aggregation: {spec.aggregation}")
    return spec.step * agg_ms


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------


def parse_okx_instrument(okx_inst: OKXInstrument, ts_init: int) -> CryptoPerpetual | CurrencyPair:
    """OKX ``Instrument`` → NT ``CryptoPerpetual`` (SWAP) 或 ``CurrencyPair`` (SPOT)。

    Args:
        okx_inst: 从 ``client.public.get_instruments(...)`` 返回的 Pydantic 模型。
        ts_init: 当前时间戳（纳秒），通常取 ``LiveClock.timestamp_ns()``。

    Returns:
        NT 不可变 Instrument 对象，可直接 ``provider.add(...)`` 注册。

    Raises:
        ValueError: 未支持的 ``inst_type``（FUTURES/OPTION 暂不实现）。
    """
    instrument_id = to_instrument_id(okx_inst.inst_id)
    raw_symbol = Symbol(okx_inst.inst_id)

    tick_sz = okx_inst.tick_sz
    lot_sz = okx_inst.lot_sz
    min_sz = okx_inst.min_sz

    price_precision = _decimal_places(tick_sz)
    size_precision = _decimal_places(lot_sz)

    price_increment = Price(float(tick_sz), precision=price_precision)
    size_increment = Quantity(float(lot_sz), precision=size_precision)
    min_quantity = Quantity(float(min_sz), precision=size_precision) if min_sz > 0 else None

    quote_ccy = Currency.from_str(okx_inst.quote_ccy, strict=False) if okx_inst.quote_ccy else None
    base_ccy = Currency.from_str(okx_inst.base_ccy, strict=False) if okx_inst.base_ccy else None

    if okx_inst.inst_type == InstType.SWAP:
        # 永续合约：必须有 settle_ccy
        if not okx_inst.settle_ccy:
            raise ValueError(f"SWAP instrument {okx_inst.inst_id} missing settleCcy")
        settle_ccy = Currency.from_str(okx_inst.settle_ccy, strict=False)
        # OKX SWAP 不在响应里返回 baseCcy/quoteCcy（仅 SPOT/MARGIN 有），
        # 从 instId 反推（如 BTC-USDT-SWAP → base=BTC, quote=USDT）。
        if base_ccy is None or quote_ccy is None:
            parts = okx_inst.inst_id.split("-")
            if len(parts) >= 3:
                if base_ccy is None:
                    base_ccy = Currency.from_str(parts[0], strict=False)
                if quote_ccy is None:
                    quote_ccy = Currency.from_str(parts[1], strict=False)
        # OKX 反向合约：settleCcy == baseCcy（如 BTC-USD-SWAP，用 BTC 结算）
        is_inverse = okx_inst.settle_ccy == okx_inst.base_ccy
        # 合约面值（USD-margined 通常是 USDT 计价的某个数量）
        multiplier = Quantity(float(okx_inst.ct_val), precision=8) if okx_inst.ct_val > 0 \
            else Quantity(1, precision=0)
        return CryptoPerpetual(
            instrument_id=instrument_id,
            raw_symbol=raw_symbol,
            base_currency=base_ccy,
            quote_currency=quote_ccy,
            settlement_currency=settle_ccy,
            is_inverse=is_inverse,
            price_precision=price_precision,
            size_precision=size_precision,
            price_increment=price_increment,
            size_increment=size_increment,
            multiplier=multiplier,
            min_quantity=min_quantity,
            ts_event=ts_init,
            ts_init=ts_init,
            info={"okx_state": okx_inst.state, "okx_inst_type": okx_inst.inst_type.value},
        )

    if okx_inst.inst_type == InstType.SPOT:
        if base_ccy is None or quote_ccy is None:
            raise ValueError(f"SPOT instrument {okx_inst.inst_id} missing base/quote ccy")
        return CurrencyPair(
            instrument_id=instrument_id,
            raw_symbol=raw_symbol,
            base_currency=base_ccy,
            quote_currency=quote_ccy,
            price_precision=price_precision,
            size_precision=size_precision,
            price_increment=price_increment,
            size_increment=size_increment,
            min_quantity=min_quantity,
            ts_event=ts_init,
            ts_init=ts_init,
            info={"okx_state": okx_inst.state, "okx_inst_type": okx_inst.inst_type.value},
        )

    raise ValueError(f"unsupported OKX instType: {okx_inst.inst_type}")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def parse_okx_book5_to_quote_tick(
    okx_data: dict[str, Any],
    inst_id: str,
    price_precision: int,
    size_precision: int,
    ts_init: int,
) -> QuoteTick | None:
    """OKX ``books5`` 推送的首档 → ``QuoteTick``。

    OKX books5 frame 形如 ``{"bids": [["px","sz","_","_"], ...], "asks": [...], "ts": "..."}``。
    取首档 bid/ask 构造 NT QuoteTick（best bid + best ask）。
    深档信息丢失没关系——NT 上层策略要深度时应订阅 ``books`` 而非 ``books5``。

    Returns:
        ``QuoteTick`` 或 None（若 bids/asks 为空）。
    """
    bids = okx_data.get("bids", [])
    asks = okx_data.get("asks", [])
    if not bids or not asks:
        return None
    bid_px, bid_sz = bids[0][0], bids[0][1]
    ask_px, ask_sz = asks[0][0], asks[0][1]
    ts_event_ms = int(okx_data.get("ts", "0"))
    return QuoteTick(
        instrument_id=to_instrument_id(inst_id),
        bid_price=Price(float(bid_px), precision=price_precision),
        ask_price=Price(float(ask_px), precision=price_precision),
        bid_size=Quantity(float(bid_sz), precision=size_precision),
        ask_size=Quantity(float(ask_sz), precision=size_precision),
        ts_event=_ms_to_nanos(ts_event_ms) if ts_event_ms else ts_init,
        ts_init=ts_init,
    )


def parse_okx_trade_to_trade_tick(
    okx_trade: dict[str, Any],
    inst_id: str,
    price_precision: int,
    size_precision: int,
    ts_init: int,
) -> TradeTick:
    """OKX ``trades`` 推送 → ``TradeTick``。

    OKX trade frame 形如 ``{"instId":"BTC-USDT","tradeId":"...","px":"...","sz":"...",
    "side":"buy"/"sell","ts":"..."}``。
    """
    px = okx_trade["px"]
    sz = okx_trade["sz"]
    trade_id = okx_trade.get("tradeId", "")
    side = okx_trade.get("side", "buy")
    ts_event_ms = int(okx_trade.get("ts", "0"))
    aggressor = AggressorSide.BUYER if side == "buy" else AggressorSide.SELLER
    return TradeTick(
        instrument_id=to_instrument_id(inst_id),
        price=Price(float(px), precision=price_precision),
        size=Quantity(float(sz), precision=size_precision),
        aggressor_side=aggressor,
        trade_id=TradeId(str(trade_id)),
        ts_event=_ms_to_nanos(ts_event_ms) if ts_event_ms else ts_init,
        ts_init=ts_init,
    )


def parse_okx_candle_to_bar(
    candle: Candle,
    bar_type: BarType,
    price_precision: int,
    size_precision: int,
    ts_init: int,
) -> Bar:
    """OKX ``Candle`` → ``Bar``。

    ``ts_event`` = bar 收盘时间（``candle.ts + bar_period``）。OKX candle ``ts`` 是
    开盘时间，若直接当 ``ts_event`` 用，NT 回测引擎会在 bar 开盘瞬间就 dispatch
    这根 bar 的全部 OHLC，造成 lookahead；同时实盘多根 bar 共享 ingestion clock
    时也会让 ``ts_event`` 失去 chronological 语义。

    ``ts_init``（系统首次感知到该事件的时刻）由调用方决定：
    - 实盘 / 历史回填：传 ``clock.timestamp_ns()``（wall-clock）；
    - 回测：传与 ``ts_event`` 相同的收盘时间，使 NT 按事件顺序回放。
    """
    period_ms = bar_spec_period_ms(bar_type.spec)
    ts_event_ns = (int(candle.ts) + period_ms) * 1_000_000
    return Bar(
        bar_type=bar_type,
        open=Price(float(candle.open), precision=price_precision),
        high=Price(float(candle.high), precision=price_precision),
        low=Price(float(candle.low), precision=price_precision),
        close=Price(float(candle.close), precision=price_precision),
        volume=Quantity(float(candle.volume), precision=size_precision),
        ts_event=ts_event_ns,
        ts_init=ts_init,
    )


def make_bar_type(inst_id: str, period: str) -> BarType:
    """``("BTC-USDT-SWAP", "1m")`` → 对应的 NT ``BarType``。

    OKX period 字符串映射到 NT BarSpec：
    - ``1m`` → step=1, aggregation=MINUTE
    - ``1H`` → step=1, aggregation=HOUR
    - 等等
    """
    period_upper = period.upper()
    if period_upper.endswith("M") and not period_upper.endswith("MO"):
        # 1m 3m 5m 15m 30m
        step = int(period_upper[:-1])
        agg = BarAggregation.MINUTE
    elif period_upper.endswith("H"):
        step = int(period_upper[:-1])
        agg = BarAggregation.HOUR
    elif period_upper.endswith("D"):
        step = int(period_upper[:-1])
        agg = BarAggregation.DAY
    elif period_upper.endswith("W"):
        step = int(period_upper[:-1])
        agg = BarAggregation.WEEK
    else:
        raise ValueError(f"unsupported OKX period: {period}")
    spec = BarSpecification(step=step, aggregation=agg, price_type=PriceType.LAST)
    return BarType(
        instrument_id=to_instrument_id(inst_id),
        bar_spec=spec,
        aggregation_source=AggregationSource.EXTERNAL,
    )


def bar_type_to_okx_period(bar_type: BarType) -> str:
    """``BarType`` → OKX K 线 period 字符串（``"1m"`` / ``"1H"``）。

    用于 ``_subscribe_bars`` 把 NT 请求翻译回 OKX channel 名。
    """
    spec = bar_type.spec
    step = spec.step
    agg = spec.aggregation
    if agg == BarAggregation.MINUTE:
        return f"{step}m"
    if agg == BarAggregation.HOUR:
        return f"{step}H"
    if agg == BarAggregation.DAY:
        return f"{step}D"
    if agg == BarAggregation.WEEK:
        return f"{step}W"
    raise ValueError(f"unsupported NT BarAggregation: {agg}")


# ---------------------------------------------------------------------------
# Order side / type
# ---------------------------------------------------------------------------


def order_side_to_okx(side: OrderSide) -> Side:
    return Side.BUY if side == OrderSide.BUY else Side.SELL


def okx_side_to_nt(side: str) -> OrderSide:
    return OrderSide.BUY if side == "buy" else OrderSide.SELL


def order_type_and_tif_to_okx(order_type: OrderType, tif: TimeInForce) -> OrdType:
    """NT ``(OrderType, TimeInForce)`` → OKX ``ordType``。

    OKX 不区分 ordType 和 tif（一个字段表达），所以这里要做一次组合映射。
    """
    if order_type == OrderType.MARKET:
        return OrdType.MARKET
    if order_type == OrderType.LIMIT:
        if tif == TimeInForce.IOC:
            return OrdType.IOC
        if tif == TimeInForce.FOK:
            return OrdType.FOK
        # GTC / 默认
        return OrdType.LIMIT
    raise ValueError(f"unsupported NT OrderType: {order_type}")


# ---------------------------------------------------------------------------
# Reconciliation: OKX dict → NT report 字段
# ---------------------------------------------------------------------------


# OKX state → NT OrderStatus
# 注意：OKX 没有 SUBMITTED——下完单到 ACK 之前是「无状态」的（NT 自动维护）。
# OKX 也没有 PENDING_UPDATE/PENDING_CANCEL——这些是 NT 内部状态。
_OKX_STATE_TO_NT_STATUS: dict[str, OrderStatus] = {
    "live": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "mmp_canceled": OrderStatus.CANCELED,
}


def okx_state_to_order_status(state: str) -> OrderStatus:
    """OKX ``state`` → NT ``OrderStatus``。

    未识别的 state 抛 ``ValueError``——好过吞掉静默：reconciliation 误判
    会导致策略状态机和真实账户对不上。
    """
    try:
        return _OKX_STATE_TO_NT_STATUS[state]
    except KeyError as exc:
        raise ValueError(f"unsupported OKX order state: {state!r}") from exc


def okx_ord_type_to_nt(ord_type: str) -> tuple[OrderType, TimeInForce, bool]:
    """OKX ``ordType`` → ``(OrderType, TimeInForce, post_only)``。

    OKX 把 tif 编进 ordType（``ioc``/``fok``/``post_only``），NT 是分开两字段。
    返回三元组：第三个 bool 标记 post_only（NT 没有专门的 ordType）。

    - ``market`` → MARKET, IOC, False
    - ``limit`` → LIMIT, GTC, False
    - ``post_only`` → LIMIT, GTC, True
    - ``fok`` → LIMIT, FOK, False
    - ``ioc`` / ``optimal_limit_ioc`` → LIMIT, IOC, False
    """
    if ord_type == "market":
        return OrderType.MARKET, TimeInForce.IOC, False
    if ord_type == "limit":
        return OrderType.LIMIT, TimeInForce.GTC, False
    if ord_type == "post_only":
        return OrderType.LIMIT, TimeInForce.GTC, True
    if ord_type == "fok":
        return OrderType.LIMIT, TimeInForce.FOK, False
    if ord_type in ("ioc", "optimal_limit_ioc"):
        return OrderType.LIMIT, TimeInForce.IOC, False
    raise ValueError(f"unsupported OKX ordType: {ord_type!r}")


def okx_pos_to_position_side(pos: Decimal, pos_side: str) -> PositionSide:
    """OKX 持仓行 → NT ``PositionSide``。

    - ``net`` 模式：靠 ``pos`` 正负号判断；``pos == 0`` → FLAT；
    - ``long_short`` 模式：``posSide`` 字段直接给方向。
    """
    if pos == 0:
        return PositionSide.FLAT
    if pos_side == "long":
        return PositionSide.LONG
    if pos_side == "short":
        return PositionSide.SHORT
    # net 模式
    return PositionSide.LONG if pos > 0 else PositionSide.SHORT


def okx_exec_type_to_liquidity(exec_type: str) -> LiquiditySide:
    """``M`` → MAKER；``T`` 或其他 → TAKER。"""
    if exec_type == "M":
        return LiquiditySide.MAKER
    if exec_type == "T":
        return LiquiditySide.TAKER
    return LiquiditySide.NO_LIQUIDITY_SIDE


__all__ = [
    "to_instrument_id",
    "from_instrument_id",
    "parse_okx_instrument",
    "parse_okx_book5_to_quote_tick",
    "parse_okx_trade_to_trade_tick",
    "parse_okx_candle_to_bar",
    "make_bar_type",
    "bar_type_to_okx_period",
    "order_side_to_okx",
    "okx_side_to_nt",
    "order_type_and_tif_to_okx",
    "okx_state_to_order_status",
    "okx_ord_type_to_nt",
    "okx_pos_to_position_side",
    "okx_exec_type_to_liquidity",
]
