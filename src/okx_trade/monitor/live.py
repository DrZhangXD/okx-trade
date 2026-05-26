"""实时监控：周期性检查风控状态 / Kelly / 相关性，触发条件时 emit alert。

设计
----
- ``LiveMonitor`` 是 asyncio task，``run()`` 内部死循环 ``poll_interval_s``；
- 每次 poll：
  1. 检查每个策略 ``DrawdownTracker`` 状态——状态变化 → CRITICAL alert；
  2. 比较 ``KellyCheck`` 当前 win_rate 与上次 snapshot——跨 ``kelly_jump`` 阈值
     → INFO alert；
  3. 算 ``CorrelationCheck`` 各对相关性——超 ``correlation_max`` → WARN alert；
- 不直接读 NT 内部，纯靠注入的 ``RiskHandles`` + ``PnLTracker``，便于单测。

为什么不用 NT 自己的 EventBus？
---------------------------
- NT 1.225 在 live 模式下 EventBus 与 strategy 实例的访问模型尚不稳定；
- 多策略共享一个 monitor 通常更合适，否则每个策略各 emit 一份 noise；
- 用 polling + 比较 snapshot 即可，开销极小（4 策略 × 60s 一次）。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..risk import DrawdownState
from ..risk.integration import RiskHandles
from .alerts import Alert, AlertSeverity, AlertSink, fan_out

# 类型别名:equity_provider 可同步 () → float | None 或异步 await
EquityProvider = Callable[[], "float | None | Awaitable[float | None]"]


@dataclass(slots=True)
class _StrategySnapshot:
    """上次 poll 时记录的策略状态，用于 diff。"""

    drawdown_state: DrawdownState | None = None
    kelly_win_rate: float | None = None
    kelly_avg_r: float | None = None


@dataclass
class MonitorThresholds:
    """监控触发参数。"""

    kelly_jump: float = 0.05            # win_rate 跨 5pp → INFO alert
    correlation_max: float = 0.7        # 任一对 |ρ| > 0.7 → WARN alert
    reject_per_hour: int = 5            # 单策略 1h 内 risk REJECT 次数阈值
    reject_window_s: int = 3600


class LiveMonitor:
    """周期性检查 + 触发 alert。

    Args:
        handles_by_strategy: ``{strategy_id: RiskHandles}``，由 live_node 构造。
        sinks: alert sink 列表。
        poll_interval_s: 轮询间隔。默认 60s。
        thresholds: 触发参数；默认见 ``MonitorThresholds``。
        clock: ``() -> ms`` 时钟函数，方便测试 freeze；默认 ``time.time_ns()/1e6``。
        heartbeat_path: 每次 poll 后写入当前 ms 的 heartbeat 文件路径；
            外部 healthcheck 据此判活。``None`` → 关闭 heartbeat（仅测试用）。
            默认 ``var/heartbeat.ts``。
        equity_provider: 每 ``alloc_interval_s`` 调一次的回调,返回当前 OKX 真实
            USDT 余额(同步或 awaitable)。``None`` → 跳过 alloc refresh,保持
            策略 cfg 静态 equity。
        allocator: 与 ``equity_provider`` 配对,把新 total_equity 重新分配到每
            个策略;接 ``portfolio.base.Allocator`` 协议。
        strategies_by_name: ``{strategy_name: Strategy 实例}``,monitor 用来写
            ``strategy._allocated_equity_usdt``。key 须与 allocator.allocate 接
            收的 strategy_ids 一致。
        pnl_tracker: 喂给 ``allocator.allocate`` 算策略历史(risk_budget mode 需要)。
        alloc_interval_s: equity 重新分配间隔(秒,默认 3600 = 1h)。
    """

    def __init__(
        self,
        handles_by_strategy: dict[str, RiskHandles],
        sinks: list[AlertSink],
        *,
        poll_interval_s: int = 60,
        thresholds: MonitorThresholds | None = None,
        clock: Callable[[], int] | None = None,
        heartbeat_path: str | Path | None = "var/heartbeat.ts",
        equity_provider: EquityProvider | None = None,
        allocator: Any = None,
        strategies_by_name: dict[str, Any] | None = None,
        pnl_tracker: Any = None,
        alloc_interval_s: int = 3600,
        regime_detector: Any = None,
        regime_inst_id: str = "BTC-USDT-SWAP",
        regime_refresh_interval_s: int = 86400,
        regime_bootstrap_days: int = 400,
        account_drawdown_tracker: Any = None,
        iso_service: Any = None,
        vol_filter: Any = None,
    ) -> None:
        self.handles_by_strategy = handles_by_strategy
        self.sinks = sinks
        self.poll_interval_s = poll_interval_s
        self.thresholds = thresholds or MonitorThresholds()
        self._snapshots: dict[str, _StrategySnapshot] = {
            sid: _StrategySnapshot() for sid in handles_by_strategy
        }
        if clock is None:
            self._clock: Callable[[], int] = lambda: int(time.time_ns() / 1_000_000)
        else:
            self._clock = clock
        self._stopped = asyncio.Event()
        self.heartbeat_path: Path | None = (
            Path(heartbeat_path) if heartbeat_path is not None else None
        )
        if self.heartbeat_path is not None:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        # equity refresh / re-allocation
        self._equity_provider = equity_provider
        self._allocator = allocator
        self._strategies_by_name: dict[str, Any] = strategies_by_name or {}
        self._pnl_tracker = pnl_tracker
        self.alloc_interval_s = alloc_interval_s
        self._last_alloc_ms: int = 0
        # Regime detector：单例共享给所有启用 regime_gate 的策略
        self._regime_detector = regime_detector
        self._regime_inst_id = regime_inst_id
        self._regime_refresh_interval_s = regime_refresh_interval_s
        self._regime_bootstrap_days = regime_bootstrap_days
        self._last_regime_ms: int = 0
        # Account-level DD: single tracker shared across all strategies via
        # AccountDrawdownCheck (injected at build_risk_manager time). Monitor
        # is the only writer; per-strategy DrawdownTrackers are no longer fed
        # by alloc_refresh (Phase 0 of 2026-05-20 DD architecture split).
        self._account_drawdown_tracker = account_drawdown_tracker
        # Isolated-margin policy singleton + 1m vol-filter singleton:
        # constructed once in build_live_context and DI'd into every
        # OkxStrategyBase. Held here for future diagnostics surfacing
        # (e.g. cache size, last outlier ratio per inst) — not used
        # internally by the monitor poll loop today.
        self._iso_service = iso_service
        self._vol_filter = vol_filter

    def stop(self) -> None:
        self._stopped.set()

    def _write_heartbeat(self) -> None:
        """把当前 wall-clock ms 原子写入 heartbeat 文件。

        外部 ``scripts/healthcheck.py`` 据此判 monitor / strategies 是否还活着。
        atomic write：写 ``<path>.tmp`` 再 ``os.replace`` 防 healthcheck 读到半截。
        """
        if self.heartbeat_path is None:
            return
        try:
            now_ms = self._clock()
            tmp = self.heartbeat_path.with_suffix(self.heartbeat_path.suffix + ".tmp")
            tmp.write_text(str(now_ms))
            os.replace(tmp, self.heartbeat_path)
        except Exception:
            # heartbeat 写失败不影响 monitor 主流程
            pass

    async def _refresh_allocations(self) -> None:
        """查 OKX 真实余额 → allocator 重算每策略预算 → 写入 strategy 实例。

        - ``equity_provider`` 异常 / 返回 None / 返回 <= 0 → 静默跳过(保留上次值)。
        - 写入失败逐策略捕获,不挂整个 monitor。
        - emit INFO alert,方便审计每次 re-allocate 的总额和分配。
        """
        if not (self._equity_provider and self._allocator and self._strategies_by_name):
            return
        try:
            result = self._equity_provider()
            if inspect.isawaitable(result):
                total_equity = await result
            else:
                total_equity = result
        except Exception as exc:  # noqa: BLE001
            try:
                fan_out(self.sinks, Alert(
                    severity=AlertSeverity.WARN,
                    source="alloc_refresh",
                    message=f"equity_provider raised: {exc}",
                    ts_ms=self._clock(),
                    context={},
                ))
            except Exception:
                pass
            return
        if total_equity is None or total_equity <= 0:
            return

        names = list(self._strategies_by_name.keys())
        try:
            allocations = self._allocator.allocate(
                names, self._pnl_tracker,
                total_equity_usdt=Decimal(str(total_equity)),
            )
        except Exception as exc:  # noqa: BLE001
            try:
                fan_out(self.sinks, Alert(
                    severity=AlertSeverity.WARN,
                    source="alloc_refresh",
                    message=f"allocator.allocate raised: {exc}",
                    ts_ms=self._clock(),
                    context={"total_equity": float(total_equity)},
                ))
            except Exception:
                pass
            return

        changed: dict[str, float] = {}
        for name, strategy in self._strategies_by_name.items():
            # 2026-05-26 fix: 把账户级 totalEq 也下发给策略，让 _feed_risk_data
            # 写到 equities 表的 snapshot 用真实多币种 NAV 而不是 NT 的 USDT 单币 bal。
            # 否则 NT USDT bal 在仓位有 BTC/ETH/OKB collateral 时会严重低估
            # （事故：实际 totalEq $30,518 → NT USDT bal $377 → publisher 写 $377）。
            strategy._account_total_equity_usdt = float(total_equity)
            new_equity = float(allocations.get(name, 0))
            if new_equity <= 0:
                continue
            old = getattr(strategy, "_allocated_equity_usdt", None)
            strategy._allocated_equity_usdt = new_equity
            if old != new_equity:
                changed[name] = new_equity

        # 2026-05-20 DD architecture Phase 0: feed ONLY the account-level
        # tracker (single shared instance). Per-strategy DrawdownTracker
        # is no longer fed here — strategies that need per-strategy DD
        # isolation should subscribe to PnLTracker (Phase 1, future).
        # AccountDrawdownCheck is injected into every strategy's risk
        # pipeline so a single breach kill-switches all strategies.
        if self._account_drawdown_tracker is not None:
            try:
                self._account_drawdown_tracker.record_equity(
                    ts_ms=self._clock(), equity=float(total_equity),
                )
            except Exception:
                pass

        if changed:
            try:
                fan_out(self.sinks, Alert(
                    severity=AlertSeverity.INFO,
                    source="alloc_refresh",
                    message=(
                        f"re-allocated total={float(total_equity):.2f} USDT "
                        f"across {len(changed)} strategies"
                    ),
                    ts_ms=self._clock(),
                    context={
                        "total_equity": float(total_equity),
                        "allocations": changed,
                    },
                ))
            except Exception:
                pass

    async def _refresh_regime(self, *, bootstrap: bool = False) -> None:
        """从 OKX REST 拉 BTC 1d candle 喂 detector。

        - ``bootstrap=True``: 拉最近 ``regime_bootstrap_days`` 根（让 detector 立即可用）。
        - ``bootstrap=False``: 只拉最近 1 根（日常 incremental update）。

        失败静默——detector 退化到 "neutral"，不阻塞策略。
        """
        if self._regime_detector is None:
            return
        try:
            from .. import OKXRestClient, OKXSettings
            settings = OKXSettings()
            async with OKXRestClient(settings) as client:
                limit = self._regime_bootstrap_days if bootstrap else 2
                candles = await client.market.get_candles_extended(
                    self._regime_inst_id, "1D", total=limit,
                )
                for c in candles:
                    if c.confirm:
                        self._regime_detector.update(
                            ts_ms=int(c.ts), close=float(c.close),
                        )
            try:
                state = self._regime_detector.current_state()
                fan_out(self.sinks, Alert(
                    severity=AlertSeverity.INFO,
                    source="regime",
                    message=f"regime detector updated: state={state} bootstrap={bootstrap}",
                    ts_ms=self._clock(),
                    context={"state": state, "bootstrap": bootstrap},
                ))
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            try:
                fan_out(self.sinks, Alert(
                    severity=AlertSeverity.WARN,
                    source="regime",
                    message=f"regime refresh failed: {exc}",
                    ts_ms=self._clock(),
                    context={},
                ))
            except Exception:
                pass

    def _emit_service_started(self) -> None:
        """启动入口 emit 一条 INFO，让 ``alerts.jsonl`` 在每次 systemd 拉起服务
        时都留痕，方便外部统计 NRestarts / 服务可用率。"""
        alert = Alert(
            severity=AlertSeverity.INFO,
            source="service",
            message=(
                f"monitor started; pid={os.getpid()} "
                f"poll_interval={self.poll_interval_s}s "
                f"strategies={len(self.handles_by_strategy)}"
            ),
            ts_ms=self._clock(),
            context={
                "pid": os.getpid(),
                "poll_interval_s": self.poll_interval_s,
                "strategy_count": len(self.handles_by_strategy),
                "strategy_ids": list(self.handles_by_strategy.keys()),
            },
        )
        fan_out(self.sinks, alert)

    async def run(self) -> None:
        """死循环 poll；外部调 ``stop()`` 退出。"""
        # 启动信标：每次 systemd 拉起服务都会在 alerts.jsonl 留下一条记录
        try:
            self._emit_service_started()
        except Exception:
            pass
        # 启动即写一次 heartbeat，避免冷启动 30s 窗口内 healthcheck 误报
        self._write_heartbeat()
        # 启动立即跑一次 alloc refresh:让策略第一秒就拿到真实 OKX 余额,而不
        # 是要等 alloc_interval_s(默认 1h)
        try:
            await self._refresh_allocations()
            self._last_alloc_ms = self._clock()
        except Exception:
            pass
        # 启动立即 bootstrap regime detector（拉 400 根 BTC 1d 让 detector 可用）
        if self._regime_detector is not None:
            try:
                await self._refresh_regime(bootstrap=True)
                self._last_regime_ms = self._clock()
            except Exception:
                pass
        while not self._stopped.is_set():
            try:
                self.poll_once()
            except Exception:
                # 监控自己挂掉不能影响主流程
                pass
            # 周期性 re-allocate(若上次距今 ≥ alloc_interval_s)
            now_ms = self._clock()
            if (self._equity_provider is not None
                    and (now_ms - self._last_alloc_ms) >= self.alloc_interval_s * 1000):
                try:
                    await self._refresh_allocations()
                    self._last_alloc_ms = now_ms
                except Exception:
                    pass
            # 周期性 regime refresh（默认每 86400s = 1d 拉一次）
            if (self._regime_detector is not None
                    and (now_ms - self._last_regime_ms) >= self._regime_refresh_interval_s * 1000):
                try:
                    await self._refresh_regime(bootstrap=False)
                    self._last_regime_ms = now_ms
                except Exception:
                    pass
            # poll 成功（或被异常吞掉但 monitor task 仍在跑）→ 刷 heartbeat
            self._write_heartbeat()
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.poll_interval_s,
                )
            except asyncio.TimeoutError:
                continue

    def poll_once(self) -> list[Alert]:
        """一次 poll：检查所有 handles，返回触发的 alert（也已 emit 给 sinks）。"""
        triggered: list[Alert] = []
        ts = self._clock()

        for sid, handles in self.handles_by_strategy.items():
            snap = self._snapshots[sid]

            # 1) Drawdown 状态变化
            if handles.drawdown_tracker is not None:
                cur = handles.drawdown_tracker.state
                if cur != snap.drawdown_state and cur != DrawdownState.NORMAL:
                    alert = Alert(
                        severity=AlertSeverity.CRITICAL,
                        source="drawdown",
                        message=f"strategy {sid} drawdown state → {cur.value}",
                        ts_ms=ts,
                        context={"strategy_id": sid, "state": cur.value},
                    )
                    triggered.append(alert)
                snap.drawdown_state = cur

            # 2) Kelly 跨档（win_rate 突变）
            if handles.kelly is not None:
                cur_w = handles.kelly.win_rate
                cur_r = handles.kelly.avg_r
                if (
                    snap.kelly_win_rate is not None
                    and abs(cur_w - snap.kelly_win_rate) >= self.thresholds.kelly_jump
                ):
                    alert = Alert(
                        severity=AlertSeverity.INFO,
                        source="kelly",
                        message=(
                            f"strategy {sid} Kelly stats jumped: "
                            f"win_rate {snap.kelly_win_rate:.2f} → {cur_w:.2f}, "
                            f"avg_r {snap.kelly_avg_r or 0:.2f} → {cur_r:.2f}"
                        ),
                        ts_ms=ts,
                        context={
                            "strategy_id": sid,
                            "old_win_rate": snap.kelly_win_rate,
                            "new_win_rate": cur_w,
                            "old_avg_r": snap.kelly_avg_r,
                            "new_avg_r": cur_r,
                        },
                    )
                    triggered.append(alert)
                snap.kelly_win_rate = cur_w
                snap.kelly_avg_r = cur_r

            # 3) Correlation 任一对超阈值
            if handles.correlation is not None:
                rets = handles.correlation._returns
                sids_pair = sorted(rets.keys())
                # 找最大 |ρ|
                from ..risk.correlation import _pearson
                max_rho = 0.0
                max_pair = None
                for i, a in enumerate(sids_pair):
                    for b in sids_pair[i + 1:]:
                        rho = abs(_pearson(list(rets[a]), list(rets[b])))
                        if rho > max_rho:
                            max_rho = rho
                            max_pair = (a, b)
                if max_pair is not None and max_rho > self.thresholds.correlation_max:
                    alert = Alert(
                        severity=AlertSeverity.WARN,
                        source="correlation",
                        message=(
                            f"high correlation {max_pair[0]}↔{max_pair[1]}: "
                            f"|ρ|={max_rho:.2f} > {self.thresholds.correlation_max}"
                        ),
                        ts_ms=ts,
                        context={"pair": list(max_pair), "rho": max_rho},
                    )
                    triggered.append(alert)
                    # 同一对每次 poll 只 emit 一次（不去重，acceptable noise）

        for alert in triggered:
            fan_out(self.sinks, alert)
        return triggered


__all__ = ["LiveMonitor", "MonitorThresholds"]
