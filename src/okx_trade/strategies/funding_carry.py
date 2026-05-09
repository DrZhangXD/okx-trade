"""资金费率 Cash-and-Carry（delta-neutral）策略。

直觉
----
永续合约的资金费率每 8 小时结算一次：
- 多头付给空头，当 ``funding > 0``；
- 空头付给多头，当 ``funding < 0``。

如果 ``funding`` 持续为正且数额可观（如 0.01% / 8h ≈ 11% APR），可以构造**delta-neutral**
组合赚取 funding：

    买入 $X 现货 BTC（``BTC-USDT``）+ 做空 $X 等值永续 BTC（``BTC-USDT-SWAP``）

价格涨跌互相对冲（delta=0），盈亏 = ``Σ(-funding_rate × notional)`` —— 持有时段累计的
funding 费率。Funding 转负或回归正常时退出，把腿对冲平仓。

为什么个人盘也能赚
----------------
- 不需要做市权限；
- 不需要预测方向，只押 funding 方向（远比预测涨跌简单）；
- $10K-$100K 容量下，BTC/ETH 流动性绰绰有余，没滑点烦恼；
- 实证：2022-2024 年化 5-30%（高时段集中在 bull market 顶部 + bear market 底部）。

设计
----
- **纯函数 ``funding_carry_decision``**：输入当前 funding rate + 持仓状态 + 阈值，
  输出动作 enum（``ENTER`` / ``EXIT`` / ``HOLD``）。便于回测和单测。
- **NT Strategy 包装**：定时从 REST 拉 funding rate（每 30 分钟 1 次足够，因为
  每 8h 结算），命中阈值时同时下两腿单。

为什么不用 WS funding-rate 频道？
- 频道更新非常稀疏（每 8h 一次新值，平时只是重复推），REST 轮询更简单；
- 策略层无延迟敏感度（funding 是慢变量）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from .pnl_hook import record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


class CarryAction(str, Enum):
    """funding_carry_decision 的输出。"""

    HOLD = "hold"   # 维持当前状态（无仓 / 已持仓）
    ENTER = "enter"  # 开仓（多 spot + 空 perp）
    EXIT = "exit"   # 平仓（卖 spot + 买回 perp）


@dataclass(frozen=True, slots=True)
class FundingCarryParams:
    """策略参数。

    Attributes:
        entry_apr_threshold: 估算 APR 高于此阈值才开仓（默认 8% APR）。
        exit_apr_threshold: 估算 APR 低于此阈值则平仓（默认 2% APR）。
        max_position_pct: 单次开仓占总资金比例（默认 30%——因为是基本盘策略，
            可以分配更多权重）。
    """

    entry_apr_threshold: float = 0.08
    exit_apr_threshold: float = 0.02
    max_position_pct: float = 0.30


def funding_carry_decision(
    funding_rate_8h: float,
    has_position: bool,
    params: FundingCarryParams,
) -> CarryAction:
    """根据当前 funding rate 决定动作。

    Args:
        funding_rate_8h: OKX 8h 周期 funding rate（如 0.0001 = 0.01%/8h）。
        has_position: 当前是否已持有 spot+perp delta-neutral 组合。
        params: 策略参数。

    Returns:
        ``CarryAction`` 枚举。
    """
    apr = funding_rate_8h * 3 * 365  # 8h × 3 = 24h；× 365 = year

    if has_position:
        # 已持仓：APR 跌破 exit 才平仓
        if apr < params.exit_apr_threshold:
            return CarryAction.EXIT
        return CarryAction.HOLD

    # 空仓：APR 高于 entry 才开仓
    if apr >= params.entry_apr_threshold:
        return CarryAction.ENTER
    return CarryAction.HOLD


def carry_position_size(
    *,
    account_equity_usdt: float,
    max_position_pct: float,
    spot_price: float,
    perp_ct_val: float,
    perp_lot_sz: float,
) -> tuple[float, float]:
    """同时算 spot 数量 + perp 张数，使两腿 USDT notional 相等（delta-neutral）。

    Returns:
        ``(spot_qty_in_base_ccy, perp_contracts)``。两腿的 USDT 名义值相等。

        ``spot_qty`` = ``budget / spot_price``
        ``perp_contracts`` = ``floor((spot_qty / perp_ct_val) / perp_lot_sz) * perp_lot_sz``

    后两腿名义可能略有差异（因为合约是离散的），策略接受这个微小 delta。
    """
    if spot_price <= 0 or perp_ct_val <= 0 or account_equity_usdt <= 0:
        return 0.0, 0.0
    budget = account_equity_usdt * max_position_pct
    spot_qty = budget / spot_price
    raw_contracts = spot_qty / perp_ct_val
    step = perp_lot_sz if perp_lot_sz > 0 else 1.0
    step_d = Decimal(str(step))
    raw_d = Decimal(str(raw_contracts))
    from decimal import ROUND_DOWN
    perp_contracts = float(
        (raw_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    )
    # 精确把 spot_qty 调整到与 perp 等 notional（避免 delta 偏差）
    if perp_contracts > 0:
        spot_qty_adjusted = perp_contracts * perp_ct_val
        return spot_qty_adjusted, perp_contracts
    return 0.0, 0.0


# ---------------------------------------------------------------------------
# NautilusTrader Strategy wrapper
# ---------------------------------------------------------------------------


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

    class FundingCarryConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Funding Cash-and-Carry NT Strategy 配置。

        Attributes:
            spot_instrument_id: 现货 NT id（如 ``"BTC-USDT.OKX"``）。
            perp_instrument_id: 永续 NT id（如 ``"BTC-USDT-SWAP.OKX"``）。
            spot_bar_type: 用来跟踪现货价格的 bar type（必填，做触发器和 sizing）。
            entry_apr_threshold: 见 ``FundingCarryParams``。
            exit_apr_threshold: 见 ``FundingCarryParams``。
            max_position_pct: 见 ``FundingCarryParams``。
            account_equity_usdt: 回测用净值；实盘留 0 → 从 cache 读。
            check_interval_sec: 检查 funding rate 的间隔（默认 1800s = 30 分钟）。
        """

        spot_instrument_id: str
        perp_instrument_id: str
        spot_bar_type: str
        entry_apr_threshold: float = 0.08
        exit_apr_threshold: float = 0.02
        max_position_pct: float = 0.30
        account_equity_usdt: float = 10000.0
        check_interval_sec: int = 1800
        # M3.6 风控（None → 透明跳过；推荐 enable_drawdown=True，不开 vol_target——
        # delta-neutral 仓位的 vol≈0，vol_target 没意义）
        risk_config: RiskConfig | None = None

    class FundingCarryStrategy(Strategy):  # type: ignore[misc]
        """Funding cash-and-carry：定时拉 funding rate，命中阈值开/平 delta-neutral 组合。

        实现细节：
        - **funding 数据来源**：策略层直接复用 ``OKXRestClient``（自己启动一个轻量
          REST 客户端，不依赖 adapter），定时调用 ``public.get_funding_rate(perp_id)``。
        - **执行**：通过 NT 的 ``submit_order`` 走标准 exec 路径，会被
          ``OKXLiveExecutionClient`` 翻译为 OKX 订单。两腿同时下，假设几乎同时成交。
        - **状态机**：``IDLE``（无仓）↔ ``LONG_CARRY``（持有 spot 多 + perp 空）。
          slippage / 部分成交等异常情况留 M3 风控层处理。
        """

        def __init__(self, config: FundingCarryConfig) -> None:
            super().__init__(config)
            # NT Strategy 是 Cython 类，self.config 已由父类设置；不能再赋值
            self.spot_id = InstrumentId.from_str(config.spot_instrument_id)
            self.perp_id = InstrumentId.from_str(config.perp_instrument_id)
            self.spot_bar_type = BarType.from_str(config.spot_bar_type)
            self._params = FundingCarryParams(
                entry_apr_threshold=config.entry_apr_threshold,
                exit_apr_threshold=config.exit_apr_threshold,
                max_position_pct=config.max_position_pct,
            )

            self._has_position: bool = False
            self._spot_qty: float = 0.0
            self._perp_contracts: float = 0.0
            self._latest_spot_price: float = 0.0
            self._last_check_ts_ns: int = 0
            self._check_task: asyncio.Task | None = None
            # 可注入的 funding-rate 取数函数（测试时用 stub）；默认走 REST
            self._funding_fetcher = None  # 由 on_start 初始化

            # M3.6 风控
            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._prev_spot_close: float | None = None

            # M5: PnL tracker（外部注入；funding_carry 是 delta-neutral，PnL 主要来自
            # funding 收入；entry/exit 之间的价差近似为 0，下面只在平仓时记 PnL=0
            # + r_multiple=0 占位，让该策略在 daily report 里有 trade count）
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None
            self._carry_entry_ts_ms: int = 0
            self._carry_entry_spot_price: float = 0.0

        def on_start(self) -> None:
            self.subscribe_bars(self.spot_bar_type)
            # 实盘：构造一个独立 REST 客户端拉 funding rate
            # 不复用 adapter 的 OKXRestClient，避免事件循环耦合
            from ..config import OKXSettings
            from ..rest.client import OKXRestClient
            self._rest_settings = OKXSettings()
            self._rest: OKXRestClient | None = None  # 首次 check 时懒构造
            self.log.info(
                f"funding_carry start: entry={self._params.entry_apr_threshold:.1%} "
                f"exit={self._params.exit_apr_threshold:.1%}"
            )

        def on_bar(self, bar: Bar) -> None:
            if bar.bar_type != self.spot_bar_type:
                return
            close = bar.close.as_double()
            self._latest_spot_price = close

            # 喂风控数据（每根 spot bar）
            self._feed_risk_data(ts_ms=int(bar.ts_event // 1_000_000), close=close)

            # 节流：每 check_interval_sec 才检查一次 funding rate
            now_ns = self.clock.timestamp_ns()
            if now_ns - self._last_check_ts_ns < self.config.check_interval_sec * 1_000_000_000:
                return
            self._last_check_ts_ns = now_ns
            # NT 在 backtest 里 clock 是定时推进的；实盘是真实 LiveClock
            try:
                loop = asyncio.get_running_loop()
                self._check_task = loop.create_task(self._check_funding_async())
            except RuntimeError:
                self.log.warning(
                    "funding_carry: no running loop; skip funding check"
                )

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
                if handles.drawdown_tracker is not None:
                    handles.drawdown_tracker.record_equity(ts_ms=ts_ms, equity=equity_usdt)
                # M5: 每个 UTC 日写一条 equity snapshot 给 PnL tracker
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=ts_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        def on_stop(self) -> None:
            self.log.info(f"funding_carry stop; has_position={self._has_position}")

        async def _check_funding_async(self) -> None:
            """异步拉 funding rate 并按 decision 执行。"""
            try:
                if self._rest is None:
                    from ..rest.client import OKXRestClient
                    self._rest = OKXRestClient(self._rest_settings)
                    await self._rest.__aenter__()

                perp_inst_id_str = self.perp_id.symbol.value
                fr = await self._rest.public.get_funding_rate(perp_inst_id_str)
                rate_8h = float(fr.funding_rate)
                action = funding_carry_decision(rate_8h, self._has_position, self._params)
                self.log.info(
                    f"funding={rate_8h:+.4%}/8h apr={float(fr.apr):+.2%} "
                    f"action={action.value} pos={self._has_position}"
                )
                if action == CarryAction.ENTER:
                    self._enter_carry()
                elif action == CarryAction.EXIT:
                    self._exit_carry()
            except Exception as exc:
                self.log.error(f"funding check failed: {exc}")

        def _enter_carry(self) -> None:
            spot_inst = self.cache.instrument(self.spot_id)
            perp_inst = self.cache.instrument(self.perp_id)
            if spot_inst is None or perp_inst is None or self._latest_spot_price <= 0:
                self.log.warning("missing instrument or spot price; skip enter")
                return

            ct_val = float(perp_inst.multiplier) if float(perp_inst.multiplier) > 0 else 1.0
            spot_qty, perp_contracts = carry_position_size(
                account_equity_usdt=self.config.account_equity_usdt,
                max_position_pct=self._params.max_position_pct,
                spot_price=self._latest_spot_price,
                perp_ct_val=ct_val,
                perp_lot_sz=float(perp_inst.size_increment),
            )
            if spot_qty <= 0 or perp_contracts <= 0:
                self.log.info("size=0; skip enter")
                return

            # M3.6: 把 perp_contracts 当 RiskIntent.size 过 RiskManager；
            # 缩仓时按比例同步缩 spot 保持 delta-neutral
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self.perp_id.value,
                direction="short",  # carry 永远做空 perp
                size=perp_contracts,
                entry_price=self._latest_spot_price,
                stop_price=self._latest_spot_price,  # delta-neutral 无传统 stop
                account_equity_usdt=self.config.account_equity_usdt,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None:
                return  # REJECT
            if adjusted != perp_contracts and adjusted > 0:
                # 按 perp 缩比例同步缩 spot，保持 USDT notional 对称
                ratio = adjusted / perp_contracts
                perp_contracts = adjusted
                spot_qty = spot_qty * ratio

            # 两腿同时下市价单 —— 任一腿 qty 对齐后变 0 则放弃，避免 delta 失衡
            spot_qty_obj = safe_make_qty(
                spot_inst, spot_qty, self.log, ctx=f"enter spot {self.spot_id.value}"
            )
            perp_qty_obj = safe_make_qty(
                perp_inst, perp_contracts, self.log,
                ctx=f"enter perp {self.perp_id.value}",
            )
            if spot_qty_obj is None or perp_qty_obj is None:
                return
            spot_order = self.order_factory.market(
                instrument_id=self.spot_id,
                order_side=OrderSide.BUY,
                quantity=spot_qty_obj,
                time_in_force=TimeInForce.IOC,
            )
            perp_order = self.order_factory.market(
                instrument_id=self.perp_id,
                order_side=OrderSide.SELL,
                quantity=perp_qty_obj,
                time_in_force=TimeInForce.IOC,
            )
            self.submit_order(spot_order)
            self.submit_order(perp_order)

            self._has_position = True
            self._spot_qty = spot_qty
            self._perp_contracts = perp_contracts
            self._carry_entry_ts_ms = int(self.clock.timestamp_ns() // 1_000_000)
            self._carry_entry_spot_price = self._latest_spot_price
            self.log.info(f"ENTER carry: spot={spot_qty} perp={perp_contracts} contracts")

        def _exit_carry(self) -> None:
            if not self._has_position:
                return
            spot_inst = self.cache.instrument(self.spot_id)
            perp_inst = self.cache.instrument(self.perp_id)
            if spot_inst is None or perp_inst is None:
                return

            spot_qty_obj = safe_make_qty(
                spot_inst, self._spot_qty, ctx=f"exit spot {self.spot_id.value}"
            )
            perp_qty_obj = safe_make_qty(
                perp_inst, self._perp_contracts,
                ctx=f"exit perp {self.perp_id.value}",
            )
            if spot_qty_obj is None or perp_qty_obj is None:
                self.log.error(
                    f"EXIT carry skipped: leg < lot_sz "
                    f"(spot={self._spot_qty}, perp={self._perp_contracts}); "
                    f"position remains open"
                )
                return
            spot_close = self.order_factory.market(
                instrument_id=self.spot_id,
                order_side=OrderSide.SELL,
                quantity=spot_qty_obj,
                time_in_force=TimeInForce.IOC,
                reduce_only=False,  # 现货账户没 reduce_only 概念，直接卖
            )
            perp_close = self.order_factory.market(
                instrument_id=self.perp_id,
                order_side=OrderSide.BUY,
                quantity=perp_qty_obj,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
            )
            self.submit_order(spot_close)
            self.submit_order(perp_close)
            self.log.info(
                f"EXIT carry: spot={self._spot_qty} perp={self._perp_contracts} contracts"
            )

            # M5: 记一笔 PnL（funding carry 是 delta-neutral，价差 PnL≈0；
            # 真实收入来自 funding rate 累积，由账户 equity 反映；这里写一条
            # 占位 trade record 让 trade-count 类的统计可见）
            cfg: FundingCarryConfig = self.config  # type: ignore[assignment]
            ts_now_ms = int(self.clock.timestamp_ns() // 1_000_000)
            record_strategy_trade(
                self._pnl_tracker,
                strategy_id=str(self.id),
                instrument_id=self.perp_id.value,
                closed_ts_ms=ts_now_ms,
                direction="short",  # carry 永远做空 perp
                entry_price=self._carry_entry_spot_price,
                exit_price=self._latest_spot_price,
                contracts=self._perp_contracts,
                ct_val=float(perp_inst.multiplier) if float(perp_inst.multiplier) > 0 else 1.0,
                risk_usdt=cfg.account_equity_usdt * cfg.max_position_pct,
            )

            self._has_position = False
            self._spot_qty = 0.0
            self._perp_contracts = 0.0
            self._carry_entry_ts_ms = 0
            self._carry_entry_spot_price = 0.0

else:  # pragma: no cover
    class FundingCarryConfig:  # type: ignore[no-redef]
        pass

    class FundingCarryStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "CarryAction",
    "FundingCarryConfig",
    "FundingCarryParams",
    "FundingCarryStrategy",
    "carry_position_size",
    "funding_carry_decision",
]
