"""期现套利策略（Basis Cash-and-Carry）。

直觉
----
OKX 同一标的存在三种合约：spot、永续 SWAP、交割合约 FUTURES（季度 / 当周 / 次周）。
当 ``futures_price > spot_price`` 时 basis 为正（contango），交割时强制收敛——
意味着 **买入现货 + 卖空交割合约** 可锁定 basis 收益，到期自动平仓。

为什么比 ``funding_carry`` 更稳？
- ``funding`` 是浮动费率，方向可能反转；basis 在结算前数学锁死，**期货必须收敛到 spot**。
- 持仓期内 funding 累计为不确定值，basis 是确定值（前提是合约不违约 / OKX 不被攻击）。
- 但 carry 容量更小、入场/出场摩擦更高。

机制
----
- **入场**：``annualized_basis(F, S, days_to_expiry) ≥ entry_basis_apr`` → 同时下
  spot BUY + futures SELL，两腿等 USDT notional；
- **持仓**：什么都不做，让 basis 自然收敛（``futures_price`` 向 ``spot_price`` 靠近）；
- **平仓**：``basis ≤ exit_basis_apr`` 或 ``days_to_expiry < force_close_days_before_expiry``，
  同时下 spot SELL + futures BUY。

为什么不用 OKX cross-margin？
- 现货账户 + futures 账户分开记账更直观；
- ``net mode`` 下 futures 短腿就是常规的卖出开仓 + 买入平仓（``reduce_only=True``）；
- ``long_short mode`` 下走 ``posSide=short`` 也支持，配置时声明即可。

风控
----
- delta ≈ 0，``vol_target`` 没意义（关掉）；
- drawdown 仍然开（防 OKX 异常事件导致一腿被强平→裸方向暴露）；
- correlation 关掉（basis 与方向不相关）。
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from ._signals import BasisAction, BasisArbParams, annualized_basis, basis_decision
from .base import effective_equity_usdt
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar
    from ..backtest.funding_data import FundingPanel


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


def _carry_position_size(
    *,
    account_equity_usdt: float,
    max_position_pct: float,
    spot_price: float,
    futures_ct_val: float,
    futures_lot_sz: float,
) -> tuple[float, float]:
    """同时算 spot 数量 + futures 张数，使两腿 USDT notional 相等。

    返回 ``(spot_qty_in_base_ccy, futures_contracts)``。futures 张数离散化下取整后，
    spot 数量按 ``perp_contracts × ct_val`` 反推对齐。
    """
    if spot_price <= 0 or futures_ct_val <= 0 or account_equity_usdt <= 0:
        return 0.0, 0.0
    budget = account_equity_usdt * max_position_pct
    spot_qty = budget / spot_price
    raw_contracts = spot_qty / futures_ct_val
    step = futures_lot_sz if futures_lot_sz > 0 else 1.0
    from decimal import ROUND_DOWN
    step_d = Decimal(str(step))
    raw_d = Decimal(str(raw_contracts))
    futures_contracts = float(
        (raw_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    )
    if futures_contracts > 0:
        spot_qty_adj = futures_contracts * futures_ct_val
        return spot_qty_adj, futures_contracts
    return 0.0, 0.0


if _NT_AVAILABLE:

    class BasisArbConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Basis Arb NT Strategy 配置。

        Attributes:
            spot_instrument_id: 现货 NT id（``"BTC-USDT.OKX"``）。
            futures_instrument_id: 交割合约 NT id（``"BTC-USDT-251226.OKX"``）。
            spot_bar_type: 用来跟踪 spot 价格 + 喂风控的 bar type。
            entry_basis_apr: 年化 basis ≥ 此值入场（默认 12%）。
            exit_basis_apr: ≤ 此值平仓（默认 2%）。
            force_close_days_before_expiry: 距到期 < 此天数强制平仓（默认 2 天）。
            max_position_pct: 单次开仓占净值比例（默认 30%）。
            check_interval_sec: 检查 basis 间隔（默认 3600s = 1h）。
            account_equity_usdt: 回测虚拟净值；实盘从 cache 读。
            risk_config: 可选风控；推荐 ``enable_drawdown=True``，其余关。
        """

        spot_instrument_id: str
        futures_instrument_id: str
        spot_bar_type: str
        entry_basis_apr: float = 0.12
        exit_basis_apr: float = 0.02
        force_close_days_before_expiry: int = 2
        max_position_pct: float = 0.30
        check_interval_sec: int = 3600
        account_equity_usdt: float = 10000.0
        risk_config: RiskConfig | None = None
        # Per-strategy td_mode override (futures leg only; SPOT always CASH).
        # 2026-05-20: default "isolated" so the futures short leg has its own
        # margin pool — if it gets liquidated (5/19 sub_type=6 incident), the
        # loss is capped at the isolated margin, the spot long leg's USDT
        # collateral is untouched, and no other strategy's margin is bled.
        # Set to "cross" to revert to account-wide pooling.
        td_mode_override: str = "isolated"


    class BasisArbStrategy(Strategy):  # type: ignore[misc]
        """期现套利：定时拉 spot + futures ticker，命中 basis 阈值开/平 carry 组合。

        状态机：``IDLE`` ↔ ``LONG_CARRY``（spot 多 + futures 空）。

        实现细节：
        - **数据来源**：策略层自持 ``OKXRestClient``，定时 ticker（不走 NT cache 因为
          NT 不会自动给 FUTURES instrument 喂 ticker，需要自己 poll）；
        - **execution**：通过 NT ``submit_order``，由 ``OKXLiveExecutionClient`` 翻译为
          OKX 订单；两腿同时下，假设近同时成交；
        - **expiry 强平**：从 FUTURES instrument 的 ``info["okx_exp_time_ms"]`` 读到期，
          每次 check 时算 ``days_to_expiry``。
        """

        def __init__(self, config: BasisArbConfig) -> None:
            super().__init__(config)
            self.spot_id = InstrumentId.from_str(config.spot_instrument_id)
            self.futures_id = InstrumentId.from_str(config.futures_instrument_id)
            self.spot_bar_type = BarType.from_str(config.spot_bar_type)
            self._params = BasisArbParams(
                entry_basis_apr=config.entry_basis_apr,
                exit_basis_apr=config.exit_basis_apr,
                force_close_days_before_expiry=config.force_close_days_before_expiry,
            )

            self._has_position: bool = False
            self._allocated_equity_usdt: float | None = None
            self._spot_qty: float = 0.0
            self._futures_contracts: float = 0.0
            self._latest_spot_price: float = 0.0
            self._last_check_ts_ns: int = 0
            self._check_task: asyncio.Task | None = None
            self._rest = None  # type: ignore[var-annotated]
            self._rest_settings = None  # type: ignore[var-annotated]
            # Futures leg td_mode override tag (consumed by OKX adapter).
            # SPOT leg ignores it (OKX forces CASH); tag is harmless there.
            self._futures_tags: list[str] | None = (
                [f"td_mode:{config.td_mode_override}"]
                if config.td_mode_override else None
            )

            self._funding_panel: "FundingPanel | None" = None

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._prev_spot_close: float | None = None

            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None
            self._entry_ts_ms: int = 0
            self._entry_spot_price: float = 0.0
            self._entry_futures_price: float = 0.0

        def on_start(self) -> None:
            self.subscribe_bars(self.spot_bar_type)
            from ..config import OKXSettings
            self._rest_settings = OKXSettings()
            self.log.info(
                f"basis_arb start: entry={self._params.entry_basis_apr:.1%} "
                f"exit={self._params.exit_basis_apr:.1%} "
                f"futures={self.futures_id.value}"
            )

        def on_bar(self, bar: Bar) -> None:
            if bar.bar_type != self.spot_bar_type:
                return
            close = bar.close.as_double()
            self._latest_spot_price = close
            self._feed_risk_data(ts_ms=int(bar.ts_event // 1_000_000), close=close)

            now_ns = self.clock.timestamp_ns()
            if now_ns - self._last_check_ts_ns < self.config.check_interval_sec * 1_000_000_000:
                return
            self._last_check_ts_ns = now_ns
            try:
                loop = asyncio.get_running_loop()
                self._check_task = loop.create_task(self._check_basis_async())
            except RuntimeError:
                self.log.warning("basis_arb: no running loop; skip basis check")

        def on_stop(self) -> None:
            self.log.info(f"basis_arb stop; has_position={self._has_position}")

        def feed_funding_panel(self, panel: "FundingPanel") -> None:
            """Optional funding-context hook for backtest. Stored but not yet used in
            entry decision (reserved for future regime-filter wiring).
            """
            self._funding_panel = panel

        def _feed_risk_data(self, ts_ms: int, close: float) -> None:
            handles = self._risk_handles
            spot_id_str = self.spot_id.value
            if handles.vol_target is not None and self._prev_spot_close is not None \
                    and self._prev_spot_close > 0:
                ret = close / self._prev_spot_close - 1.0
                handles.vol_target.update_return(spot_id_str, ret)
            self._prev_spot_close = close

            equity_usdt: float | None = None
            try:
                account = self.portfolio.account(self.spot_id.venue)
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

        def _days_to_expiry(self) -> float:
            """从 FUTURES instrument 的 ``info["okx_exp_time_ms"]`` 读到期。"""
            inst = self.cache.instrument(self.futures_id)
            if inst is None:
                return 0.0
            info = getattr(inst, "info", {}) or {}
            exp_ms = int(info.get("okx_exp_time_ms", 0) or 0)
            if exp_ms <= 0:
                return 0.0
            now_ms = int(time.time() * 1000)
            return max(0.0, (exp_ms - now_ms) / (1000 * 86400))

        async def _check_basis_async(self) -> None:
            """异步拉 spot + futures ticker 并按 decision 执行。"""
            try:
                if self._rest is None:
                    from ..rest.client import OKXRestClient
                    self._rest = OKXRestClient(self._rest_settings)
                    await self._rest.__aenter__()

                spot_inst_id_str = self.spot_id.symbol.value
                futures_inst_id_str = self.futures_id.symbol.value
                spot_ticker, futures_ticker = await asyncio.gather(
                    self._rest.market.get_ticker(spot_inst_id_str),
                    self._rest.market.get_ticker(futures_inst_id_str),
                )
                spot_px = float(spot_ticker.last)
                futures_px = float(futures_ticker.last)
                dte = self._days_to_expiry()
                ann_basis = annualized_basis(futures_px, spot_px, dte)
                action = basis_decision(ann_basis, dte, self._has_position, self._params)
                self.log.info(
                    f"basis: spot={spot_px:.4f} futures={futures_px:.4f} "
                    f"ann_basis={ann_basis:+.2%} dte={dte:.1f}d "
                    f"action={action.value} pos={self._has_position}"
                )
                if action == BasisAction.ENTER:
                    self._enter_carry(spot_px, futures_px)
                elif action == BasisAction.EXIT:
                    self._exit_carry()
            except Exception as exc:
                self.log.error(f"basis check failed: {exc}")

        def _enter_carry(self, spot_px: float, futures_px: float) -> None:
            spot_inst = self.cache.instrument(self.spot_id)
            futures_inst = self.cache.instrument(self.futures_id)
            if spot_inst is None or futures_inst is None or spot_px <= 0:
                self.log.warning("missing instrument or price; skip enter")
                return

            ct_val = float(futures_inst.multiplier) if float(futures_inst.multiplier) > 0 else 1.0
            equity = effective_equity_usdt(
                self._allocated_equity_usdt, self.config.account_equity_usdt,
            )
            spot_qty, futures_contracts = _carry_position_size(
                account_equity_usdt=equity,
                max_position_pct=self.config.max_position_pct,
                spot_price=spot_px,
                futures_ct_val=ct_val,
                futures_lot_sz=float(futures_inst.size_increment),
            )
            if spot_qty <= 0 or futures_contracts <= 0:
                self.log.info("size=0; skip enter")
                return

            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self.futures_id.value,
                direction="short",
                size=futures_contracts,
                entry_price=futures_px,
                stop_price=futures_px,  # delta-neutral, no traditional stop
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None:
                return
            if adjusted != futures_contracts and adjusted > 0:
                ratio = adjusted / futures_contracts
                futures_contracts = adjusted
                spot_qty = spot_qty * ratio

            spot_qty_obj = safe_make_qty(
                spot_inst, spot_qty, self.log, ctx=f"enter spot {self.spot_id.value}"
            )
            futures_qty_obj = safe_make_qty(
                futures_inst, futures_contracts, self.log,
                ctx=f"enter futures {self.futures_id.value}",
            )
            if spot_qty_obj is None or futures_qty_obj is None:
                return
            spot_order = self.order_factory.market(
                instrument_id=self.spot_id,
                order_side=OrderSide.BUY,
                quantity=spot_qty_obj,
                time_in_force=TimeInForce.IOC,
            )
            futures_order = self.order_factory.market(
                instrument_id=self.futures_id,
                order_side=OrderSide.SELL,
                quantity=futures_qty_obj,
                time_in_force=TimeInForce.IOC,
                tags=self._futures_tags,
            )
            self.submit_order(spot_order)
            self.submit_order(futures_order)

            self._has_position = True
            self._spot_qty = spot_qty
            self._futures_contracts = futures_contracts
            self._entry_ts_ms = int(self.clock.timestamp_ns() // 1_000_000)
            self._entry_spot_price = spot_px
            self._entry_futures_price = futures_px
            self.log.info(
                f"ENTER carry: spot={spot_qty}@{spot_px:.4f} "
                f"futures={futures_contracts}@{futures_px:.4f}"
            )

        def _exit_carry(self) -> None:
            if not self._has_position:
                return
            spot_inst = self.cache.instrument(self.spot_id)
            futures_inst = self.cache.instrument(self.futures_id)
            if spot_inst is None or futures_inst is None:
                return

            spot_qty_obj = safe_make_qty(
                spot_inst, self._spot_qty, ctx=f"exit spot {self.spot_id.value}"
            )
            futures_qty_obj = safe_make_qty(
                futures_inst, self._futures_contracts,
                ctx=f"exit futures {self.futures_id.value}",
            )
            if spot_qty_obj is None or futures_qty_obj is None:
                self.log.error(
                    f"EXIT carry skipped: leg < lot_sz "
                    f"(spot={self._spot_qty}, futures={self._futures_contracts}); "
                    f"position remains open"
                )
                return
            spot_close = self.order_factory.market(
                instrument_id=self.spot_id,
                order_side=OrderSide.SELL,
                quantity=spot_qty_obj,
                time_in_force=TimeInForce.IOC,
                reduce_only=False,
            )
            futures_close = self.order_factory.market(
                instrument_id=self.futures_id,
                order_side=OrderSide.BUY,
                quantity=futures_qty_obj,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
                tags=self._futures_tags,
            )
            self.submit_order(spot_close)
            self.submit_order(futures_close)
            self.log.info(
                f"EXIT carry: spot={self._spot_qty} futures={self._futures_contracts}"
            )

            cfg: BasisArbConfig = self.config  # type: ignore[assignment]
            ts_now_ms = int(self.clock.timestamp_ns() // 1_000_000)
            record_strategy_trade(
                self._pnl_tracker,
                strategy_id=str(self.id),
                instrument_id=self.futures_id.value,
                closed_ts_ms=ts_now_ms,
                direction="short",
                entry_price=self._entry_futures_price,
                exit_price=self._latest_spot_price,
                contracts=self._futures_contracts,
                ct_val=float(futures_inst.multiplier) if float(futures_inst.multiplier) > 0 else 1.0,
                risk_usdt=effective_equity_usdt(
                    self._allocated_equity_usdt, cfg.account_equity_usdt,
                ) * cfg.max_position_pct,
                n_legs=4,  # spot enter + futures enter + spot exit + futures exit
            )

            self._has_position = False
            self._spot_qty = 0.0
            self._futures_contracts = 0.0
            self._entry_ts_ms = 0
            self._entry_spot_price = 0.0
            self._entry_futures_price = 0.0

else:  # pragma: no cover
    class BasisArbConfig:  # type: ignore[no-redef]
        pass

    class BasisArbStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "BasisArbConfig",
    "BasisArbStrategy",
]
