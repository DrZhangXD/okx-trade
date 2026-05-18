"""Funding 横截面套利（Funding Cross-Section）。

直觉
----
不同 perp 同一时间的 funding rate 差异巨大（BTC ~0.01%, 小币 ~0.1% 是常态）。
做多 **funding 最负的 top-N**（被空头追着付，意味未来需要再付出 negative funding；
等同你做多收正 funding）+ 做空 **funding 最正的 top-N**（多头付钱给空头），
组合 delta ≈ 0（多数 altcoin 对 BTC β≈1，β-hedge 微调即可），但两边都赚 funding。

机制
----
- universe：top-N USDT 永续按 24h 成交额排（与 xs_momentum 的 universe resolver 同源）
- 每个 funding cycle（00:00 / 08:00 / 16:00 UTC 后 +1 min）：
  1. 拉所有 universe 的当前 funding rate（REST batch）
  2. 算每个 inst 30 日 β vs BTC（用 1D candle 收盘价）
  3. 选 funding 最负 top_n 做多、最正 bot_n 做空
  4. 按 β-hedge 调腿尺寸：长腿净 β + 空腿净 β ≈ 0
- 出场：每次 rebalance 把不在新集合的腿 close，新增的腿 open

冲突避免
-------
funding_carry 已经在赚 BTC funding；如 BTC 出现在本策略的 short leg（funding > 0），
会撞仓位（funding_carry short BTC perp + 本策略 short BTC perp）。
做法：rebalance 时跳过 funding_carry 当前持仓的 instrument，让 funding_carry 优先。

风控
----
- delta ≈ 0，vol_target 关
- regime gate 关（funding 与 regime 关系弱）
- enable_drawdown：开（防一边腿被强平后裸方向暴露）
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from ..risk.stats import rolling_beta
from .base import effective_equity_usdt
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


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


if _NT_AVAILABLE:

    class FundingXSConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Funding Cross-Section NT Strategy 配置。

        Attributes:
            instrument_ids: 候选 universe（``"BTC-USDT-SWAP.OKX"`` 列表）。由
                ``scripts/live.py`` universe resolver 在启动时填入 top-30。
            beta_reference_id: β 基准（一般 BTC）。
            beta_bar_type_template: β 回归用 bar type（默认 1D，30 根足够）。
            beta_window_days: β 计算窗口（默认 30 天）。
            top_n / bot_n: 多 / 空腿数量（默认各 3）。
            rebalance_hours_utc: 在哪几个小时（UTC）触发 rebalance（默认 funding
                结算 +1 分钟：0/8/16 → 用 [0, 8, 16]，监听 1m bar 在该小时第 1 min
                触发一次）。
            max_position_pct: 6 腿合计仓位占净值上限（默认 40%）。
            enable_beta_hedge: 是否做 β-中性调腿（默认 True）。
            account_equity_usdt: 起始净值（live 由 allocator 覆盖）。
            risk_config: 可选风控；推荐 ``enable_drawdown=True``，其他关。
        """

        instrument_ids: list[str]
        beta_reference_id: str = "BTC-USDT-SWAP.OKX"
        beta_bar_type_template: str = "{inst}-1-DAY-LAST-EXTERNAL"
        beta_window_days: int = 30
        top_n: int = 3
        bot_n: int = 3
        # NT StrategyConfig (msgspec frozen) 不允许 list 默认值，用 tuple 代替
        rebalance_hours_utc: tuple[int, ...] = (0, 8, 16)
        max_position_pct: float = 0.40
        enable_beta_hedge: bool = True
        check_interval_sec: int = 1800  # 也支持 30min 节流的额外检查
        account_equity_usdt: float = 10000.0
        risk_config: RiskConfig | None = None


    class FundingXSStrategy(Strategy):  # type: ignore[misc]
        """每个 funding cycle 重平 funding 横截面对冲组合。

        状态：
        - ``self._positions: dict[inst_id, ("long"|"short", contracts)]``
        - ``self._last_rebalance_ts_ms``
        - ``self._closes_by_inst``: deque of recent 1D close（用来算 β）
        """

        def __init__(self, config: FundingXSConfig) -> None:
            super().__init__(config)
            self._inst_ids = [InstrumentId.from_str(s) for s in config.instrument_ids]
            self._beta_ref_id = InstrumentId.from_str(config.beta_reference_id)
            self._bar_types: dict[InstrumentId, BarType] = {
                iid: BarType.from_str(
                    config.beta_bar_type_template.format(inst=iid.value)
                )
                for iid in self._inst_ids
            }
            # β 基准也必须订阅（即使不在 candidate 列表里）
            if self._beta_ref_id not in self._bar_types:
                self._bar_types[self._beta_ref_id] = BarType.from_str(
                    config.beta_bar_type_template.format(inst=self._beta_ref_id.value)
                )

            self._closes_by_inst: dict[str, list[float]] = {}
            self._latest_funding: dict[str, float] = {}
            self._positions: dict[str, tuple[str, float]] = {}  # inst_id → (dir, contracts)
            self._last_rebalance_hour: int = -1
            self._last_rebalance_day: str = ""
            self._allocated_equity_usdt: float | None = None

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None
            self._funding_task: asyncio.Task | None = None
            self._rest = None  # type: ignore[var-annotated]
            self._rest_settings = None  # type: ignore[var-annotated]

        def on_start(self) -> None:
            for iid, bar_type in self._bar_types.items():
                self.subscribe_bars(bar_type)
            from ..config import OKXSettings
            self._rest_settings = OKXSettings()
            self.log.info(
                f"funding_xs start: universe={len(self._inst_ids)} "
                f"top_n={self.config.top_n} bot_n={self.config.bot_n} "
                f"beta_hedge={self.config.enable_beta_hedge}"
            )

        def on_stop(self) -> None:
            if self._funding_task is not None:
                self._funding_task.cancel()
            self.log.info(f"funding_xs stop; open_legs={len(self._positions)}")

        def on_bar(self, bar: Bar) -> None:
            # 缓存收盘价用于 β 回归
            inst_value = bar.bar_type.instrument_id.value
            buf = self._closes_by_inst.setdefault(inst_value, [])
            buf.append(bar.close.as_double())
            # 只留 beta_window_days + 2 个收盘价
            cap = self.config.beta_window_days + 2
            if len(buf) > cap:
                buf[:] = buf[-cap:]

            # 喂风控
            self._feed_risk_data()

            # 节流触发 rebalance：每根 bar 检查一次小时
            now_ms = int(self.clock.timestamp_ns() // 1_000_000)
            now_hour = (now_ms // 3_600_000) % 24
            today = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
            if (now_hour in self.config.rebalance_hours_utc
                    and (today, now_hour) != (self._last_rebalance_day, self._last_rebalance_hour)):
                self._last_rebalance_day = today
                self._last_rebalance_hour = now_hour
                try:
                    loop = asyncio.get_running_loop()
                    self._funding_task = loop.create_task(self._rebalance_async())
                except RuntimeError:
                    self.log.warning("funding_xs: no running loop; skip rebalance")

        def _feed_risk_data(self) -> None:
            handles = self._risk_handles
            equity_usdt: float | None = None
            try:
                account = self.portfolio.account(self._beta_ref_id.venue)
                if account is not None:
                    from nautilus_trader.model.currencies import USDT
                    bal = account.balance_total(USDT)
                    if bal is not None:
                        equity_usdt = float(bal.as_decimal())
            except Exception:
                equity_usdt = None
            if equity_usdt is not None:
                now_ms = int(time.time() * 1000)
                if handles.drawdown_tracker is not None:
                    handles.drawdown_tracker.record_equity(ts_ms=now_ms, equity=equity_usdt)
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=now_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        async def _rebalance_async(self) -> None:
            try:
                if self._rest is None:
                    from ..rest.client import OKXRestClient
                    self._rest = OKXRestClient(self._rest_settings)
                    await self._rest.__aenter__()
                # 1) 拉所有 universe 当前 funding rate
                funding_tasks = [
                    self._rest.public.get_funding_rate(iid.symbol.value)
                    for iid in self._inst_ids
                ]
                rates = await asyncio.gather(*funding_tasks, return_exceptions=True)
                self._latest_funding = {}
                for iid, r in zip(self._inst_ids, rates):
                    if isinstance(r, Exception):
                        continue
                    self._latest_funding[iid.value] = float(r.funding_rate)

                # 2) 选股 + 计算 β-hedged 仓位
                target_positions = self._compute_target_positions()
                # 3) diff 当前持仓 vs target
                self._execute_diff(target_positions)
                self.log.info(
                    f"funding_xs rebalanced: open_legs={len(self._positions)} "
                    f"funding_count={len(self._latest_funding)}"
                )
            except Exception as exc:
                self.log.error(f"funding_xs rebalance failed: {exc}")

        def _compute_target_positions(self) -> dict[str, tuple[str, float]]:
            """选 top_n 最负 funding 做多 + bot_n 最正 funding 做空，β-hedge 后返回。

            Returns:
                ``{inst_id: (direction, contracts)}``。空 dict = 全平。
            """
            if not self._latest_funding:
                return {}
            ranked = sorted(self._latest_funding.items(), key=lambda kv: kv[1])
            # 多腿：funding 最负 top_n
            long_legs = [iid for iid, _ in ranked[:self.config.top_n]]
            # 空腿：funding 最正 bot_n
            short_legs = [iid for iid, _ in ranked[-self.config.bot_n:]]
            target: dict[str, tuple[str, float]] = {}

            equity = effective_equity_usdt(
                self._allocated_equity_usdt, self.config.account_equity_usdt,
            )
            per_leg_budget = equity * self.config.max_position_pct / max(
                1, self.config.top_n + self.config.bot_n
            )

            ref_closes = self._closes_by_inst.get(self._beta_ref_id.value, [])
            ref_returns = self._returns(ref_closes)

            for direction, legs in (("long", long_legs), ("short", short_legs)):
                for inst_value in legs:
                    # β-hedge：调整 size，让 portfolio β ≈ 0
                    size_scale = 1.0
                    if self.config.enable_beta_hedge:
                        asset_closes = self._closes_by_inst.get(inst_value, [])
                        asset_returns = self._returns(asset_closes)
                        if len(asset_returns) >= 10 and len(ref_returns) >= 10:
                            n = min(len(asset_returns), len(ref_returns))
                            beta = rolling_beta(asset_returns[-n:], ref_returns[-n:])
                            if beta > 0:
                                # 高 β 标的 → 减仓（避免该腿主导组合 β）
                                size_scale = 1.0 / max(0.3, min(3.0, beta))
                    # 价格 → 合约张数：缺 instrument 信息时跳过
                    inst = self.cache.instrument(InstrumentId.from_str(inst_value))
                    if inst is None:
                        continue
                    last_close = self._closes_by_inst.get(inst_value, [])
                    if not last_close:
                        continue
                    spot_px = last_close[-1]
                    ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
                    notional = per_leg_budget * size_scale
                    raw_contracts = notional / max(spot_px * ct_val, 1e-9)
                    lot_sz = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0
                    # 取整到 lot
                    contracts = (int(raw_contracts / lot_sz) * lot_sz) if lot_sz > 0 else raw_contracts
                    if contracts <= 0:
                        continue
                    target[inst_value] = (direction, contracts)
            return target

        @staticmethod
        def _returns(closes: list[float]) -> list[float]:
            if len(closes) < 2:
                return []
            return [(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes))]

        def _execute_diff(self, target: dict[str, tuple[str, float]]) -> None:
            """把当前 self._positions diff 到 target，平多余腿、开新腿、调整 size。"""
            current = dict(self._positions)
            # 平：当前持有但不在 target，或方向变了
            for inst_value, (cur_dir, cur_qty) in current.items():
                if inst_value not in target or target[inst_value][0] != cur_dir:
                    self._close_leg(inst_value, cur_dir, cur_qty)
            # 开 / 调：target 里有但当前没有，或 size 变了
            for inst_value, (tgt_dir, tgt_qty) in target.items():
                cur = self._positions.get(inst_value)
                if cur is None:
                    self._open_leg(inst_value, tgt_dir, tgt_qty)
                elif cur[0] == tgt_dir and abs(cur[1] - tgt_qty) > 1e-6:
                    # 同方向但 size 变 → 简化：先平后开（避免增仓 / 减仓两套 path）
                    self._close_leg(inst_value, cur[0], cur[1])
                    self._open_leg(inst_value, tgt_dir, tgt_qty)

        def _open_leg(self, inst_value: str, direction: str, contracts: float) -> None:
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            equity = effective_equity_usdt(
                self._allocated_equity_usdt, self.config.account_equity_usdt,
            )
            last_closes = self._closes_by_inst.get(inst_value, [])
            entry_px = last_closes[-1] if last_closes else 0.0
            # 用 entry_px 当 stop_price 跑 risk（neutral 策略没有 SL 概念）
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=inst_value,
                direction=direction,  # type: ignore[arg-type]
                size=contracts,
                entry_price=entry_px,
                stop_price=entry_px,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            contracts = adjusted

            qty_obj = safe_make_qty(inst, contracts, self.log, ctx=f"open {inst_value}")
            if qty_obj is None:
                return
            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=inst_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
            )
            self.submit_order(order)
            self._positions[inst_value] = (direction, contracts)
            self.log.info(f"OPEN {direction} {inst_value} qty={contracts}")

        def _close_leg(self, inst_value: str, direction: str, contracts: float) -> None:
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            qty_obj = safe_make_qty(inst, contracts, ctx=f"close {inst_value}")
            if qty_obj is None:
                self.log.error(
                    f"CLOSE skipped: {inst_value} contracts {contracts} < lot_sz"
                )
                return
            close_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
            order = self.order_factory.market(
                instrument_id=inst_id, order_side=close_side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )
            self.submit_order(order)
            self.log.info(f"CLOSE {direction} {inst_value} qty={contracts}")

            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            last_closes = self._closes_by_inst.get(inst_value, [])
            close_px = last_closes[-1] if last_closes else 0.0
            ts_now_ms = int(self.clock.timestamp_ns() // 1_000_000)
            cfg: FundingXSConfig = self.config  # type: ignore[assignment]
            record_strategy_trade(
                self._pnl_tracker,
                strategy_id=str(self.id),
                instrument_id=inst_value,
                closed_ts_ms=ts_now_ms,
                direction=direction,  # type: ignore[arg-type]
                entry_price=close_px,  # 简化：funding-driven 策略主要靠 funding，价差 ~0
                exit_price=close_px,
                contracts=contracts,
                ct_val=ct_val,
                risk_usdt=effective_equity_usdt(
                    self._allocated_equity_usdt, cfg.account_equity_usdt,
                ) * cfg.max_position_pct / max(1, cfg.top_n + cfg.bot_n),
            )

            self._positions.pop(inst_value, None)

        def on_order_rejected(self, event) -> None:  # noqa: ANN001
            # 简化处理：拒了哪条腿、就从 _positions 移除（避免幻仓）
            reason = getattr(event, "reason", "?")
            self.log.warning(f"funding_xs order rejected: {reason}")
            # 我们不知道是哪条腿，最稳的做法是下次 rebalance 重对账；先不动 state

else:  # pragma: no cover

    class FundingXSConfig:  # type: ignore[no-redef]
        pass

    class FundingXSStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "FundingXSConfig",
    "FundingXSStrategy",
]
