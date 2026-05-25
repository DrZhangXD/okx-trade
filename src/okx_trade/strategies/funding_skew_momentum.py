"""Funding-Skew Momentum：funding rate 极端拥挤反向交易。

直觉
----
当 perp funding rate 突然冲到历史 30 天 +2σ → 多头过度拥挤 + 强制持仓人付钱给空头
→ 短期内多头被挤出 / 平仓 → 价格下跌（拥挤交易反向）。
反之 funding 急速冲到 -2σ → 空头被挤压 → 短期反弹。

参考：Soska/Chen (2022) 在 Coinbase Derivatives 上的实证，crypto perp 上的拥挤
效应明显高于传统期货。

机制
----
- 每个标的维护 30 天 funding rate deque（OKX 每 8h 结算 1 次 = 90 个采样）
- 每 funding cycle 结算后 + 1 分钟拉新值
- z-score = (current - 30d_mean) / 30d_std
  - |z| > z_threshold 触发反向：z > +2 → short，z < -2 → long
- SL/TP = N × ATR_14 + RR ratio
- hold_max_hours 超时强平
- 风控：reversal kind，regime gate 开（震荡市加强、趋势市砍仓）

冲突避免
-------
本策略和 funding_carry 在同一个标的可能撞向：
- funding_carry 在 funding 持续 ≥ 8% APR 时开 carry（spot long + perp short）
- funding_skew 在 funding 突然冲高时也 short perp
两者方向一致，所以撞仓的概率 = 增量买卖压力，但风险被双倍计算。
做法：策略层维护 ``_other_strategy_positions`` 字典让 monitor 注入跨策略持仓信息
（v1 暂不实现，先靠 risk_pct 总额控制风险）。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Literal

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from ..risk.stats import zscore
from .base import BarSnapshot, effective_equity_usdt, position_contracts, to_bar_snapshots
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar
    from ..backtest.funding_data import FundingPanel


TradeDir = Literal["long", "short"]


try:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NT_AVAILABLE = False
    StrategyConfig = object  # type: ignore[assignment,misc]
    Strategy = object  # type: ignore[assignment,misc]


def funding_skew_decision(
    current_funding: float,
    history: list[float],
    z_threshold: float = 2.0,
) -> TradeDir | None:
    """funding 突破 ±z_threshold 时给出反向交易方向。

    Args:
        current_funding: 当前 8h funding 费率（如 0.0001 = 0.01%）。
        history: 过去 N 个 funding rate（不含 current）。
        z_threshold: ±σ 阈值。

    Returns:
        ``"short"`` if current is too positive（多头拥挤反向）
        ``"long"`` if current is too negative（空头被挤反弹）
        ``None`` 数据不足或未达阈值
    """
    if len(history) < 10:
        return None
    z = zscore(current_funding, history)
    if z >= z_threshold:
        return "short"
    if z <= -z_threshold:
        return "long"
    return None


def compute_atr(bars: list[BarSnapshot], period: int = 14) -> float:
    """简单 ATR（不含 EWMA，按 SMA(TR) 计算）。返回最近 ``period`` bar 的 ATR。"""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        h = bars[i].high
        l = bars[i].low
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


if _NT_AVAILABLE:

    class FundingSkewConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Funding Skew Momentum NT Strategy 配置。

        Attributes:
            instrument_id: 单标的（每个 instrument 一个 strategy 实例）。
            bar_type: ATR 计算 + SL/TP 检查的 K 线（推荐 1H 平衡精度与噪声）。
            history_periods: funding rate 历史样本数（默认 90 = 30 天 × 3 funding/day）。
            z_threshold: ±σ 入场阈值。
            hold_max_hours: 单笔持仓上限（默认 72h）。
            atr_period / atr_sl_mult: SL = N × ATR。
            tp_rr_ratio: TP / SL 比。
            risk_pct: 单笔风险占净值。
            check_interval_sec: funding rate poll 间隔（默认 1800s = 30min）。
            account_equity_usdt: 起始净值。
            risk_config: 风控配置。
        """

        instrument_id: str
        bar_type: str
        history_periods: int = 90
        z_threshold: float = 2.0
        hold_max_hours: int = 72
        atr_period: int = 14
        atr_sl_mult: float = 2.0
        tp_rr_ratio: float = 1.5
        risk_pct: float = 0.005
        check_interval_sec: int = 1800
        account_equity_usdt: float = 10000.0
        risk_config: RiskConfig | None = None


    class FundingSkewStrategy(Strategy):  # type: ignore[misc]
        """每 ~30min poll funding rate，z-score 触发反向交易。"""

        def __init__(self, config: FundingSkewConfig) -> None:
            super().__init__(config)
            self._inst_id = InstrumentId.from_str(config.instrument_id)
            self._bar_type = BarType.from_str(config.bar_type)
            self._funding_history: deque[float] = deque(maxlen=config.history_periods)
            self._bars: deque[BarSnapshot] = deque(maxlen=max(50, config.atr_period * 3))

            self._active_direction: TradeDir | None = None
            self._entry_price: float = 0.0
            self._stop_price: float = 0.0
            self._tp_price: float = 0.0
            self._position_contracts: float = 0.0
            self._entry_ts_ms: int = 0
            self._latest_close: float = 0.0
            self._latest_ts_ms: int = 0
            self._last_check_ts_ns: int = 0
            self._allocated_equity_usdt: float | None = None

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None
            self._check_task: asyncio.Task | None = None
            self._rest = None  # type: ignore[var-annotated]
            self._rest_settings = None  # type: ignore[var-annotated]

            self._funding_panels: dict[str, "FundingPanel"] = {}
            self._funding_source_kind: str = "rest"

        def on_start(self) -> None:
            self.subscribe_bars(self._bar_type)
            from ..config import OKXSettings
            self._rest_settings = OKXSettings()
            # 异步预填充 30d funding rate history（panel 模式下跳过 REST bootstrap）
            if self._funding_source_kind != "panel":
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._bootstrap_history())
                except RuntimeError:
                    self.log.warning("funding_skew: no running loop; skip history bootstrap")
            self.log.info(
                f"funding_skew start: inst={self._inst_id.value} "
                f"z_thr={self.config.z_threshold}σ history={self.config.history_periods} "
                f"source={self._funding_source_kind}"
            )

        def on_stop(self) -> None:
            self.log.info(f"funding_skew stop; active={self._active_direction}")

        def feed_funding_panel(self, panels: dict[str, "FundingPanel"]) -> None:
            """Inject pre-loaded funding panels + pre-fill rolling history deque.

            Replaces the async ``_bootstrap_history`` REST call in backtest mode.

            Args:
                panels: Mapping of inst_id (symbol, e.g. "BTC-USDT-SWAP") to
                    :class:`FundingPanel`. Only the panel whose key matches this
                    strategy's instrument symbol is consumed.
            """
            from collections import deque as _deque
            self._funding_panels = dict(panels)
            self._funding_source_kind = "panel"
            # This strategy is single-instrument; find the matching panel by symbol.
            symbol = self._inst_id.symbol.value  # e.g. "BTC-USDT-SWAP"
            panel = panels.get(symbol)
            if panel is None:
                return
            # Re-initialise the deque with the correct maxlen and pre-fill.
            maxlen = self.config.history_periods
            self._funding_history = _deque(maxlen=maxlen)
            for rate in panel.rates[-maxlen:]:
                self._funding_history.append(rate)

        async def _bootstrap_history(self) -> None:
            try:
                if self._rest is None:
                    from ..rest.client import OKXRestClient
                    self._rest = OKXRestClient(self._rest_settings)
                    await self._rest.__aenter__()
                history = await self._rest.public.get_funding_rate_history_extended(
                    self._inst_id.symbol.value,
                    total=self.config.history_periods,
                )
                for r in history:
                    self._funding_history.append(float(r.funding_rate))
                self.log.info(
                    f"funding_skew {self._inst_id.value}: bootstrapped "
                    f"{len(self._funding_history)} funding rate samples"
                )
            except Exception as exc:
                self.log.error(f"funding_skew bootstrap failed: {exc}")

        def on_bar(self, bar: Bar) -> None:
            if bar.bar_type != self._bar_type:
                return
            snap = to_bar_snapshots([bar])[0]
            self._bars.append(snap)
            self._latest_close = snap.close
            self._latest_ts_ms = snap.ts

            self._feed_risk_data(snap)

            # 出场检查（先于触发）
            if self._active_direction is not None:
                self._check_exit_on_bar(snap)
                return

            # 节流：每 check_interval_sec 拉一次 funding rate
            now_ns = self.clock.timestamp_ns()
            if now_ns - self._last_check_ts_ns < self.config.check_interval_sec * 1_000_000_000:
                return
            self._last_check_ts_ns = now_ns
            try:
                loop = asyncio.get_running_loop()
                self._check_task = loop.create_task(self._check_funding_async())
            except RuntimeError:
                self.log.warning("funding_skew: no running loop; skip funding check")

        def _feed_risk_data(self, snap: BarSnapshot) -> None:
            handles = self._risk_handles
            equity_usdt: float | None = None
            try:
                account = self.portfolio.account(self._inst_id.venue)
                if account is not None:
                    from nautilus_trader.model.currencies import USDT
                    bal = account.balance_total(USDT)
                    if bal is not None:
                        equity_usdt = float(bal.as_decimal())
            except Exception:
                equity_usdt = None
            if equity_usdt is not None:
                now_ms = int(time.time() * 1000)
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=now_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        async def _check_funding_async(self) -> None:
            try:
                if self._rest is None:
                    from ..rest.client import OKXRestClient
                    self._rest = OKXRestClient(self._rest_settings)
                    await self._rest.__aenter__()
                fr = await self._rest.public.get_funding_rate(self._inst_id.symbol.value)
                current = float(fr.funding_rate)
                history = list(self._funding_history)
                decision = funding_skew_decision(
                    current_funding=current,
                    history=history,
                    z_threshold=self.config.z_threshold,
                )
                self.log.info(
                    f"funding_skew {self._inst_id.value}: "
                    f"current={current:+.5%}/8h history_n={len(history)} "
                    f"decision={decision}"
                )
                # 把 current 加入 history（在 decision 之后避免污染本次判定）
                self._funding_history.append(current)
                if decision is not None and self._active_direction is None:
                    self._enter(decision)
            except Exception as exc:
                self.log.error(f"funding_skew check failed: {exc}")

        def _enter(self, direction: TradeDir) -> None:
            cfg: FundingSkewConfig = self.config  # type: ignore[assignment]
            inst = self.cache.instrument(self._inst_id)
            if inst is None or self._latest_close <= 0:
                return
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            lot_sz = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0

            atr = compute_atr(list(self._bars), period=cfg.atr_period)
            if atr <= 0:
                self.log.info(f"funding_skew: ATR=0, skip enter")
                return
            sl_dist = atr * cfg.atr_sl_mult
            entry = self._latest_close
            if direction == "long":
                stop = entry - sl_dist
                tp = entry + sl_dist * cfg.tp_rr_ratio
            else:
                stop = entry + sl_dist
                tp = entry - sl_dist * cfg.tp_rr_ratio

            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            contracts = position_contracts(
                account_equity_usdt=equity,
                risk_pct=cfg.risk_pct,
                entry_price=entry,
                stop_price=stop,
                ct_val=ct_val,
                min_sz=lot_sz,
                lot_sz=lot_sz,
            )
            if contracts <= 0:
                return

            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self._inst_id.value,
                direction=direction,
                size=contracts,
                entry_price=entry,
                stop_price=stop,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            contracts = adjusted

            qty_obj = safe_make_qty(inst, contracts, self.log, ctx=f"enter {self._inst_id.value}")
            if qty_obj is None:
                return
            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=self._inst_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
            )
            self.submit_order(order)
            self._active_direction = direction
            self._entry_price = entry
            self._stop_price = stop
            self._tp_price = tp
            self._position_contracts = contracts
            self._entry_ts_ms = self._latest_ts_ms
            self.log.info(
                f"ENTER {direction.upper()} {contracts}@{entry:.4f} "
                f"sl={stop:.4f} tp={tp:.4f} atr={atr:.4f}"
            )

        def _check_exit_on_bar(self, bar: BarSnapshot) -> None:
            if self._active_direction is None:
                return
            if self._active_direction == "long":
                hit_sl = bar.low <= self._stop_price
                hit_tp = bar.high >= self._tp_price
            else:
                hit_sl = bar.high >= self._stop_price
                hit_tp = bar.low <= self._tp_price
            if hit_sl or hit_tp:
                reason = "SL" if hit_sl else "TP"
                exit_price = self._stop_price if hit_sl else self._tp_price
                self._exit(reason, exit_price=exit_price, ts_ms=bar.ts)
                return
            # Timeout
            elapsed_ms = bar.ts - self._entry_ts_ms
            if elapsed_ms >= self.config.hold_max_hours * 3_600_000:
                self._exit("TIMEOUT", exit_price=bar.close, ts_ms=bar.ts)

        def _exit(
            self, reason: str, *, exit_price: float | None = None, ts_ms: int | None = None,
        ) -> None:
            if self._active_direction is None or self._position_contracts <= 0:
                return
            inst = self.cache.instrument(self._inst_id)
            if inst is None:
                return
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            direction = self._active_direction
            entry_price = self._entry_price
            contracts = self._position_contracts
            qty_obj = safe_make_qty(inst, contracts, ctx=f"exit {self._inst_id.value}")
            if qty_obj is None:
                return
            close_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
            order = self.order_factory.market(
                instrument_id=self._inst_id, order_side=close_side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )
            self.submit_order(order)
            self.log.info(
                f"EXIT {direction.upper()} reason={reason} qty={contracts}"
            )
            if exit_price is not None and ts_ms is not None:
                cfg: FundingSkewConfig = self.config  # type: ignore[assignment]
                record_strategy_trade(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    instrument_id=self._inst_id.value,
                    closed_ts_ms=ts_ms,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    contracts=contracts,
                    ct_val=ct_val,
                    risk_usdt=effective_equity_usdt(
                        self._allocated_equity_usdt, cfg.account_equity_usdt,
                    ) * cfg.risk_pct,
                )
            self._active_direction = None
            self._position_contracts = 0.0
            self._entry_ts_ms = 0

        def on_order_rejected(self, event) -> None:  # noqa: ANN001
            if self._active_direction is None and self._position_contracts == 0.0:
                return
            self.log.warning(
                f"funding_skew order rejected; clearing phantom state: "
                f"{getattr(event, 'reason', '?')}"
            )
            self._active_direction = None
            self._position_contracts = 0.0
            self._entry_ts_ms = 0


else:  # pragma: no cover

    class FundingSkewConfig:  # type: ignore[no-redef]
        pass

    class FundingSkewStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "FundingSkewConfig",
    "FundingSkewStrategy",
    "compute_atr",
    "funding_skew_decision",
]
