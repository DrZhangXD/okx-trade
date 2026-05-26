"""统计套利配对（Cointegration Pairs）。

直觉
----
某些 token 对存在长期协整关系（如 BTC-ETH、SOL-AVAX、LDO-RPL）：
两个价格序列各自非平稳（随机游走），但线性组合 spread = log(s1) - β·log(s2)
是平稳的。spread 偏离均值时反向（多 spread 低腿 + 空 spread 高腿）→ 收敛获利。

机制
----
- 预定义 pair universe（如 BTC-ETH, SOL-AVAX）
- 每个 pair 维护 60 天 1H bar 缓存
- 每 24h 用 Engle-Granger 测协整：拿到 hedge ratio β + ADF p-value + OU half-life
- 仅在 ``p < 0.05`` 时该 pair 处于"可交易"
- 每根 1H bar 算 spread_z = (current_spread - mean) / std
  - |z| > entry_threshold → 反向开 pair
  - |z| < exit_threshold → 平
  - |z| > stop_threshold → 强平止损
- 单仓位上限：每 pair risk_pct（默认 0.3%）

为什么 1H 而不是 daily？
- crypto 协整窗口短（regime change 频繁），1H 让 z-score 反应快
- daily 在熊市/牛市切换时 spread 会 reframe，1H 切回均值更频繁

风控
----
- neutral kind（β-hedged）；regime gate 关
- enable_drawdown 防一条腿被强平后裸方向
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from typing import TYPE_CHECKING

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from ..risk.stats import engle_granger_coint, zscore
from .base import effective_equity_usdt
from .pnl_hook import read_account_total_equity_usdt, record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


try:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    from ._okx_base import OkxStrategyBase

    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NT_AVAILABLE = False
    StrategyConfig = object  # type: ignore[assignment,misc]
    Strategy = object  # type: ignore[assignment,misc]


def compute_spread(
    log_s1: list[float], log_s2: list[float], hedge_ratio: float,
) -> list[float]:
    """spread_t = log(s1_t) - β·log(s2_t)。两条序列等长。"""
    n = min(len(log_s1), len(log_s2))
    return [log_s1[i] - hedge_ratio * log_s2[i] for i in range(n)]


if _NT_AVAILABLE:

    class StatArbConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Stat Arb Pairs NT Strategy 配置。

        Attributes:
            pair_left / pair_right: 两个 inst_id（默认 BTC-ETH 永续）。每个 strategy
                实例只交易一个 pair；多 pair 用多个实例。
            bar_type_template: ``"{inst}-1-HOUR-LAST-EXTERNAL"``。
            lookback_bars: 缓存 bar 数（默认 1440 = 60 天 × 24h）。
            coint_check_interval_h: 协整重测间隔（默认 24h）。
            coint_pvalue_threshold: p-value < 此值才视为协整可交易。
            spread_z_entry / spread_z_exit / spread_z_stop。
            risk_pct: 单 pair 风险占净值。
            account_equity_usdt: 起始净值。
            risk_config: 风控。
        """

        pair_left: str
        pair_right: str
        bar_type_template: str = "{inst}-1-HOUR-LAST-EXTERNAL"
        lookback_bars: int = 1440
        coint_check_interval_h: int = 24
        coint_pvalue_threshold: float = 0.05
        spread_z_entry: float = 2.0
        spread_z_exit: float = 0.5
        spread_z_stop: float = 3.5
        risk_pct: float = 0.003
        account_equity_usdt: float = 10000.0
        risk_config: RiskConfig | None = None
        # Live REST warmup: on_start fetches historical 1H bars so the strategy
        # can run coint check immediately instead of waiting weeks of live bars.
        # Disable for backtest (simulated clock + offline data).
        warmup_via_rest: bool = True
        # 2026-05-26 Phase 1c: isolated margin opt-in
        enable_isolated_margin: bool = False
        isolated_lever: int = 3


    class StatArbStrategy(OkxStrategyBase):  # type: ignore[misc]
        """单 pair 协整套利策略。

        状态：
        - ``self._closes_left/right``: deque of log close（用 log 让 β 表示弹性）
        - ``self._hedge_ratio``: 当前 β（每 24h 更新）
        - ``self._spread_mean / std``: 当前 spread 分布
        - ``self._position``: ``"long_spread" / "short_spread" / None``
        """

        def __init__(self, config: StatArbConfig) -> None:
            super().__init__(config)
            self._left_id = InstrumentId.from_str(config.pair_left)
            self._right_id = InstrumentId.from_str(config.pair_right)
            self._bar_type_left = BarType.from_str(
                config.bar_type_template.format(inst=config.pair_left)
            )
            self._bar_type_right = BarType.from_str(
                config.bar_type_template.format(inst=config.pair_right)
            )
            self._closes_left: deque[float] = deque(maxlen=config.lookback_bars)
            self._closes_right: deque[float] = deque(maxlen=config.lookback_bars)
            self._last_left_close: float = 0.0
            self._last_right_close: float = 0.0
            self._last_left_ts_ms: int = 0
            self._last_right_ts_ms: int = 0

            self._hedge_ratio: float = 0.0
            self._coint_pvalue: float = 1.0
            self._spread_history: list[float] = []
            self._last_coint_check_ms: int = 0

            self._position: str | None = None  # "long_spread" / "short_spread"
            self._left_contracts: float = 0.0
            self._right_contracts: float = 0.0
            self._entry_left_px: float = 0.0
            self._entry_right_px: float = 0.0
            self._entry_ts_ms: int = 0
            self._allocated_equity_usdt: float | None = None

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None
            self._warmup_task = None  # type: ignore[var-annotated]

        def on_start(self) -> None:
            self.subscribe_bars(self._bar_type_left)
            self.subscribe_bars(self._bar_type_right)
            self.log.info(
                f"stat_arb start: {self._left_id.value} vs {self._right_id.value} "
                f"entry_z={self.config.spread_z_entry} exit_z={self.config.spread_z_exit}"
            )
            if self.config.warmup_via_rest:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    self._warmup_task = loop.create_task(self._warmup_bars_via_rest())
                except RuntimeError as exc:
                    self.log.warning(
                        f"stat_arb: cannot spawn warmup task (no event loop): {exc}"
                    )

        def on_stop(self) -> None:
            if self._warmup_task is not None and not self._warmup_task.done():
                self._warmup_task.cancel()
                self._warmup_task = None
            self.log.info(f"stat_arb stop; position={self._position}")

        async def _warmup_bars_via_rest(self) -> None:
            """One-shot REST fetch of ``lookback_bars`` 1H closes into both deques.

            Without warmup, fresh-start service waits ~60 days for ``on_bar`` to
            accumulate enough history for engle_granger_coint (which needs n≥50).
            With warmup, the first coint check fires within ~30s of startup.

            Only pre-fills when the deque is empty (NT may already have replayed
            a few bars from its catalog). Sets ``_last_coint_check_ms`` to now so
            the next 24h interval check waits a full day.
            """
            from .. import OKXRestClient, OKXSettings

            n_needed = self.config.lookback_bars
            try:
                async with OKXRestClient(OKXSettings()) as client:
                    bare_left = self._left_id.value.split(".")[0]
                    bare_right = self._right_id.value.split(".")[0]
                    left_candles = await client.market.get_candles_extended(
                        bare_left, bar="1H", total=n_needed,
                    )
                    right_candles = await client.market.get_candles_extended(
                        bare_right, bar="1H", total=n_needed,
                    )
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"stat_arb warmup failed: {exc}")
                return

            # Clear + refill unconditionally. NT may have pushed a handful of
            # bars via on_bar during the 5s REST fetch (catalog replay); those
            # would defeat a "only fill if empty" guard, leaving the deque
            # with just those few bars while we discard 1440 historical ones.
            # Losing 5s of live bars is the better tradeoff.
            if left_candles:
                self._closes_left.clear()
                for c in left_candles:
                    close = float(c.close)
                    if close > 0:
                        self._closes_left.append(math.log(close))
                last = left_candles[-1]
                self._last_left_close = float(last.close)
                self._last_left_ts_ms = int(last.ts)
            if right_candles:
                self._closes_right.clear()
                for c in right_candles:
                    close = float(c.close)
                    if close > 0:
                        self._closes_right.append(math.log(close))
                last = right_candles[-1]
                self._last_right_close = float(last.close)
                self._last_right_ts_ms = int(last.ts)

            self._recompute_cointegration()
            self._last_coint_check_ms = int(time.time() * 1000)
            self.log.info(
                f"stat_arb warmup loaded: left={len(self._closes_left)} "
                f"right={len(self._closes_right)} β={self._hedge_ratio:.4f} "
                f"p={self._coint_pvalue:.4f} "
                f"tradeable={self._coint_pvalue < self.config.coint_pvalue_threshold}"
            )

        def on_bar(self, bar: Bar) -> None:
            close = bar.close.as_double()
            log_close = math.log(close) if close > 0 else 0.0
            ts_ms = int(bar.ts_event // 1_000_000)
            if bar.bar_type == self._bar_type_left:
                self._closes_left.append(log_close)
                self._last_left_close = close
                self._last_left_ts_ms = ts_ms
            elif bar.bar_type == self._bar_type_right:
                self._closes_right.append(log_close)
                self._last_right_close = close
                self._last_right_ts_ms = ts_ms
            else:
                return

            self._feed_risk_data()

            # 协整重测节流
            now_ms = int(time.time() * 1000)
            if (now_ms - self._last_coint_check_ms
                    >= self.config.coint_check_interval_h * 3_600_000):
                self._recompute_cointegration()
                self._last_coint_check_ms = now_ms

            # 判定信号 — enter/exit are async (isolated-margin two-phase commit);
            # dispatch via create_task from the sync NT on_bar boundary.
            if self._coint_pvalue >= self.config.coint_pvalue_threshold:
                # 当前 pair 不可交易；若有持仓就平
                if self._position is not None:
                    self._spawn_async(self._exit_pair(reason="COINT_BROKEN"))
                return

            self._spawn_async(self._evaluate_signal())

        def _spawn_async(self, coro) -> None:  # noqa: ANN001
            """Bridge sync NT on_bar → async enter/exit. Logs + drops on no loop."""
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                # No running loop (e.g., pure-sync backtest); close the coroutine
                # to avoid "coroutine was never awaited" warnings.
                coro.close()
                self.log.warning("stat_arb: no running loop; skip async dispatch")

        def _feed_risk_data(self) -> None:
            handles = self._risk_handles
            equity_usdt = read_account_total_equity_usdt(
                self, fallback_venue=self._left_id.venue,
            )
            if equity_usdt is not None:
                now_ms = int(time.time() * 1000)
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=now_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        def _recompute_cointegration(self) -> None:
            n = min(len(self._closes_left), len(self._closes_right))
            if n < 50:
                self._hedge_ratio = 0.0
                self._coint_pvalue = 1.0
                return
            left = list(self._closes_left)[-n:]
            right = list(self._closes_right)[-n:]
            beta, pvalue, hl = engle_granger_coint(left, right)
            self._hedge_ratio = beta
            self._coint_pvalue = pvalue
            self._spread_history = compute_spread(left, right, beta)
            self.log.info(
                f"stat_arb coint: β={beta:.4f} p={pvalue:.4f} hl={hl:.2f} "
                f"n={n} tradeable={pvalue < self.config.coint_pvalue_threshold}"
            )

        async def _evaluate_signal(self) -> None:
            if not self._spread_history or len(self._spread_history) < 30:
                return
            n = min(len(self._closes_left), len(self._closes_right))
            if n == 0:
                return
            cur_spread = (
                list(self._closes_left)[-1] - self._hedge_ratio * list(self._closes_right)[-1]
            )
            z = zscore(cur_spread, self._spread_history[-60:])

            # 出场
            if self._position == "long_spread":
                if z >= -self.config.spread_z_exit:
                    await self._exit_pair(reason="MEAN_REVERT")
                    return
                if z <= -self.config.spread_z_stop:
                    await self._exit_pair(reason="STOP")
                    return
            elif self._position == "short_spread":
                if z <= self.config.spread_z_exit:
                    await self._exit_pair(reason="MEAN_REVERT")
                    return
                if z >= self.config.spread_z_stop:
                    await self._exit_pair(reason="STOP")
                    return

            # 入场
            if self._position is None:
                if z >= self.config.spread_z_entry:
                    # spread 偏高 → 空 spread = 空 left + 多 right
                    await self._enter_pair(direction="short_spread")
                elif z <= -self.config.spread_z_entry:
                    await self._enter_pair(direction="long_spread")

        async def _enter_pair(self, direction: str) -> None:
            cfg: StatArbConfig = self.config  # type: ignore[assignment]
            left_inst = self.cache.instrument(self._left_id)
            right_inst = self.cache.instrument(self._right_id)
            if left_inst is None or right_inst is None:
                return
            if self._last_left_close <= 0 or self._last_right_close <= 0:
                return

            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            notional = equity * cfg.risk_pct * 10  # risk_pct ≈ size pct（pair 中性，单腿没有显式 SL）
            left_ct_val = float(left_inst.multiplier) if float(left_inst.multiplier) > 0 else 1.0
            right_ct_val = float(right_inst.multiplier) if float(right_inst.multiplier) > 0 else 1.0
            left_lot = float(left_inst.size_increment) if float(left_inst.size_increment) > 0 else 1.0
            right_lot = float(right_inst.size_increment) if float(right_inst.size_increment) > 0 else 1.0

            # left notional = β · right notional 让 β-hedged
            # 但 β 是 log-price 的弹性，转 notional 时近似为：right_notional = notional, left_notional = β · notional
            left_notional = notional * abs(self._hedge_ratio) if self._hedge_ratio != 0 else notional
            right_notional = notional
            left_contracts_raw = left_notional / (self._last_left_close * left_ct_val)
            right_contracts_raw = right_notional / (self._last_right_close * right_ct_val)
            left_contracts = (int(left_contracts_raw / left_lot) * left_lot) if left_lot > 0 else left_contracts_raw
            right_contracts = (int(right_contracts_raw / right_lot) * right_lot) if right_lot > 0 else right_contracts_raw

            if left_contracts <= 0 or right_contracts <= 0:
                return

            # 风控只走 left 腿（spread 策略 risk 在 pair 层）
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self._left_id.value,
                direction=("long" if direction == "long_spread" else "short"),
                size=left_contracts,
                entry_price=self._last_left_close,
                stop_price=self._last_left_close,  # neutral：无传统 stop
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            if adjusted != left_contracts and adjusted > 0:
                ratio = adjusted / left_contracts
                left_contracts = adjusted
                right_contracts = right_contracts * ratio

            left_qty = safe_make_qty(left_inst, left_contracts, self.log, ctx="enter left")
            right_qty = safe_make_qty(right_inst, right_contracts, self.log, ctx="enter right")
            if left_qty is None or right_qty is None:
                return

            if direction == "long_spread":
                # 多 spread = 多 left + 空 right
                left_side = OrderSide.BUY
                right_side = OrderSide.SELL
            else:
                left_side = OrderSide.SELL
                right_side = OrderSide.BUY

            left_order = self.order_factory.market(
                instrument_id=self._left_id, order_side=left_side,
                quantity=left_qty, time_in_force=TimeInForce.IOC,
            )
            right_order = self.order_factory.market(
                instrument_id=self._right_id, order_side=right_side,
                quantity=right_qty, time_in_force=TimeInForce.IOC,
            )

            # 2026-05-26 Phase 1c: pair entry is atomic — if isolated margin is
            # enabled, pre-validate set-leverage for BOTH legs before submitting
            # either order. If one fails we abort the whole pair to avoid a
            # directional residual (one leg filled, the other rejected).
            use_isolated = (
                cfg.enable_isolated_margin
                and self._iso_service is not None
                and not self._iso_service.is_backtest()
            )
            if use_isolated:
                pos_mode = await self._iso_service.get_pos_mode()
                from ..enums import PosSide
                if pos_mode == "long_short_mode":
                    left_pos_side: "PosSide | None" = (
                        PosSide.LONG if left_side == OrderSide.BUY else PosSide.SHORT
                    )
                    right_pos_side: "PosSide | None" = (
                        PosSide.LONG if right_side == OrderSide.BUY else PosSide.SHORT
                    )
                else:
                    left_pos_side = None
                    right_pos_side = None
                lever = cfg.isolated_lever
                left_inst_okx = self._left_id.value.split(".")[0]
                right_inst_okx = self._right_id.value.split(".")[0]
                result = await self._iso_service.batch_ensure_leverage([
                    (left_inst_okx, lever, left_pos_side),
                    (right_inst_okx, lever, right_pos_side),
                ])
                if not result.all_ok:
                    self.log.warning(
                        f"stat_arb ABORT pair entry: set-leverage failed for "
                        f"{[f[0] for f in result.failed]}; skipping both legs "
                        f"to avoid directional residual"
                    )
                    return  # leave self._position untouched (still None)

            # Submit both legs. submit_isolated_order cache-hits the
            # ensure_leverage call we just made (Phase 3 of two-phase commit),
            # and gracefully degrades to plain submit_order when isolated mode
            # is disabled / backtest / no service.
            left_submitted = await self.submit_isolated_order(
                left_order, lever=cfg.isolated_lever,
            )
            right_submitted = await self.submit_isolated_order(
                right_order, lever=cfg.isolated_lever,
            )
            if not (left_submitted and right_submitted):
                # Pre-validation should have caught this; defensive log only.
                # We don't try to unwind a partial fill here — on_order_rejected
                # clears _position state and live monitor will close residuals.
                self.log.warning(
                    f"stat_arb partial submit despite pre-validate "
                    f"(left={left_submitted} right={right_submitted}); "
                    f"position state not recorded"
                )
                return

            self._position = direction
            self._left_contracts = left_contracts
            self._right_contracts = right_contracts
            self._entry_left_px = self._last_left_close
            self._entry_right_px = self._last_right_close
            self._entry_ts_ms = self._last_left_ts_ms
            self.log.info(
                f"ENTER {direction} left={left_contracts}@{self._last_left_close:.4f} "
                f"right={right_contracts}@{self._last_right_close:.4f}"
            )

        async def _exit_pair(self, reason: str) -> None:
            if self._position is None:
                return
            left_inst = self.cache.instrument(self._left_id)
            right_inst = self.cache.instrument(self._right_id)
            if left_inst is None or right_inst is None:
                return
            left_qty = safe_make_qty(left_inst, self._left_contracts, ctx="exit left")
            right_qty = safe_make_qty(right_inst, self._right_contracts, ctx="exit right")
            if left_qty is None or right_qty is None:
                return

            if self._position == "long_spread":
                left_side = OrderSide.SELL
                right_side = OrderSide.BUY
            else:
                left_side = OrderSide.BUY
                right_side = OrderSide.SELL
            cfg: StatArbConfig = self.config  # type: ignore[assignment]
            left_order = self.order_factory.market(
                instrument_id=self._left_id, order_side=left_side,
                quantity=left_qty, time_in_force=TimeInForce.IOC, reduce_only=True,
            )
            right_order = self.order_factory.market(
                instrument_id=self._right_id, order_side=right_side,
                quantity=right_qty, time_in_force=TimeInForce.IOC, reduce_only=True,
            )
            # Reduce-only exits: route through submit_isolated_order for
            # consistency (cache-hits ensure_leverage; degrades to cross when
            # opt-in is off / backtest / no service). Two-phase validation is
            # unnecessary here — closing one leg even if the other rejects
            # only shrinks the residual, never creates one.
            await self.submit_isolated_order(left_order, lever=cfg.isolated_lever)
            await self.submit_isolated_order(right_order, lever=cfg.isolated_lever)
            self.log.info(
                f"EXIT {self._position} reason={reason} "
                f"left={self._left_contracts} right={self._right_contracts}"
            )

            # 记一笔 PnL（用 left 腿当代表）
            ts_now_ms = int(self.clock.timestamp_ns() // 1_000_000)
            ct_val = float(left_inst.multiplier) if float(left_inst.multiplier) > 0 else 1.0
            direction = "long" if self._position == "long_spread" else "short"
            record_strategy_trade(
                self._pnl_tracker,
                strategy_id=str(self.id),
                instrument_id=self._left_id.value,
                closed_ts_ms=ts_now_ms,
                direction=direction,  # type: ignore[arg-type]
                entry_price=self._entry_left_px,
                exit_price=self._last_left_close,
                contracts=self._left_contracts,
                ct_val=ct_val,
                risk_usdt=effective_equity_usdt(
                    self._allocated_equity_usdt, cfg.account_equity_usdt,
                ) * cfg.risk_pct,
                n_legs=4,  # left enter + right enter + left exit + right exit
            )
            self._position = None
            self._left_contracts = 0.0
            self._right_contracts = 0.0
            self._entry_ts_ms = 0

        def on_order_rejected(self, event) -> None:  # noqa: ANN001
            if self._position is None:
                return
            self.log.warning(
                f"stat_arb order rejected; clearing position state: "
                f"{getattr(event, 'reason', '?')}"
            )
            self._position = None
            self._left_contracts = 0.0
            self._right_contracts = 0.0
            self._entry_ts_ms = 0


else:  # pragma: no cover

    class StatArbConfig:  # type: ignore[no-redef]
        pass

    class StatArbStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "StatArbConfig",
    "StatArbStrategy",
    "compute_spread",
]
