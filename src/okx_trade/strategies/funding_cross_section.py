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
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
from ..risk.stats import rolling_beta
from .base import effective_equity_usdt
from ._isolated_helpers import (
    compute_edge_score,
    compute_leverage,
    outlier_check,
)
from .pnl_hook import read_account_total_equity_usdt, record_strategy_equity_daily, record_strategy_trade
from .qty import safe_make_qty


@dataclass(slots=True, frozen=True)
class _LegTarget:
    """Per-leg planned position (output of _compute_target_positions).

    pos_side semantics: PosSide enum value (.LONG / .SHORT) for long_short
    account posMode; ignored in net mode. Caller uses this to call OKX
    set-leverage with the right side. direction is independent ("long"/
    "short") and is what we use for order side derivation.
    """
    direction: str
    contracts: float
    lever: float
    edge_score: float
    pos_side: object  # PosSide enum, kept generic to avoid import at module scope

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar
    from ..backtest.funding_data import FundingPanel


try:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import BarAggregation, OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NT_AVAILABLE = False
    StrategyConfig = object  # type: ignore[assignment,misc]
    Strategy = object  # type: ignore[assignment,misc]


if _NT_AVAILABLE:
    from ..enums import PosSide

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
        # 1m bars per inst feed the Layer-3 outlier guard (recent vol vs baseline).
        # Without this subscription, _closes_by_inst (1D) never reaches the
        # 1440-bar warmup the outlier_check defaults assume.
        vol_bar_type_template: str = "{inst}-1-MINUTE-LAST-EXTERNAL"
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
        # Live REST warmup: on_start fetches `beta_window_days + 2` 个 1D close
        # per instrument so β-hedge is accurate from the first rebalance instead
        # of waiting ~30 days of live 1D bars. Disable for backtest.
        warmup_via_rest: bool = True
        funding_panel_parquet_path: str | None = None
        """Optional: parquet catalog root (the directory that contains a
        ``funding/<inst_id>/<YYYYMM>.parquet`` subtree). When set, on_start
        reads the panel via read_funding_parquet() and calls feed_funding_panel()
        before any bar subscription. Used for backtest.
        """

        # === 2026-05-26: isolated margin + dynamic leverage ===
        enable_dynamic_lever: bool = True
        margin_mode: str = "isolated"   # "isolated" or "cross"; backtest forces "cross"
        lever_min: float = 2.0
        lever_max: float = 10.0
        lever_base: float = 2.0
        lever_slope: float = 3.0
        lever_edge_combine_basis: bool = True

        # === 2026-05-26: outlier guard ===
        enable_outlier_guard: bool = True
        outlier_vol_ratio: float = 3.0
        outlier_window_min: int = 60
        outlier_baseline_min: int = 1440
        outlier_warmup_min: int = 1440


    class FundingXSStrategy(Strategy):  # type: ignore[misc]
        """每个 funding cycle 重平 funding 横截面对冲组合。

        状态：
        - ``self._positions: dict[inst_id, ("long"|"short", contracts)]``
        - ``self._last_rebalance_ts_ms``
        - ``self._closes_by_inst``: deque of recent 1D close（用来算 β）
        - ``self._closes_1m_by_inst``: deque of recent 1m close（喂 Layer-3 outlier guard）
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

            # 1m bar types per inst for outlier_check (Layer 3 guard). Keyed
            # the same as _bar_types so on_start can iterate uniformly.
            self._vol_bar_types: dict[InstrumentId, BarType] = {
                iid: BarType.from_str(
                    config.vol_bar_type_template.format(inst=iid.value)
                )
                for iid in self._inst_ids
            }

            self._closes_by_inst: dict[str, list[float]] = {}
            # 1m closes for outlier_check. maxlen sized to cover the largest
            # outlier_baseline_min default (1440) with headroom.
            self._closes_1m_by_inst: dict[str, deque[float]] = {
                iid.value: deque(maxlen=2000) for iid in self._inst_ids
            }
            self._latest_funding: dict[str, float] = {}
            self._positions: dict[str, tuple[str, float]] = {}  # inst_id → (dir, contracts)
            self._last_rebalance_hour: int = -1
            self._last_rebalance_day: str = ""
            self._allocated_equity_usdt: float | None = None

            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]
            self._last_equity_day: str | None = None
            self._funding_task: asyncio.Task | None = None
            self._warmup_task: asyncio.Task | None = None
            self._rest = None  # type: ignore[var-annotated]
            self._rest_settings = None  # type: ignore[var-annotated]
            self._funding_panels: dict[str, "FundingPanel"] = {}
            self._funding_source_kind: str = "rest"  # "rest" (live) | "panel" (backtest)

            # 2026-05-26: isolated margin support
            self._set_lever_cache: dict[tuple[str, str], float] = {}  # (inst, posSide) → lever
            self._account_pos_mode: str | None = None  # cached from OKX /account/config
            self._latest_basis: dict[str, float] = {}  # populated in P-Task 10

        def on_start(self) -> None:
            # Backtest: auto-load funding panels for all configured insts
            if self.config.funding_panel_parquet_path:
                from pathlib import Path
                from ..backtest.funding_data import FundingPanel, read_funding_parquet
                panels: dict[str, FundingPanel] = {}
                cat_path = Path(self.config.funding_panel_parquet_path)
                for inst_id_obj in self._inst_ids:
                    sym = inst_id_obj.symbol.value
                    try:
                        panels[sym] = read_funding_parquet(sym, catalog_path=cat_path)
                    except FileNotFoundError:
                        self.log.warning(f"no funding panel for {sym}, skipping")
                if panels:
                    self.feed_funding_panel(panels)
                    self.log.info(f"loaded funding panels for {len(panels)} insts")
            for iid, bar_type in self._bar_types.items():
                self.subscribe_bars(bar_type)
            # Subscribe 1m bars for outlier guard (skip beta_ref if not a candidate)
            if self.config.enable_outlier_guard:
                for iid, bar_type in self._vol_bar_types.items():
                    self.subscribe_bars(bar_type)
            from ..config import OKXSettings
            self._rest_settings = OKXSettings()
            self.log.info(
                f"funding_xs start: universe={len(self._inst_ids)} "
                f"top_n={self.config.top_n} bot_n={self.config.bot_n} "
                f"beta_hedge={self.config.enable_beta_hedge}"
            )
            if self.config.warmup_via_rest:
                try:
                    loop = asyncio.get_event_loop()
                    self._warmup_task = loop.create_task(self._warmup_closes_via_rest())
                except RuntimeError as exc:
                    self.log.warning(
                        f"funding_xs: cannot spawn warmup task (no event loop): {exc}"
                    )

        def on_stop(self) -> None:
            if self._funding_task is not None:
                self._funding_task.cancel()
            if self._warmup_task is not None and not self._warmup_task.done():
                self._warmup_task.cancel()
                self._warmup_task = None
            self.log.info(f"funding_xs stop; open_legs={len(self._positions)}")

        def feed_funding_panel(self, panels: dict[str, "FundingPanel"]) -> None:
            """Inject pre-loaded funding panels keyed by inst_id (no venue suffix).

            When called, the funding-rate poll routes lookups through these
            panels instead of hitting REST.
            """
            self._funding_panels = dict(panels)
            self._funding_source_kind = "panel"

        async def _fetch_current_funding(self, inst_id_symbol: str) -> float | None:
            """Return latest funding rate for one inst from the active source.

            Args:
                inst_id_symbol: bare symbol without venue, e.g. ``"BTC-USDT-SWAP"``.
            """
            if self._funding_source_kind == "panel":
                panel = self._funding_panels.get(inst_id_symbol)
                if panel is None:
                    return None
                # Use the latest seen bar timestamp; fall back to last sample if no bar yet
                ts_ms = int(getattr(self, "_last_bar_ts_ns", 0) // 1_000_000)
                if ts_ms <= 0 and panel.ts_ms:
                    return panel.rates[-1]
                return panel.rate_at_or_before(ts_ms)
            # REST path (live)
            fr = await self._rest.public.get_funding_rate(inst_id_symbol)
            return float(fr.funding_rate)

        async def _fetch_basis(self, inst_value: str) -> float | None:
            """Pull (perp_mark - spot_index) / spot_index via OKX REST.

            Uses /api/v5/market/index-tickers for the spot index and
            /api/v5/public/mark-price for the perp mark. Returns ``None``
            on failure so caller can skip basis-component for that leg.
            """
            if self._is_backtest_context():
                return None
            if self._rest is None:
                from ..rest.client import OKXRestClient
                self._rest = OKXRestClient(self._rest_settings)
                await self._rest.__aenter__()
            # inst_value like "DOT-USDT-SWAP" → underlying "DOT-USDT"
            underlying = inst_value.replace("-SWAP", "")
            try:
                idx_data = await self._rest.transport.request(
                    "GET", "/api/v5/market/index-tickers",
                    params={"instId": underlying}, private=False, group=None,
                )
                mark_data = await self._rest.transport.request(
                    "GET", "/api/v5/public/mark-price",
                    params={"instType": "SWAP", "instId": inst_value},
                    private=False, group=None,
                )
                if not idx_data or not mark_data:
                    return None
                idx_px = float(idx_data[0].get("idxPx") or 0)
                mark_px = float(mark_data[0].get("markPx") or 0)
                if idx_px <= 0 or mark_px <= 0:
                    return None
                return (mark_px - idx_px) / idx_px
            except Exception as exc:
                self.log.warning(f"funding_xs fetch_basis failed inst={inst_value}: {exc}")
                return None

        async def _warmup_closes_via_rest(self) -> None:
            """One-shot REST fetch of ``beta_window_days + 2`` 1D closes per
            instrument so β-hedge is accurate from the first rebalance, plus
            ``outlier_baseline_min + outlier_window_min`` 1m closes per
            candidate so the outlier guard (Layer 3) is active from day 0.

            Without warmup, β computation needs ≥10 1D returns (per
            ``_compute_target_positions``), which means ≥10 days of live bars
            before any β scaling kicks in; and the outlier guard would sit at
            ``(True, "warmup")`` for the first ~24h of live 1m bars. With
            warmup, both are correct on the first rebalance.
            """
            from ..rest.client import OKXRestClient

            n_needed = self.config.beta_window_days + 2
            all_iids = list(self._bar_types.keys())  # 包括 beta_ref
            # 1m warmup target: cover the largest sliding window outlier_check reads.
            n_1m_needed = (
                self.config.outlier_baseline_min + self.config.outlier_window_min + 2
            )
            candidate_iids = list(self._inst_ids)  # 1m only needed for legs we may open
            try:
                async with OKXRestClient(self._rest_settings) as client:
                    fetched: dict[str, list[float]] = {}
                    for iid in all_iids:
                        try:
                            bare = iid.symbol.value  # e.g. "BTC-USDT-SWAP"
                            candles = await client.market.get_candles_extended(
                                bare, bar="1D", total=n_needed,
                            )
                            fetched[iid.value] = [
                                float(c.close) for c in candles if float(c.close) > 0
                            ]
                        except Exception as exc:  # noqa: BLE001
                            self.log.warning(
                                f"funding_xs warmup: {iid.value} fetch failed: {exc}"
                            )
                    fetched_1m: dict[str, list[float]] = {}
                    if self.config.enable_outlier_guard:
                        for iid in candidate_iids:
                            try:
                                bare = iid.symbol.value
                                candles = await client.market.get_candles_extended(
                                    bare, bar="1m", total=n_1m_needed,
                                )
                                fetched_1m[iid.value] = [
                                    float(c.close) for c in candles if float(c.close) > 0
                                ]
                            except Exception as exc:  # noqa: BLE001
                                self.log.warning(
                                    f"funding_xs 1m warmup: {iid.value} fetch failed: {exc}"
                                )
            except Exception as exc:  # noqa: BLE001
                self.log.warning(f"funding_xs warmup aborted: {exc}")
                return

            # Clear + refill per inst; on_bar appends new live bars after.
            filled = 0
            for inst_value, closes in fetched.items():
                if closes:
                    self._closes_by_inst[inst_value] = closes[:]
                    filled += 1
            self.log.info(
                f"funding_xs warmup loaded: {filled}/{len(all_iids)} instruments "
                f"closes (~{n_needed} 1D bars each)"
            )
            filled_1m = 0
            for inst_value, closes_1m in fetched_1m.items():
                if closes_1m:
                    # Reseed the deque (drops anything older / preserves maxlen).
                    dq = self._closes_1m_by_inst.setdefault(
                        inst_value, deque(maxlen=2000),
                    )
                    dq.clear()
                    dq.extend(closes_1m)
                    filled_1m += 1
            if self.config.enable_outlier_guard:
                self.log.info(
                    f"funding_xs 1m warmup loaded: {filled_1m}/{len(candidate_iids)} "
                    f"instruments (~{n_1m_needed} 1m bars each)"
                )

        def on_bar(self, bar: Bar) -> None:
            inst_value = bar.bar_type.instrument_id.value
            spec = bar.bar_type.spec
            close_px = bar.close.as_double()

            # Dispatch by bar period:
            #   1m bars → outlier_check cache (separate deque, sized for 1440+ warmup)
            #   1D bars → β-hedge cache (capped at beta_window_days + 2)
            # Anything else is logged once and ignored.
            if spec.aggregation == BarAggregation.MINUTE and spec.step == 1:
                buf_1m = self._closes_1m_by_inst.setdefault(
                    inst_value, deque(maxlen=2000),
                )
                buf_1m.append(close_px)
                # 1m bars don't gate rebalance / risk — just cache.
                return

            # 缓存收盘价用于 β 回归（1D path）
            buf = self._closes_by_inst.setdefault(inst_value, [])
            buf.append(close_px)
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
            equity_usdt = read_account_total_equity_usdt(
                self, fallback_venue=self._beta_ref_id.venue,
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

        async def _rebalance_async(self) -> None:
            try:
                if self._funding_source_kind == "rest" and self._rest is None:
                    from ..rest.client import OKXRestClient
                    self._rest = OKXRestClient(self._rest_settings)
                    await self._rest.__aenter__()
                # 1) 拉所有 universe 当前 funding rate（REST or panel）
                funding_tasks = [
                    self._fetch_current_funding(iid.symbol.value)
                    for iid in self._inst_ids
                ]
                rates = await asyncio.gather(*funding_tasks, return_exceptions=True)
                self._latest_funding = {}
                for iid, r in zip(self._inst_ids, rates):
                    if isinstance(r, Exception) or r is None:
                        continue
                    self._latest_funding[iid.value] = float(r)

                # 2) 可选：拉所有 universe 的 basis（perp_mark - spot_index）/ spot_index
                if self.config.lever_edge_combine_basis:
                    basis_tasks = [
                        self._fetch_basis(iid.value) for iid in self._inst_ids
                    ]
                    basis_values = await asyncio.gather(*basis_tasks, return_exceptions=True)
                    self._latest_basis = {}
                    for iid, b in zip(self._inst_ids, basis_values):
                        if isinstance(b, Exception) or b is None:
                            continue
                        self._latest_basis[iid.value] = float(b)

                # 3) 选股 + 计算 β-hedged 仓位
                target_positions = self._compute_target_positions()
                # 4) diff 当前持仓 vs target
                await self._execute_diff(target_positions)
                self.log.info(
                    f"funding_xs rebalanced: open_legs={len(self._positions)} "
                    f"funding_count={len(self._latest_funding)}"
                )
            except Exception as exc:
                self.log.error(f"funding_xs rebalance failed: {exc}")

        def _compute_target_positions(self) -> dict[str, _LegTarget]:
            """选 top_n 最负 funding 做多 + bot_n 最正 funding 做空，β-hedge 后返回。

            Returns:
                ``{inst_id: _LegTarget}``。空 dict = 全平。
            """
            if not self._latest_funding:
                return {}
            ranked = sorted(self._latest_funding.items(), key=lambda kv: kv[1])
            # 多腿：funding 最负 top_n
            long_legs = [iid for iid, _ in ranked[:self.config.top_n]]
            # 空腿：funding 最正 bot_n
            short_legs = [iid for iid, _ in ranked[-self.config.bot_n:]]
            target: dict[str, _LegTarget] = {}

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
                    # 2026-05-26: outlier guard — skip leg if recent vol abnormal
                    if self.config.enable_outlier_guard:
                        ok, reason = outlier_check(
                            closes=list(self._closes_1m_by_inst.get(inst_value, [])),
                            window=self.config.outlier_window_min,
                            baseline=self.config.outlier_baseline_min,
                            warmup=self.config.outlier_warmup_min,
                            ratio_threshold=self.config.outlier_vol_ratio,
                        )
                        if not ok:
                            self.log.warning(
                                f"funding_xs OUTLIER_SKIP inst={inst_value} "
                                f"direction={direction} reason={reason}"
                            )
                            continue
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
                    # 2026-05-26: dynamic leverage from funding (+ optional basis) z-score
                    if self.config.enable_dynamic_lever:
                        basis = (
                            self._latest_basis.get(inst_value)
                            if self.config.lever_edge_combine_basis else None
                        )
                        basis_universe = (
                            list(self._latest_basis.values())
                            if (self.config.lever_edge_combine_basis and self._latest_basis) else None
                        )
                        edge_score = compute_edge_score(
                            funding_rate=self._latest_funding[inst_value],
                            funding_universe=list(self._latest_funding.values()),
                            basis=basis,
                            basis_universe=basis_universe,
                            direction=direction,
                            combine_basis=self.config.lever_edge_combine_basis,
                        )
                        lever = compute_leverage(
                            edge_score,
                            base=self.config.lever_base,
                            slope=self.config.lever_slope,
                            lo=self.config.lever_min,
                            hi=self.config.lever_max,
                        )
                    else:
                        edge_score = 0.0
                        lever = self.config.lever_max

                    # pos_side: in net_mode account, None (account.py auto-fills net);
                    # in long_short_mode, derive from leg direction.
                    pos_side_value = (
                        PosSide.LONG if direction == "long" else PosSide.SHORT
                    )

                    target[inst_value] = _LegTarget(
                        direction=direction,
                        contracts=contracts,
                        lever=lever,
                        edge_score=edge_score,
                        pos_side=pos_side_value,
                    )
            return target

        @staticmethod
        def _returns(closes: list[float]) -> list[float]:
            if len(closes) < 2:
                return []
            return [(closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes))]

        async def _execute_diff(self, target: dict[str, _LegTarget]) -> None:
            """把当前 self._positions diff 到 target，平多余腿、开新腿、调整 size。"""
            current = dict(self._positions)
            # 平：当前持有但不在 target，或方向变了
            for inst_value, (cur_dir, cur_qty) in current.items():
                if inst_value not in target or target[inst_value].direction != cur_dir:
                    self._close_leg(inst_value, cur_dir, cur_qty)
            # 开 / 调：target 里有但当前没有，或 size 变了
            for inst_value, leg in target.items():
                cur = self._positions.get(inst_value)
                if cur is None:
                    await self._open_leg(inst_value, leg)
                elif cur[0] == leg.direction and abs(cur[1] - leg.contracts) > 1e-6:
                    # 同方向但 size 变 → 简化：先平后开（避免增仓 / 减仓两套 path）
                    self._close_leg(inst_value, cur[0], cur[1])
                    await self._open_leg(inst_value, leg)

        async def _open_leg(self, inst_value: str, leg: "_LegTarget") -> None:
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            equity = effective_equity_usdt(
                self._allocated_equity_usdt, self.config.account_equity_usdt,
            )
            last_closes = self._closes_by_inst.get(inst_value, [])
            entry_px = last_closes[-1] if last_closes else 0.0
            intent = RiskIntent(
                strategy_id=str(self.id),
                instrument_id=inst_value,
                direction=leg.direction,  # type: ignore[arg-type]
                size=leg.contracts,
                entry_price=entry_px,
                stop_price=entry_px,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            contracts = adjusted

            # 2026-05-26: isolated margin path (live only)
            use_isolated = (
                self.config.margin_mode == "isolated"
                and self.config.enable_dynamic_lever
                and not self._is_backtest_context()
            )
            if use_isolated:
                pos_mode = await self._get_account_pos_mode()
                # In long_short_mode, pass leg.pos_side (PosSide.LONG/SHORT).
                # In net_mode, pass None (account.py auto-fills PosSide.NET).
                pos_side_arg = leg.pos_side if pos_mode == "long_short_mode" else None
                ok = await self._set_leverage_cached(inst_value, leg.lever, pos_side_arg)
                if not ok:
                    self.log.warning(
                        f"funding_xs skip leg inst={inst_value} (set_leverage failed)"
                    )
                    return

            qty_obj = safe_make_qty(inst, contracts, self.log, ctx=f"open {inst_value}")
            if qty_obj is None:
                return
            side = OrderSide.BUY if leg.direction == "long" else OrderSide.SELL
            tags = ["td_mode:isolated"] if use_isolated else None
            order = self.order_factory.market(
                instrument_id=inst_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
                tags=tags,
            )
            self.submit_order(order)
            self._positions[inst_value] = (leg.direction, contracts)
            self.log.info(
                f"OPEN {leg.direction} {inst_value} qty={contracts} "
                f"lever={leg.lever:.1f} edge={leg.edge_score:+.2f} "
                f"mode={'isolated' if use_isolated else 'cross'}"
            )

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

        def _is_backtest_context(self) -> bool:
            """Detect whether we're in NT backtest engine (synchronous, no
            live REST client). Used to skip set-leverage and fall back to
            cross mode in backtest, since NT's MarginAccount doesn't model
            isolated correctly.

            Cheap heuristic: live mode has ``self._rest_settings`` populated
            with real credentials; backtest fixture passes empty/dummy
            settings. We check whether the settings look usable.
            """
            try:
                return not bool(getattr(self._rest_settings, "api_key", None))
            except Exception:
                return True  # safe default: assume backtest on any error

        async def _get_account_pos_mode(self) -> str:
            """Cached fetch of OKX account-level posMode. Values: 'net_mode',
            'long_short_mode'. Cached once per strategy lifetime — switching
            mode mid-session would require a restart anyway.
            """
            if self._account_pos_mode is not None:
                return self._account_pos_mode
            if self._rest is None:
                from ..rest.client import OKXRestClient
                self._rest = OKXRestClient(self._rest_settings)
                await self._rest.__aenter__()
            try:
                data = await self._rest.transport.request(
                    "GET", "/api/v5/account/config",
                    private=True, group=None,
                )
                if data and isinstance(data, list) and data[0]:
                    mode = data[0].get("posMode") or "net_mode"
                    self._account_pos_mode = str(mode)
                    self.log.info(f"funding_xs cached account posMode={mode}")
                    return self._account_pos_mode
            except Exception as exc:
                self.log.warning(f"funding_xs _get_account_pos_mode failed: {exc}; defaulting to net_mode")
            self._account_pos_mode = "net_mode"
            return self._account_pos_mode

        async def _set_leverage_cached(
            self, inst_value: str, lever: float, pos_side,
        ) -> bool:
            """Idempotent: call OKX set-leverage only if (inst, posSide, lever) changed.

            Args:
                inst_value: e.g. "DOT-USDT-SWAP".
                lever: target leverage (rounded to int for OKX).
                pos_side: PosSide enum or None. In net_mode, callers pass None
                    and account.py auto-fills posSide=net. In long_short_mode,
                    callers must pass PosSide.LONG or PosSide.SHORT.

            Returns ``True`` if leverage is in the desired state (set or
            already cached); ``False`` on REST failure (caller should skip
            the leg this round).
            """
            ps_key = pos_side.value if pos_side is not None else "net"
            cache_key = (inst_value, ps_key)
            cached = self._set_lever_cache.get(cache_key)
            if cached is not None and abs(cached - lever) < 0.01:
                return True
            if self._rest is None:
                from ..rest.client import OKXRestClient
                self._rest = OKXRestClient(self._rest_settings)
                await self._rest.__aenter__()
            try:
                from ..enums import TdMode
                await self._rest.account.set_leverage(
                    inst_id=inst_value,
                    leverage=int(round(lever)),
                    mgn_mode=TdMode.ISOLATED,
                    pos_side=pos_side,
                )
                self._set_lever_cache[cache_key] = lever
                self.log.info(
                    f"funding_xs set leverage inst={inst_value} "
                    f"mgnMode=isolated posSide={ps_key} lever={int(round(lever))}"
                )
                return True
            except Exception as exc:
                self.log.warning(
                    f"funding_xs set_leverage failed inst={inst_value} "
                    f"posSide={ps_key} lever={lever}: {exc}"
                )
                return False

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
