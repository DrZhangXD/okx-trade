"""期权波动率卖方（Short Straddle + Delta Hedge）。

直觉
----
crypto 期权（BTC/ETH）的 IV 长期高于 realized vol（5-15% 年化溢价），是 vol risk
premium 最稳定的 carry 来源。卖 ATM straddle（同 strike 的 call + put）+ 用永续
delta-hedge，剩下 vega/theta exposure：
- theta 衰减：每天吃时间价值
- vega 风险：IV 上升导致 mtm 损失（卖空 vega）
- gamma 风险：spot 大幅波动时 hedge 滞后 → 实现负 P&L

机制
----
- 启动加载 OKX BTC 期权所有 live instrument（通过 instrument_provider option_uly filter）
- 选择最接近 ``tenor_target_days`` 的 expiry + 最近 ATM strike
- 每小时 poll opt-summary：IV / mark / Greeks
- 入场条件：
  - 当前 expiry 的 ATM IV / 30d realized vol > ``iv_rv_ratio_min``（默认 1.2 = 20% premium）
  - 暂无持仓
- 入场动作：
  - sell 1 张 call + sell 1 张 put（同 strike，等张数 → 净 delta ≈ 0）
  - 立即按当前 net delta 用 perp 对冲
- 持仓管理：
  - 每小时算 net delta（call Δ × pos_call + put Δ × pos_put + perp_pos / spot）
  - |net_delta| > ``delta_hedge_threshold`` → 用 perp 加 / 减仓
- 出场：
  - 到期前 1 天平掉所有腿
  - 或 |spot - strike| / strike > 5% → 强平止损（gamma 风险太大）

风险约束
-------
- ``max_notional_per_leg_usdt``：每腿名义上限（防 tail risk）
- 单个 expiry 只持一个 straddle（不滚仓）
- delta hedge 至少 1 小时间隔（避免高频 hedge 吃手续费）

注意
----
- 默认 ``enabled: false``：上线前要先 paper 验证 1-2 周看 OKX 期权 fill 行为
- BS pricer 只用于 sanity check，OKX opt-summary 已经给 Greeks（信任后者）
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING

from ..pricing.options import vol_premium
from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
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


def realized_vol_annualized(closes: list[float], periods_per_year: int = 365) -> float:
    """从一系列收盘价算年化实现波动率。

    log returns 的标准差 × √(periods_per_year)。
    closes 不足 3 个返回 0。
    """
    if len(closes) < 3:
        return 0.0
    log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(log_rets) < 2:
        return 0.0
    m = sum(log_rets) / len(log_rets)
    var = sum((r - m) ** 2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def select_atm_strike(strikes: list[float], spot: float) -> float | None:
    """从可用 strike 列表中选最近 ATM 的。空列表返回 None。"""
    if not strikes or spot <= 0:
        return None
    return min(strikes, key=lambda k: abs(k - spot))


if _NT_AVAILABLE:

    class OptionVolConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Option Vol Selling NT Strategy 配置。

        Attributes:
            underlying: OKX 期权标的（``"BTC-USD"`` 或 ``"BTC-USDT"``——OKX 期权
                的 instrument 命名是 ``BTC-USD-251225-100000-C``，underlying 是
                ``BTC-USD``，与现货 ``BTC-USDT`` 不同）。
            perp_instrument_id: 用来 delta-hedge 的永续合约。
            perp_bar_type: 永续 K 线（计算 spot + realized vol）。
            tenor_target_days: 目标剩余天数（选 expiry 时找最近的）。
            iv_rv_ratio_min: ``IV / RV ≥ 此值`` 才入场（默认 1.2）。
            rv_window: realized vol 计算窗口（1D 收盘价根数，默认 30）。
            max_notional_per_leg_usdt: 单 option 腿名义上限。
            delta_hedge_threshold: ``|net_delta| > 此值`` 触发 perp re-hedge。
            delta_hedge_freq_min: re-hedge 最小间隔（分钟）。
            stop_distance_strike_pct: ``|spot - strike|/strike > 此值`` 强平止损。
            check_interval_sec: 每多久 poll 一次 opt-summary（默认 1h）。
            account_equity_usdt / risk_config。
        """

        underlying: str
        perp_instrument_id: str
        perp_bar_type: str
        tenor_target_days: int = 7
        iv_rv_ratio_min: float = 1.20
        rv_window: int = 30
        max_notional_per_leg_usdt: float = 1000.0
        delta_hedge_threshold: float = 0.05
        delta_hedge_freq_min: int = 60
        stop_distance_strike_pct: float = 0.05
        close_days_before_expiry: int = 1
        check_interval_sec: int = 3600
        account_equity_usdt: float = 10000.0
        risk_config: RiskConfig | None = None


    class OptionVolStrategy(Strategy):  # type: ignore[misc]
        """Short straddle + delta hedge。

        状态：
        - ``self._call_inst_id`` / ``_put_inst_id``：当前持仓 option 的 inst id
        - ``self._call_qty`` / ``_put_qty``：每张数（卖出，故记正数表示 short size）
        - ``self._perp_pos``：净 perp 持仓张数（正数 = long perp，负数 = short）
        - ``self._strike`` / ``_expiry_ms``：当前 straddle 参数
        - ``self._last_hedge_ts_ms`` / ``_last_check_ts_ns``
        """

        def __init__(self, config: OptionVolConfig) -> None:
            super().__init__(config)
            self._perp_id = InstrumentId.from_str(config.perp_instrument_id)
            self._perp_bar_type = BarType.from_str(config.perp_bar_type)
            self._rv_closes: list[float] = []  # 1D 收盘价缓存（用 1H bar 取每日最后一根近似）
            self._last_perp_close: float = 0.0
            self._last_perp_ts_ms: int = 0
            self._last_perp_day: str = ""

            self._call_inst_id: InstrumentId | None = None
            self._put_inst_id: InstrumentId | None = None
            self._call_qty: float = 0.0
            self._put_qty: float = 0.0
            self._perp_pos: float = 0.0
            self._strike: float = 0.0
            self._expiry_ms: int = 0
            self._entry_iv: float = 0.0
            self._entry_spot: float = 0.0
            self._entry_ts_ms: int = 0

            self._last_hedge_ts_ms: int = 0
            self._last_check_ts_ns: int = 0
            self._allocated_equity_usdt: float | None = None

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None
            self._rest = None  # type: ignore[var-annotated]
            self._rest_settings = None  # type: ignore[var-annotated]
            self._check_task: asyncio.Task | None = None

        def on_start(self) -> None:
            self.subscribe_bars(self._perp_bar_type)
            from ..config import OKXSettings
            self._rest_settings = OKXSettings()
            self.log.info(
                f"option_vol start: uly={self.config.underlying} "
                f"target_tenor={self.config.tenor_target_days}d "
                f"iv_rv_min={self.config.iv_rv_ratio_min:.2f}"
            )

        def on_stop(self) -> None:
            self.log.info(
                f"option_vol stop; has_straddle={self._call_inst_id is not None}"
            )

        def on_bar(self, bar: Bar) -> None:
            if bar.bar_type != self._perp_bar_type:
                return
            close = bar.close.as_double()
            ts_ms = int(bar.ts_event // 1_000_000)
            self._last_perp_close = close
            self._last_perp_ts_ms = ts_ms

            # 累积每日最后一根 close（用于算 RV）
            day = time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))
            if day != self._last_perp_day:
                self._rv_closes.append(close)
                self._last_perp_day = day
                if len(self._rv_closes) > self.config.rv_window + 5:
                    self._rv_closes = self._rv_closes[-(self.config.rv_window + 5):]

            self._feed_risk_data()

            # 节流：每 check_interval_sec poll opt-summary
            now_ns = self.clock.timestamp_ns()
            if now_ns - self._last_check_ts_ns < self.config.check_interval_sec * 1_000_000_000:
                return
            self._last_check_ts_ns = now_ns
            try:
                loop = asyncio.get_running_loop()
                self._check_task = loop.create_task(self._check_options_async())
            except RuntimeError:
                self.log.warning("option_vol: no running loop; skip check")

        def _feed_risk_data(self) -> None:
            handles = self._risk_handles
            equity_usdt: float | None = None
            try:
                account = self.portfolio.account(self._perp_id.venue)
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

        async def _check_options_async(self) -> None:
            try:
                if self._rest is None:
                    from ..rest.client import OKXRestClient
                    self._rest = OKXRestClient(self._rest_settings)
                    await self._rest.__aenter__()

                summaries = await self._rest.public.get_option_summary(self.config.underlying)
                if not summaries:
                    return

                if self._call_inst_id is None:
                    # 没仓 → 看是否入场
                    await self._maybe_enter(summaries)
                else:
                    # 持仓 → re-hedge / 止损 / 到期平仓
                    await self._manage_position(summaries)
            except Exception as exc:
                self.log.error(f"option_vol check failed: {exc}")

        async def _maybe_enter(self, summaries: list) -> None:
            spot = self._last_perp_close
            if spot <= 0:
                return
            # 1. 选 expiry：找剩余天数最接近 target 的
            now_ms = int(time.time() * 1000)
            target_ms = self.config.tenor_target_days * 86400_000
            # 解析 inst_id 里的 expiry: BTC-USD-YYMMDD-STRIKE-C
            def _parse_expiry(inst_id: str) -> int:
                parts = inst_id.split("-")
                if len(parts) < 4:
                    return 0
                yymmdd = parts[2]
                try:
                    yr = 2000 + int(yymmdd[:2])
                    mo = int(yymmdd[2:4])
                    da = int(yymmdd[4:6])
                    # 16:00 UTC 是 OKX 期权交割时间
                    import datetime as dt
                    return int(dt.datetime(yr, mo, da, 16, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
                except (ValueError, IndexError):
                    return 0

            expiries: dict[int, list] = {}
            for s in summaries:
                exp_ms = _parse_expiry(s.inst_id)
                if exp_ms <= now_ms:
                    continue
                expiries.setdefault(exp_ms, []).append(s)
            if not expiries:
                return
            target_expiry = min(expiries.keys(), key=lambda e: abs((e - now_ms) - target_ms))
            target_summaries = expiries[target_expiry]

            # 2. 选 ATM strike
            def _parse_strike(inst_id: str) -> float:
                parts = inst_id.split("-")
                if len(parts) < 5:
                    return 0.0
                try:
                    return float(parts[3])
                except ValueError:
                    return 0.0

            strikes = sorted({_parse_strike(s.inst_id) for s in target_summaries if _parse_strike(s.inst_id) > 0})
            atm_strike = select_atm_strike(strikes, spot)
            if atm_strike is None:
                return

            # 3. 拉这个 strike 的 call + put 数据
            call_sum = next((s for s in target_summaries
                             if _parse_strike(s.inst_id) == atm_strike and s.inst_id.endswith("-C")), None)
            put_sum = next((s for s in target_summaries
                            if _parse_strike(s.inst_id) == atm_strike and s.inst_id.endswith("-P")), None)
            if call_sum is None or put_sum is None:
                return

            # 4. 入场判定：ATM IV vs realized vol
            avg_iv = (float(call_sum.mark_vol) + float(put_sum.mark_vol)) / 2.0
            rv = realized_vol_annualized(self._rv_closes, periods_per_year=365)
            if rv <= 0:
                self.log.info("option_vol: RV=0, skip enter")
                return
            premium = vol_premium(avg_iv, rv)
            self.log.info(
                f"option_vol candidate: strike={atm_strike} iv={avg_iv:.2%} "
                f"rv={rv:.2%} ratio={(avg_iv/rv):.2f}x premium={premium:+.2%}"
            )
            if avg_iv / rv < self.config.iv_rv_ratio_min:
                return

            # 5. 下单
            await self._open_straddle(
                call_inst_id=call_sum.inst_id,
                put_inst_id=put_sum.inst_id,
                strike=atm_strike,
                expiry_ms=target_expiry,
                spot=spot,
                avg_iv=avg_iv,
                call_delta=float(call_sum.delta),
                put_delta=float(put_sum.delta),
            )

        async def _open_straddle(
            self, *, call_inst_id: str, put_inst_id: str, strike: float,
            expiry_ms: int, spot: float, avg_iv: float,
            call_delta: float, put_delta: float,
        ) -> None:
            cfg: OptionVolConfig = self.config  # type: ignore[assignment]
            # 拿 instrument 信息（应该已经在 cache 里，instrument_provider 启动加载）
            call_id = InstrumentId.from_str(f"{call_inst_id}.OKX")
            put_id = InstrumentId.from_str(f"{put_inst_id}.OKX")
            call_inst = self.cache.instrument(call_id)
            put_inst = self.cache.instrument(put_id)
            if call_inst is None or put_inst is None:
                self.log.warning(
                    f"option_vol: option instruments missing in cache "
                    f"({call_inst_id} / {put_inst_id})"
                )
                return

            # 仓位：每腿 max_notional_per_leg_usdt
            # 期权 sz 单位是合约张数，BTC option 一张 = 0.01 BTC（按 multiplier）
            ct_val = float(call_inst.multiplier) if float(call_inst.multiplier) > 0 else 1.0
            qty_raw = cfg.max_notional_per_leg_usdt / (spot * ct_val)
            lot = float(call_inst.size_increment) if float(call_inst.size_increment) > 0 else 1.0
            qty = (int(qty_raw / lot) * lot) if lot > 0 else qty_raw
            if qty <= 0:
                self.log.info("option_vol: qty=0, skip enter")
                return

            # 风控：用 perp 名义当 risk intent
            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=self._perp_id.value,
                direction="short",  # 卖期权 ≈ vega short
                size=qty,
                entry_price=spot,
                stop_price=spot,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            if adjusted < qty:
                qty = adjusted

            qty_call = safe_make_qty(call_inst, qty, self.log, ctx="sell call")
            qty_put = safe_make_qty(put_inst, qty, self.log, ctx="sell put")
            if qty_call is None or qty_put is None:
                return

            # Sell 1 张 call + 1 张 put
            self.submit_order(self.order_factory.market(
                instrument_id=call_id, order_side=OrderSide.SELL,
                quantity=qty_call, time_in_force=TimeInForce.IOC,
            ))
            self.submit_order(self.order_factory.market(
                instrument_id=put_id, order_side=OrderSide.SELL,
                quantity=qty_put, time_in_force=TimeInForce.IOC,
            ))

            self._call_inst_id = call_id
            self._put_inst_id = put_id
            self._call_qty = qty
            self._put_qty = qty
            self._strike = strike
            self._expiry_ms = expiry_ms
            self._entry_iv = avg_iv
            self._entry_spot = spot
            self._entry_ts_ms = int(self.clock.timestamp_ns() // 1_000_000)

            # 立即 delta-hedge：net option delta = qty × (call_Δ + put_Δ)，全 short → -qty × (call_Δ + put_Δ)
            # ATM straddle ≈ -qty × (0.5 - 0.5) ≈ 0；但仍然挂一个对冲单初始化 perp pos
            net_delta = -qty * (call_delta + put_delta) * ct_val  # ct_val 折回 BTC notional
            self._hedge_to_neutral(net_delta=net_delta)
            self.log.info(
                f"OPEN straddle strike={strike} expiry_ms={expiry_ms} "
                f"qty={qty} iv={avg_iv:.2%}"
            )

        def _hedge_to_neutral(self, net_delta: float) -> None:
            """根据当前 option net_delta 调整 perp_pos 让 portfolio delta ≈ 0。

            target_perp_pos = -net_delta（单位是 BTC，转 perp 张数：除 perp ct_val）
            """
            now_ms = int(self.clock.timestamp_ns() // 1_000_000)
            if (now_ms - self._last_hedge_ts_ms
                    < self.config.delta_hedge_freq_min * 60_000):
                return
            perp_inst = self.cache.instrument(self._perp_id)
            if perp_inst is None:
                return
            perp_ct_val = float(perp_inst.multiplier) if float(perp_inst.multiplier) > 0 else 1.0
            target_perp_btc = -net_delta
            target_perp_contracts = target_perp_btc / perp_ct_val
            diff = target_perp_contracts - self._perp_pos
            lot = float(perp_inst.size_increment) if float(perp_inst.size_increment) > 0 else 1.0
            diff_qty = abs(int(diff / lot) * lot) if lot > 0 else abs(diff)
            if diff_qty <= 0:
                return
            qty_obj = safe_make_qty(perp_inst, diff_qty, self.log, ctx="hedge perp")
            if qty_obj is None:
                return
            side = OrderSide.BUY if diff > 0 else OrderSide.SELL
            self.submit_order(self.order_factory.market(
                instrument_id=self._perp_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
            ))
            self._perp_pos += (diff_qty if diff > 0 else -diff_qty)
            self._last_hedge_ts_ms = now_ms
            self.log.info(
                f"HEDGE perp side={side.name} qty={diff_qty} → pos={self._perp_pos}"
            )

        async def _manage_position(self, summaries: list) -> None:
            now_ms = int(time.time() * 1000)
            # 1. 到期前 close_days_before_expiry 天强平
            days_to_expiry = (self._expiry_ms - now_ms) / 86400_000
            if days_to_expiry <= self.config.close_days_before_expiry:
                self._close_all(reason="EXPIRY_NEAR")
                return
            # 2. spot 远离 strike → 强平
            if self._last_perp_close > 0:
                deviation = abs(self._last_perp_close - self._strike) / self._strike
                if deviation > self.config.stop_distance_strike_pct:
                    self._close_all(reason=f"STRIKE_DEV {deviation:.2%}")
                    return
            # 3. 重新算 net delta + re-hedge
            if self._call_inst_id is None or self._put_inst_id is None:
                return
            call_summary = next(
                (s for s in summaries if s.inst_id == self._call_inst_id.symbol.value), None,
            )
            put_summary = next(
                (s for s in summaries if s.inst_id == self._put_inst_id.symbol.value), None,
            )
            if call_summary is None or put_summary is None:
                return
            call_inst = self.cache.instrument(self._call_inst_id)
            ct_val = float(call_inst.multiplier) if call_inst and float(call_inst.multiplier) > 0 else 1.0
            net_delta = (
                -self._call_qty * float(call_summary.delta) * ct_val
                - self._put_qty * float(put_summary.delta) * ct_val
                + self._perp_pos * ct_val
            )
            # 阈值判断：偏离 > threshold 的 spot 等价量 → re-hedge
            if abs(net_delta) > self.config.delta_hedge_threshold * self._strike / 10000.0:
                # 阈值是 BTC 单位下的近似（0.05 ≈ 0.05 BTC ≈ $4000 spot）；这里粗略
                self._hedge_to_neutral(net_delta)

        def _close_all(self, reason: str) -> None:
            cfg: OptionVolConfig = self.config  # type: ignore[assignment]
            self.log.info(f"CLOSE straddle reason={reason}")
            if self._call_inst_id is not None and self._call_qty > 0:
                inst = self.cache.instrument(self._call_inst_id)
                if inst is not None:
                    qty = safe_make_qty(inst, self._call_qty, ctx="buy back call")
                    if qty is not None:
                        self.submit_order(self.order_factory.market(
                            instrument_id=self._call_inst_id, order_side=OrderSide.BUY,
                            quantity=qty, time_in_force=TimeInForce.IOC, reduce_only=True,
                        ))
            if self._put_inst_id is not None and self._put_qty > 0:
                inst = self.cache.instrument(self._put_inst_id)
                if inst is not None:
                    qty = safe_make_qty(inst, self._put_qty, ctx="buy back put")
                    if qty is not None:
                        self.submit_order(self.order_factory.market(
                            instrument_id=self._put_inst_id, order_side=OrderSide.BUY,
                            quantity=qty, time_in_force=TimeInForce.IOC, reduce_only=True,
                        ))
            # 平 perp hedge
            if abs(self._perp_pos) > 0:
                perp_inst = self.cache.instrument(self._perp_id)
                if perp_inst is not None:
                    qty = safe_make_qty(perp_inst, abs(self._perp_pos), ctx="close perp hedge")
                    if qty is not None:
                        side = OrderSide.SELL if self._perp_pos > 0 else OrderSide.BUY
                        self.submit_order(self.order_factory.market(
                            instrument_id=self._perp_id, order_side=side,
                            quantity=qty, time_in_force=TimeInForce.IOC, reduce_only=True,
                        ))

            ts_now_ms = int(self.clock.timestamp_ns() // 1_000_000)
            record_strategy_trade(
                self._pnl_tracker,
                strategy_id=str(self.id),
                instrument_id=self._call_inst_id.value if self._call_inst_id else "OPTION",
                closed_ts_ms=ts_now_ms,
                direction="short",
                entry_price=self._entry_spot,
                exit_price=self._last_perp_close,
                contracts=self._call_qty,
                ct_val=1.0,
                risk_usdt=cfg.max_notional_per_leg_usdt * 2,
            )

            self._call_inst_id = None
            self._put_inst_id = None
            self._call_qty = 0.0
            self._put_qty = 0.0
            self._perp_pos = 0.0
            self._strike = 0.0
            self._expiry_ms = 0
            self._entry_ts_ms = 0


else:  # pragma: no cover

    class OptionVolConfig:  # type: ignore[no-redef]
        pass

    class OptionVolStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "OptionVolConfig",
    "OptionVolStrategy",
    "realized_vol_annualized",
    "select_atm_strike",
]
