"""区间假突破回归策略（Range Breakout Scalping），从 ``~/scalp/strategy.py`` 移植。

规则
----
1. 用「上一根已完结」的 ``range_bar`` K 线（默认日 / 周）的高低点形成区间
   ``[range_low, range_high]``。
2. 在 ``signal_bar`` K 线（默认 1H / 15m）上找：
   - 一根 K 线**实体**完全收盘在区间外（上破或下破）；
   - 紧接着一根 K 线收盘**回到区间内**——这就是「假突破 → 回归」入场点。
3. 方向：
   - 上破回归 → 做空；下破回归 → 做多。
   - 止损 = 突破期间的极值；止盈 = 入场到止损距离的 ``tp_rr_ratio`` 倍（默认 2R）。

过滤
----
- 上区入场禁止做多、下区入场禁止做空（防止追高 / 追低）；
- 突破幅度过大时尝试用 swing 止损替代极值止损；仍超阈值的 1.5 倍则放弃信号。

为什么拆成「纯函数 + NT Strategy 外壳」
----------------------------------
- 纯函数 ``detect_range_breakout_signal(bars, wrange, params)`` 不依赖 NT，
  接受 ``list[BarSnapshot]`` → 返回 ``ScalpSignal | None``，便于单测；
- NT Strategy 只负责：维护两个 BarBuffer、调用纯函数、把 Signal 翻成 NT Order。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from .base import (
    BarBuffer,
    BarSnapshot,
    Direction,
    ScalpSignal,
    position_contracts,
    to_bar_snapshots,
)
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

State = Literal["idle", "breakout_up", "breakout_down"]


# ---------------------------------------------------------------------------
# Pure data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Range:
    """已完结区间 K 线。"""

    high: float
    low: float
    range_open_ts: int   # 区间 K 线起始时间戳（毫秒）

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def width(self) -> float:
        return self.high - self.low

    def contains_close(self, close: float) -> bool:
        return self.low < close < self.high


@dataclass(frozen=True, slots=True)
class RangeBreakoutLogic:
    """策略参数（纯函数共用）。"""

    tp_rr_ratio: float = 2.0
    max_stop_distance_pct: float = 0.02
    swing_lookback: int = 10


# ---------------------------------------------------------------------------
# Pure functions（无 IO，可单测）
# ---------------------------------------------------------------------------


def last_completed_range(bars: list[BarSnapshot]) -> Range | None:
    """从 ``range_bar`` 序列中取最后一根 confirmed 作为区间。"""
    for bar in reversed(bars):
        if bar.confirm:
            return Range(high=bar.high, low=bar.low, range_open_ts=bar.ts)
    return None


def detect_range_breakout_signal(
    bars: list[BarSnapshot],
    wrange: Range,
    params: RangeBreakoutLogic,
) -> ScalpSignal | None:
    """对一段 ``signal_bar`` 序列做状态机扫描，仅当**最后一根**完成「回归」时返回信号。

    输入 ``bars`` 应**按时间升序**排列；只考虑 ``confirm=True`` 的 K 线。
    """
    confirmed = [b for b in bars if b.confirm]
    if len(confirmed) < 2:
        return None

    state: State = "idle"
    extreme = 0.0
    last_idx = len(confirmed) - 1
    signal: ScalpSignal | None = None

    for i, bar in enumerate(confirmed):
        hi, lo, c = bar.high, bar.low, bar.close
        close_above = c > wrange.high
        close_below = c < wrange.low
        close_back_in = wrange.low <= c <= wrange.high

        if state == "idle":
            if close_above:
                state, extreme = "breakout_up", hi
            elif close_below:
                state, extreme = "breakout_down", lo

        elif state == "breakout_up":
            if hi > wrange.high:
                extreme = max(extreme, hi)
            if close_back_in:
                if i == last_idx:
                    signal = _build_signal(
                        direction="short", entry=c, extreme=extreme,
                        bars_before=confirmed[:i],
                        wrange=wrange, trigger_ts=bar.ts, params=params,
                    )
                state = "idle"
            elif close_below:
                state, extreme = "breakout_down", lo

        else:  # breakout_down
            if lo < wrange.low:
                extreme = min(extreme, lo)
            if close_back_in:
                if i == last_idx:
                    signal = _build_signal(
                        direction="long", entry=c, extreme=extreme,
                        bars_before=confirmed[:i],
                        wrange=wrange, trigger_ts=bar.ts, params=params,
                    )
                state = "idle"
            elif close_above:
                state, extreme = "breakout_up", hi

    return signal


def _build_signal(
    *,
    direction: Direction,
    entry: float,
    extreme: float,
    bars_before: list[BarSnapshot],
    wrange: Range,
    trigger_ts: int,
    params: RangeBreakoutLogic,
) -> ScalpSignal | None:
    raw_stop = extreme
    used_swing = False

    # 区间四分位过滤：禁止顶部追多 / 底部追空
    upper_third = wrange.mid + wrange.width * 0.25
    lower_third = wrange.mid - wrange.width * 0.25
    if direction == "long" and entry > upper_third:
        return None
    if direction == "short" and entry < lower_third:
        return None

    stop_distance = abs(entry - raw_stop)
    if entry > 0 and stop_distance / entry > params.max_stop_distance_pct:
        # 尝试 swing 止损
        swing_bars = bars_before[-params.swing_lookback:]
        if swing_bars:
            if direction == "long":
                swing_stop = min(b.low for b in swing_bars)
                if swing_stop < entry and (entry - swing_stop) < stop_distance:
                    raw_stop = swing_stop
                    used_swing = True
            else:
                swing_stop = max(b.high for b in swing_bars)
                if swing_stop > entry and (swing_stop - entry) < stop_distance:
                    raw_stop = swing_stop
                    used_swing = True
        new_dist = abs(entry - raw_stop)
        if entry > 0 and new_dist / entry > params.max_stop_distance_pct * 1.5:
            return None

    # TP 计算
    if direction == "long":
        if raw_stop >= entry:
            return None
        dist = entry - raw_stop
        tp = entry + dist * params.tp_rr_ratio
    else:
        if raw_stop <= entry:
            return None
        dist = raw_stop - entry
        tp = entry - dist * params.tp_rr_ratio

    reason = (
        f"{direction.upper()} after {'breakout_up' if direction == 'short' else 'breakout_down'}"
        f" return; extreme={extreme:.4f}, stop={raw_stop:.4f}"
        f"{' (swing)' if used_swing else ''}"
    )
    return ScalpSignal(
        direction=direction,
        entry_price=entry,
        stop_price=raw_stop,
        take_profit_price=tp,
        breakout_extreme=extreme,
        used_swing_stop=used_swing,
        trigger_bar_ts=trigger_ts,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# NautilusTrader Strategy wrapper
# ---------------------------------------------------------------------------


if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar

try:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover —— pyproject [strategy] extra 未装时的 fallback
    _NT_AVAILABLE = False
    StrategyConfig = object  # type: ignore[assignment,misc]
    Strategy = object  # type: ignore[assignment,misc]


if _NT_AVAILABLE:

    class RangeBreakoutConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Range Breakout NT Strategy 配置。

        Attributes:
            instrument_id: 交易品种 NT id（如 ``"BTC-USDT-SWAP.OKX"``）。
            range_bar_type: 区间 K 线 NT BarType（如 ``"BTC-USDT-SWAP.OKX-1-DAY-LAST-EXTERNAL"``）。
            signal_bar_type: 信号 K 线 NT BarType（如 ``"BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL"``）。
            tp_rr_ratio / max_stop_distance_pct / swing_lookback: 见 ``RangeBreakoutLogic``。
            risk_pct: 单笔风险占净值（默认 0.005 = 0.5%）。
            account_equity_usdt: 用于回测的虚拟净值；实盘留 0 → 从账户读取。
            signal_buffer_size: 信号 K 线缓冲深度（默认 200）。
            risk_config: 可选的 ``RiskConfig`` —— ``None`` 走原仓位，否则下单前
                过 ``RiskManager``。M3.6 接入。
        """

        instrument_id: str
        range_bar_type: str
        signal_bar_type: str
        tp_rr_ratio: float = 2.0
        max_stop_distance_pct: float = 0.02
        swing_lookback: int = 10
        risk_pct: float = 0.005
        account_equity_usdt: float = 10000.0
        signal_buffer_size: int = 200
        risk_config: RiskConfig | None = None

    class RangeBreakoutStrategy(Strategy):  # type: ignore[misc]
        """NT 包装：维护两个 BarBuffer，每根 signal bar 收盘时跑信号检测，触发时下单。

        持仓管理使用「synthetic bracket」：进场后跟踪 ``_active_signal``，每根 bar
        检查 close 是否击穿 SL/TP，触发后用反向 market order 平仓。

        这样不依赖 OKX adapter 的 stop order / bracket 支持，回测和实盘都能跑。
        """

        def __init__(self, config: RangeBreakoutConfig) -> None:
            super().__init__(config)
            # 注：NT Strategy 是 Cython 类，self.config 已由 super().__init__(config) 设置；
            # 不要再赋值，否则 AttributeError。需要类型提示时通过 cast 或 type: ignore 局部使用。
            self.instrument_id = InstrumentId.from_str(config.instrument_id)
            self.range_bar_type = BarType.from_str(config.range_bar_type)
            self.signal_bar_type = BarType.from_str(config.signal_bar_type)

            self._range_buffer = BarBuffer(maxlen=10)        # 区间 bar 只看最新一根，多留备份
            self._signal_buffer = BarBuffer(maxlen=config.signal_buffer_size)
            self._params = RangeBreakoutLogic(
                tp_rr_ratio=config.tp_rr_ratio,
                max_stop_distance_pct=config.max_stop_distance_pct,
                swing_lookback=config.swing_lookback,
            )

            self._active_signal: ScalpSignal | None = None
            self._position_contracts: float = 0.0

            # M3.6 风控（None config → 透明跳过）
            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            # 上一根 bar 的 close，用于算 returns 喂 vol_target
            self._prev_close: float | None = None

            # M5: PnL tracker（外部注入；live_node / 测试 fixture 设置）
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None

        def on_start(self) -> None:
            self.subscribe_bars(self.range_bar_type)
            self.subscribe_bars(self.signal_bar_type)
            self.log.info(
                f"range_breakout subscribed: range={self.range_bar_type}, "
                f"signal={self.signal_bar_type}"
            )

        def on_bar(self, bar: Bar) -> None:
            # 路由：按 bar.bar_type 分到两个 buffer
            snap = to_bar_snapshots([bar])[0]
            if bar.bar_type == self.range_bar_type:
                self._range_buffer.append(snap)
                return
            if bar.bar_type != self.signal_bar_type:
                return

            self._signal_buffer.append(snap)

            # 喂风控数据（每根信号 bar）
            self._feed_risk_data(snap)

            # 已持仓：先检查 SL/TP
            if self._active_signal is not None:
                self._check_exit_on_bar(snap)
                return

            # 计算当前区间
            wrange = last_completed_range(list(self._range_buffer))
            if wrange is None:
                return

            # 检测信号
            signal = detect_range_breakout_signal(
                list(self._signal_buffer), wrange, self._params,
            )
            if signal is None:
                return

            self._enter_position(signal)

        def _feed_risk_data(self, snap: BarSnapshot) -> None:
            """把每根 signal bar 的 returns / equity 喂给 risk handles。"""
            handles = self._risk_handles
            inst_id_str = self.instrument_id.value

            # vol_target: 日收益 (close_t / close_{t-1}) - 1
            if handles.vol_target is not None and self._prev_close is not None \
                    and self._prev_close > 0:
                ret = snap.close / self._prev_close - 1.0
                handles.vol_target.update_return(inst_id_str, ret)
            self._prev_close = snap.close

            # drawdown + correlation：用 portfolio 当前净值。读不到就跳过
            equity_usdt: float | None = None
            try:
                account = self.portfolio.account(self.instrument_id.venue)
                if account is not None:
                    # NT Account 的 ``balance_total`` 是 Money 对象
                    from nautilus_trader.model.currencies import USDT
                    bal = account.balance_total(USDT)
                    if bal is not None:
                        equity_usdt = float(bal.as_decimal())
            except Exception:
                equity_usdt = None  # 回测早期可能还没初始化，静默跳过

            if equity_usdt is not None:
                # equity 用 wall-clock：snap.ts 是 1H signal_bar 收线时间，会落到未来。
                now_ms = int(time.time() * 1000)
                if handles.drawdown_tracker is not None:
                    handles.drawdown_tracker.record_equity(ts_ms=now_ms, equity=equity_usdt)
                # M5: 每个 UTC 日写一条 equity snapshot 给 PnL tracker（喂 correlation）
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=now_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        def _make_intent(self, signal: ScalpSignal, base_size: float) -> RiskIntent:
            return RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self.instrument_id.value,
                direction=signal.direction,
                size=base_size,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                account_equity_usdt=self.config.account_equity_usdt,
            )

        def on_stop(self) -> None:
            self.log.info(
                f"range_breakout stopped; active_signal={self._active_signal is not None}"
            )

        # ------------------------------------------------------------------
        # 持仓管理（synthetic bracket）
        # ------------------------------------------------------------------

        def _enter_position(self, signal: ScalpSignal) -> None:
            inst = self.cache.instrument(self.instrument_id)
            if inst is None:
                self.log.warning(f"instrument not in cache: {self.instrument_id}")
                return

            ct_val = float(inst.multiplier)  # SWAP 的合约面值（CryptoPerpetual.multiplier）
            lot_sz = float(inst.size_increment)
            min_sz = float(inst.size_increment)  # NT 没显式 min_sz，用 size_increment 当下限

            contracts = position_contracts(
                account_equity_usdt=self.config.account_equity_usdt,
                risk_pct=self.config.risk_pct,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                ct_val=ct_val if ct_val > 0 else 1.0,
                min_sz=min_sz,
                lot_sz=lot_sz if lot_sz > 0 else 1.0,
            )
            if contracts <= 0:
                self.log.info(f"signal skipped: contracts=0 ({signal.reason})")
                return

            # M3.6: 过 RiskManager
            intent = self._make_intent(signal, base_size=contracts)
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None:
                # 被风控 REJECT —— 不下单也不进入持仓状态
                return
            if adjusted != contracts:
                contracts = adjusted

            qty_obj = safe_make_qty(
                inst, contracts, self.log, ctx=f"enter {self.instrument_id}"
            )
            if qty_obj is None:
                return
            side = OrderSide.BUY if signal.direction == "long" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=side,
                quantity=qty_obj,
                time_in_force=TimeInForce.IOC,
            )
            self.submit_order(order)
            self._active_signal = signal
            self._position_contracts = contracts
            self.log.info(
                f"ENTER {signal.direction.upper()} {contracts} @ {signal.entry_price:.4f} "
                f"sl={signal.stop_price:.4f} tp={signal.take_profit_price:.4f}"
            )

        def _check_exit_on_bar(self, bar: BarSnapshot) -> None:
            assert self._active_signal is not None
            sig = self._active_signal

            # 用 high/low 检查（更接近实盘 stop 触发逻辑）
            if sig.direction == "long":
                hit_sl = bar.low <= sig.stop_price
                hit_tp = bar.high >= sig.take_profit_price
            else:
                hit_sl = bar.high >= sig.stop_price
                hit_tp = bar.low <= sig.take_profit_price

            if not (hit_sl or hit_tp):
                return

            # SL 优先（保守）
            reason = "SL" if hit_sl else "TP"
            exit_price = sig.stop_price if reason == "SL" else sig.take_profit_price
            self._exit_position(reason, exit_price=exit_price, ts_ms=bar.ts)

        def _exit_position(
            self,
            reason: str,
            *,
            exit_price: float | None = None,
            ts_ms: int | None = None,
        ) -> None:
            sig = self._active_signal
            if sig is None or self._position_contracts <= 0:
                return
            inst = self.cache.instrument(self.instrument_id)
            if inst is None:
                return
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            contracts = self._position_contracts
            qty_obj = safe_make_qty(
                inst, contracts, ctx=f"exit {self.instrument_id}"
            )
            if qty_obj is None:
                self.log.error(
                    f"EXIT skipped: position contracts {contracts} < lot_sz "
                    f"for {self.instrument_id}; position remains open"
                )
                return
            # 反向平仓（reduce-only）
            close_side = OrderSide.SELL if sig.direction == "long" else OrderSide.BUY
            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=close_side,
                quantity=qty_obj,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )
            self.submit_order(order)
            self.log.info(
                f"EXIT {sig.direction.upper()} reason={reason} qty={contracts}"
            )

            # M5: 把这笔成交写进 PnL tracker
            if exit_price is not None and ts_ms is not None:
                cfg: RangeBreakoutConfig = self.config  # type: ignore[assignment]
                record_strategy_trade(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    instrument_id=self.instrument_id.value,
                    closed_ts_ms=ts_ms,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    exit_price=exit_price,
                    contracts=contracts,
                    ct_val=ct_val,
                    risk_usdt=cfg.account_equity_usdt * cfg.risk_pct,
                )

            self._active_signal = None
            self._position_contracts = 0.0

else:  # pragma: no cover —— [strategy] extra 未装：留 stub 让 import 不炸
    class RangeBreakoutConfig:  # type: ignore[no-redef]
        pass

    class RangeBreakoutStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "Range",
    "RangeBreakoutConfig",
    "RangeBreakoutLogic",
    "RangeBreakoutStrategy",
    "detect_range_breakout_signal",
    "last_completed_range",
]
