"""横截面动量（Cross-Sectional Momentum），vol-managed。

直觉
----
crypto 市场存在显著且稳定的横截面动量：过去 1-7 天涨幅靠前的币种，未来 1-7
天倾向继续相对跑赢；靠后的相对跑输。学术 + 实盘都验证过（2017-2024 仍然有效）。

具体做法：
1. **Universe**：每日选 top N 个 USDT 永续（按 24h 成交额排）；
2. **打分**：每个 inst 的动量 = ``(close_today / close_{T-lookback}) - 1``；
3. **排序**：score 降序，**做多 top_n 个**、**做空 bottom_n 个**（多空各等权）；
4. **Vol-managed**：每个 inst 仓位再按各自 N 日 realized vol 反向缩放到目标年化波动率；
5. **每日 UTC 0 点 rebalance**：算出新目标持仓，与当前持仓 diff，下增量市价单。

设计
----
- 纯函数 ``momentum_score`` / ``cross_section_rank`` / ``vol_managed_weight``
  / ``rebalance_orders`` 全部 NT 无关，便于单测；
- NT Strategy 包装：每个 inst 一个 ``BarBuffer``，所有 inst 都收到当日 D1
  close 后触发 ``_rebalance()``；下单走 NT ``submit_order``，被
  ``OKXLiveExecutionClient`` 翻译成实盘下单。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from ..risk import (
    RiskConfig,
    RiskIntent,
    apply_risk_manager,
    build_risk_manager,
    realized_vol_annualized,
    vol_target_position_size,
)
from .base import BarBuffer, BarSnapshot, effective_equity_usdt, to_bar_snapshots

# xs_momentum 走每日 rebalance + 增量订单，不存在清晰的"一笔成交"边界，
# PnL 主要通过 equity snapshot 喂 correlation；不需要 record_strategy_trade。
from .pnl_hook import record_strategy_equity_daily
from .qty import safe_make_qty

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def momentum_score(closes: list[float], lookback_days: int = 7) -> float | None:
    """简单动量 = ``(close_t / close_{t-lookback}) - 1``。

    Args:
        closes: 升序 close 序列。
        lookback_days: 回看天数。

    Returns:
        动量百分比（如 0.05 = +5%）；样本不足或无效时返回 ``None``。
    """
    if len(closes) <= lookback_days:
        return None
    base = closes[-1 - lookback_days]
    last = closes[-1]
    if base <= 0:
        return None
    return last / base - 1.0


def apply_inst_count_cap(
    longs: list[str],
    shorts: list[str],
    *,
    cap: int,
    top_n: int,
    bot_n: int,
) -> tuple[list[str], list[str]]:
    """按 ``top_n : bot_n`` 比例把 ``(longs, shorts)`` 裁到总数 ≤ ``cap``。

    场景:demo 账户保证金有限,XSMomentum 一次 rebalance 同时下 10 单会撑爆
    其他策略的开仓 margin。给出 ``cap`` 后只保留各自评分最好的若干个。

    Args:
        longs: ``cross_section_rank`` 返回的多腿,**按 score 降序**,``longs[0]`` 最佳。
        shorts: 空腿,**按 score 升序**(最弱在前),``shorts[0]`` 最佳。
        cap: 多 + 空总数上限。
        top_n / bot_n: 原始名额,用于按比例分配 cap。

    Returns:
        裁剪后的 ``(longs, shorts)``。``cap >= len(longs) + len(shorts)`` 时不动。

    边界:
    - ``cap <= 0`` → 都清空。
    - ``top_n + bot_n == 0`` → 都清空(无法按比例)。
    - 单边 ``top_n = 0`` 或 ``bot_n = 0`` → 全部 cap 给非零那边。
    """
    if cap <= 0 or (top_n + bot_n) == 0:
        return [], []
    if cap >= len(longs) + len(shorts):
        return longs, shorts
    # 按比例分配,longs 用 round (banker's rounding) 然后让 shorts 补齐总数
    max_long = round(cap * top_n / (top_n + bot_n))
    # 单边名额为 0 时强制把 cap 全给非零边
    if top_n == 0:
        max_long = 0
    elif bot_n == 0:
        max_long = cap
    max_long = min(max_long, len(longs))
    max_short = min(cap - max_long, len(shorts))
    # cap 没用满(一边不够)时,把余额还给另一边
    leftover = cap - max_long - max_short
    if leftover > 0:
        max_long = min(max_long + leftover, len(longs))
        max_short = min(cap - max_long, len(shorts))
    return longs[:max_long], shorts[:max_short]


def cross_section_rank(
    scores: dict[str, float],
    top_n: int = 5,
    bot_n: int = 5,
) -> tuple[list[str], list[str]]:
    """按 score 降序选出 longs 和 shorts。

    Args:
        scores: ``{inst_id: momentum_score}``，None / NaN 不应进入此映射（调用方过滤）。
        top_n: 做多名额；超过 universe 大小时取实际可用数量。
        bot_n: 做空名额。

    Returns:
        ``(longs, shorts)`` —— longs 按 score 降序，shorts 按 score 升序（最弱在前）。
        两者**不重叠**：当 universe 太小、top 和 bot 重合时丢掉重合部分（保证不会
        同时多空同一币）。

        排序内部用 ``(-score, inst_id)`` 字典序，并列时按 inst_id 升序——提供稳定
        可复现的回测结果。
    """
    if not scores:
        return [], []
    items = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    longs = [k for k, _ in items[:top_n]]
    # 反转：items[-bot_n:] 是升序末段，反转后变成「score 最低 → 最高」
    shorts_raw = list(reversed([k for k, _ in items[-bot_n:]])) if bot_n > 0 else []
    long_set = set(longs)
    shorts = [s for s in shorts_raw if s not in long_set]
    return longs, shorts


def vol_managed_weight(
    raw_weight: float,
    closes: list[float],
    target_vol_annualized: float = 0.15,
    vol_window: int = 20,
    max_scale: float = 2.0,
    min_scale: float = 0.1,
    periods_per_year: int = 365,
) -> float:
    """根据近 ``vol_window`` 日 realized vol 把 raw_weight 缩放到目标 vol。

    Args:
        raw_weight: 原始权重（含正负，多 = +1，空 = -1）。
        closes: 升序 close 序列；至少 ``vol_window+1`` 根才有意义。
        target_vol_annualized: 目标年化波动率（默认 15%）。
        vol_window: realized vol 计算窗口。
        max_scale / min_scale: 缩放因子上下限。

    Returns:
        缩放后的权重（保留 raw_weight 的正负号）。样本不足时返回 ``raw_weight``。
    """
    if len(closes) < vol_window + 1:
        return raw_weight
    rets = [closes[i] / closes[i - 1] - 1.0
            for i in range(len(closes) - vol_window, len(closes))
            if closes[i - 1] > 0]
    if len(rets) < 2:
        return raw_weight
    rv = realized_vol_annualized(rets, periods_per_year=periods_per_year)
    if rv <= 0:
        return raw_weight
    sign = 1.0 if raw_weight >= 0 else -1.0
    scaled_abs = vol_target_position_size(
        abs(raw_weight),
        realized_vol=rv,
        target_vol=target_vol_annualized,
        max_scale=max_scale,
        min_scale=min_scale,
    )
    return sign * scaled_abs


def plan_delta_orders(
    current: float, delta: float,
) -> list[tuple[float, bool]]:
    """根据当前持仓 + delta 拆出 1-2 个市价单,每个标注 reduce_only。

    OKX 在 hedged 模式下会按 ``posSide`` 判定开/平,而 ``posSide`` 又取决于
    ``reduce_only`` 标志(见 ``adapter/execution.py:resolve_pos_side``)。
    所以 XSMomentum 的 ``_submit_delta`` **必须正确标 reduce_only**,否则
    "缩仓 / 平仓"会被 OKX 当成新开反向单,持续吃保证金直至全锁(2026-05 真实问题)。

    Args:
        current: 当前 net position(正=多,负=空,0=空仓)。
        delta: 期望增量(正=买入,负=卖出),保持 ``target - current`` 的符号。

    Returns:
        ``[(qty_signed, reduce_only), ...]`` 列表:
        - ``current == 0`` 或同向加仓 → 单一开仓单 ``[(delta, False)]``
        - 反向且 ``|delta| <= |current|`` → 单一平 / 缩仓 ``[(delta, True)]``
        - 反向且 ``|delta| > |current|`` → 翻仓 → 拆 2 单:
          ``[(-current, True), (delta + current, False)]`` 先平到 0 再反向开
        - ``delta == 0`` → 空列表(no-op)

    每个 tuple 的 qty 保留原始符号(>0 buy / <0 sell),上层据此选 OrderSide。
    """
    if delta == 0.0:
        return []
    if current == 0.0 or (current > 0) == (delta > 0):
        # 全新开仓 / 同向加仓
        return [(delta, False)]
    # 反向
    if abs(delta) <= abs(current):
        # 纯平 / 缩仓 —— qty 保留原始符号,reduce_only=True
        return [(delta, True)]
    # 翻仓:先把 current 平到 0,再反向开 (delta + current = 剩余 open 量)
    close_qty = -current          # 与 current 反号,大小 = |current|
    open_qty = delta - close_qty  # 仍与 delta 同号(反向开仓的方向)
    return [(close_qty, True), (open_qty, False)]


def rebalance_orders(
    current: dict[str, float],
    target: dict[str, float],
    min_delta: float = 0.0,
) -> dict[str, float]:
    """计算从 ``current`` 到 ``target`` 的增量订单。

    Args:
        current: 当前持仓 ``{inst_id: contracts}``，正数 = 多头，负数 = 空头，0 = 无仓。
        target: 目标持仓，同结构。
        min_delta: 增量绝对值低于该值的不下单（默认 0，全部下）。

    Returns:
        ``{inst_id: delta_contracts}``，正数 = 净买入张数，负数 = 净卖出。
        没变化的 inst 不出现在结果中。
    """
    out: dict[str, float] = {}
    keys = set(current) | set(target)
    for k in keys:
        delta = target.get(k, 0.0) - current.get(k, 0.0)
        if abs(delta) > min_delta and delta != 0.0:
            out[k] = delta
    return out


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

    class XSMomentumConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """XS Momentum NT Strategy 配置。

        Attributes:
            instrument_ids: universe（如 ``("BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX", ...)``）。
                调用方可在启动 NT 前用 ``rest/public.get_instruments + market.get_tickers``
                算出 top N by 24h volume，再传进来。
            bar_type_template: D1 K 线 BarType 模板。每个 inst 用
                ``bar_type_template.replace("{inst}", inst_id)`` 生成自己的 BarType。
                典型："{inst}-1-DAY-LAST-EXTERNAL"。
            lookback_days: 动量回看天数（默认 7）。
            top_n / bot_n: 多 / 空腿名额（各 5）。
            target_vol_annualized: vol-managed 目标年化波动率。
            vol_window: realized vol 计算窗口（默认 20 日）。
            rebalance_hour_utc: 当日 UTC 几点触发 rebalance（默认 0）。
            risk_pct: 每个腿单笔风险占净值（默认 1%）。
            account_equity_usdt: 回测虚拟净值；实盘留 0 → 从账户读。
            buffer_size: 每个 inst 的 BarBuffer 长度（>= lookback_days + vol_window + 5）。
            risk_config: 可选 ``RiskConfig`` —— None 跳过风控。推荐
                ``enable_vol_target=False``（vol 缩放已在策略内做了）+
                ``enable_drawdown=True``。
        """

        instrument_ids: tuple[str, ...]
        bar_type_template: str
        lookback_days: int = 7
        top_n: int = 5
        bot_n: int = 5
        max_inst_count: int | None = None  # 单次 rebalance 实际持仓 inst 数上限;
                                            # None = 不限,等于 top_n + bot_n
        target_vol_annualized: float = 0.15
        vol_window: int = 20
        rebalance_hour_utc: int = 0
        risk_pct: float = 0.01
        account_equity_usdt: float = 10000.0
        buffer_size: int = 60
        risk_config: RiskConfig | None = None


    class XSMomentumStrategy(Strategy):  # type: ignore[misc]
        """每日 UTC 0 点 rebalance 的横截面动量策略。

        持仓表示：``self._target_contracts[inst_id]`` 正负浮点；
        - 实际持仓由 NT 维护（``self.portfolio.net_position(...)``），策略只算 target；
        - 每次 rebalance 把 (target - current) 的 delta 用市价 IOC 下出去。

        Synthetic stop：本策略不设固定 SL/TP，靠每日 rebalance 自然换仓 + drawdown
        熔断兜底。
        """

        def __init__(self, config: XSMomentumConfig) -> None:
            super().__init__(config)
            self._inst_ids = [InstrumentId.from_str(s) for s in config.instrument_ids]
            self._bar_types: dict[str, BarType] = {
                iid.value: BarType.from_str(
                    config.bar_type_template.replace("{inst}", iid.value)
                )
                for iid in self._inst_ids
            }
            self._buffers: dict[str, BarBuffer] = {
                iid.value: BarBuffer(maxlen=config.buffer_size) for iid in self._inst_ids
            }
            self._last_close_date: dict[str, date] = {}
            self._last_rebalance_date: date | None = None
            self._target_contracts: dict[str, float] = {}
            # 由 LiveMonitor 周期写入(OKX 真实余额 + allocator);None 退 cfg
            self._allocated_equity_usdt: float | None = None

            # M3.6 风控
            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._prev_close: dict[str, float] = {}

            # M5: PnL tracker（外部注入）
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None

        # ------------------------------------------------------------------
        # NT lifecycle
        # ------------------------------------------------------------------

        def on_start(self) -> None:
            for iid in self._inst_ids:
                self.subscribe_bars(self._bar_types[iid.value])
            self.log.info(
                f"xs_momentum start: universe={len(self._inst_ids)} "
                f"top_n={self.config.top_n} bot_n={self.config.bot_n} "
                f"target_vol={self.config.target_vol_annualized:.0%}"
            )

        def on_bar(self, bar: Bar) -> None:
            inst_id_str = bar.bar_type.instrument_id.value
            if inst_id_str not in self._buffers:
                return
            snap = to_bar_snapshots([bar])[0]
            self._buffers[inst_id_str].append(snap)

            # 喂风控数据
            self._feed_risk_data(inst_id_str, snap)

            # 标记当日 close 已收
            bar_dt = datetime.fromtimestamp(snap.ts / 1000, tz=timezone.utc)
            self._last_close_date[inst_id_str] = bar_dt.date()

            # 当所有 inst 都拿到了相同日期的 close，且超过上次 rebalance，触发
            if not self._all_inst_caught_up():
                return
            today = self._last_close_date[inst_id_str]
            if self._last_rebalance_date is not None and today <= self._last_rebalance_date:
                return
            if bar_dt.hour < self.config.rebalance_hour_utc:
                return
            self._rebalance(today)

        def on_stop(self) -> None:
            self.log.info(
                f"xs_momentum stop; last_rebalance={self._last_rebalance_date}"
            )

        # ------------------------------------------------------------------
        # Internals
        # ------------------------------------------------------------------

        def _all_inst_caught_up(self) -> bool:
            if len(self._last_close_date) < len(self._inst_ids):
                return False
            target = max(self._last_close_date.values())
            return all(d == target for d in self._last_close_date.values())

        def _feed_risk_data(self, inst_id_str: str, snap: BarSnapshot) -> None:
            """给 risk handles 喂 returns / equity（每根 bar）。"""
            handles = self._risk_handles
            prev = self._prev_close.get(inst_id_str)
            if handles.vol_target is not None and prev is not None and prev > 0:
                ret = snap.close / prev - 1.0
                handles.vol_target.update_return(inst_id_str, ret)
            self._prev_close[inst_id_str] = snap.close

            equity_usdt: float | None = None
            try:
                venue = self._inst_ids[0].venue
                account = self.portfolio.account(venue)
                if account is not None:
                    from nautilus_trader.model.currencies import USDT
                    bal = account.balance_total(USDT)
                    if bal is not None:
                        equity_usdt = float(bal.as_decimal())
            except Exception:
                equity_usdt = None

            if equity_usdt is not None:
                # equity 用 wall-clock：snap.ts 是 bar 收线时间，对 1D bar 可能在未来。
                now_ms = int(time.time() * 1000)
                if handles.drawdown_tracker is not None:
                    handles.drawdown_tracker.record_equity(ts_ms=now_ms, equity=equity_usdt)
                # M5: 每个 UTC 日写一条 equity snapshot 给 PnL tracker
                self._last_equity_day = record_strategy_equity_daily(
                    self._pnl_tracker,
                    strategy_id=str(self.id),
                    ts_ms=now_ms,
                    equity_usdt=equity_usdt,
                    last_day=self._last_equity_day,
                )

        def _rebalance(self, today: date) -> None:
            """计算 target 持仓 → diff current → 下增量订单。"""
            cfg: XSMomentumConfig = self.config  # type: ignore[assignment]

            # 1) 算每个 inst 的 momentum score
            scores: dict[str, float] = {}
            closes_by_inst: dict[str, list[float]] = {}
            for iid in self._inst_ids:
                key = iid.value
                bars = list(self._buffers[key])
                closes = [b.close for b in bars]
                closes_by_inst[key] = closes
                s = momentum_score(closes, lookback_days=cfg.lookback_days)
                if s is not None:
                    scores[key] = s

            if len(scores) < cfg.top_n + cfg.bot_n:
                self.log.info(
                    f"rebalance skipped: scored={len(scores)} < required={cfg.top_n + cfg.bot_n}"
                )
                return

            # 2) cross-section rank
            longs, shorts = cross_section_rank(scores, top_n=cfg.top_n, bot_n=cfg.bot_n)
            # 2a) demo / 小账户 margin 节流:把同时下单的 inst 数裁到 cap
            if cfg.max_inst_count is not None:
                longs, shorts = apply_inst_count_cap(
                    longs, shorts,
                    cap=cfg.max_inst_count, top_n=cfg.top_n, bot_n=cfg.bot_n,
                )
            self.log.info(
                f"rebalance {today} longs={longs} shorts={shorts} "
                f"cap={cfg.max_inst_count}"
            )

            # 3) 等权 + vol-managed → 每个腿的 contracts 张数
            target: dict[str, float] = {}
            for inst_id_str in longs:
                target[inst_id_str] = self._compute_leg_size(
                    inst_id_str, direction="long", closes=closes_by_inst[inst_id_str],
                )
            for inst_id_str in shorts:
                target[inst_id_str] = self._compute_leg_size(
                    inst_id_str, direction="short", closes=closes_by_inst[inst_id_str],
                )

            # 4) diff 当前持仓 → 下增量订单
            current = {iid.value: float(self.portfolio.net_position(iid))
                       for iid in self._inst_ids}
            deltas = rebalance_orders(current, target)

            for inst_id_str, delta in deltas.items():
                self._submit_delta(inst_id_str, delta)

            self._last_rebalance_date = today
            self._target_contracts = target

        def _compute_leg_size(
            self,
            inst_id_str: str,
            direction: str,
            closes: list[float],
        ) -> float:
            """单条腿（多 / 空）的目标张数（带正负号）。

            按 ``equity × risk_pct`` 反推 USDT 名义额，再除以 ``close × ct_val``，
            乘 vol_managed scaling，对齐 lot_sz。
            """
            cfg: XSMomentumConfig = self.config  # type: ignore[assignment]
            inst = self.cache.instrument(InstrumentId.from_str(inst_id_str))
            if inst is None or not closes:
                return 0.0
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            lot_sz = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0
            close = closes[-1]
            if close <= 0:
                return 0.0

            sign = 1.0 if direction == "long" else -1.0
            raw_weight = sign * cfg.risk_pct  # 名义占净值比例
            scaled = vol_managed_weight(
                raw_weight,
                closes,
                target_vol_annualized=cfg.target_vol_annualized,
                vol_window=cfg.vol_window,
                periods_per_year=365,
            )
            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            notional = abs(scaled) * equity
            raw_contracts = notional / (close * ct_val)
            # 对齐 lot
            from decimal import ROUND_DOWN
            step_d = Decimal(str(lot_sz))
            raw_d = Decimal(str(raw_contracts))
            contracts = float(
                (raw_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
            )
            if contracts <= 0:
                return 0.0

            # 过 RiskManager（用 |contracts| 当 size，方向通过 sign 还原）
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=inst_id_str,
                direction=direction,  # type: ignore[arg-type]
                size=contracts,
                entry_price=close,
                stop_price=close * (0.98 if direction == "long" else 1.02),
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return 0.0
            return sign * adjusted

        def _submit_delta(self, inst_id_str: str, delta_contracts: float) -> None:
            inst_id = InstrumentId.from_str(inst_id_str)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                self.log.warning(f"rebalance: instrument missing {inst_id_str}")
                return
            # 读 OKX-synced 的当前净仓,据此决定开 / 平 / 翻
            current = float(self.portfolio.net_position(inst_id))
            for qty_signed, reduce_only in plan_delta_orders(current, delta_contracts):
                qty_obj = safe_make_qty(
                    inst, abs(qty_signed), self.log, ctx=f"rebalance {inst_id_str}",
                )
                if qty_obj is None:
                    continue
                side = OrderSide.BUY if qty_signed > 0 else OrderSide.SELL
                order = self.order_factory.market(
                    instrument_id=inst_id,
                    order_side=side,
                    quantity=qty_obj,
                    time_in_force=TimeInForce.IOC,
                    reduce_only=reduce_only,
                )
                self.submit_order(order)
                self.log.info(
                    f"rebalance order: {inst_id_str} qty={qty_signed:+.4f} "
                    f"side={side.name} reduce_only={reduce_only} "
                    f"(current={current:+.4f}, delta={delta_contracts:+.4f})"
                )

else:  # pragma: no cover —— [strategy] extra 未装

    class XSMomentumConfig:  # type: ignore[no-redef]
        pass

    class XSMomentumStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError("nautilus_trader not installed; run pip install -e '.[strategy]'")


__all__ = [
    "XSMomentumConfig",
    "XSMomentumStrategy",
    "apply_inst_count_cap",
    "cross_section_rank",
    "momentum_score",
    "plan_delta_orders",
    "rebalance_orders",
    "vol_managed_weight",
]
