"""日 / 周 PnL 监控 + 多档熝断状态机。

熝断阈值（与 plan 对齐）
------------------
| 层级 | 触发条件 | 动作 |
|---|---|---|
| 日 | 净值 < 昨收 × 0.97（-3%） | 全部平仓 + 新单冻结到次日 0:00 UTC |
| 周 | 净值 < 周初 × 0.92（-8%）  | 强制人工 review（拒绝所有新单直到手动 reset） |

设计
----
- ``DrawdownTracker``：纯状态机，``record_equity(ts, equity)`` 喂时序净值，
  内部维护当日开盘 / 周开盘净值 + 当前 drawdown 状态。
- ``DrawdownCheck``：RiskCheck，看 tracker 的状态决定 ``REJECT`` 或 ``APPROVE``。
- 决定何时 reset：
  - **日熝断**：自动在 UTC 0:00 重置（当 ``record_equity`` 跨日时）；
  - **周熝断**：必须手动 ``acknowledge_weekly_breach()``（人工 review 后）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .base import RiskCheck, RiskCheckResult, RiskIntent


class DrawdownState(str, Enum):
    """熝断状态。"""

    NORMAL = "normal"               # 正常交易
    DAILY_BREACH = "daily_breach"   # 日熝断中（次日 UTC 0:00 自动恢复）
    WEEKLY_BREACH = "weekly_breach"  # 周熝断中（必须手动 reset）


@dataclass(slots=True)
class DrawdownTracker:
    """跟踪账户净值，维护日 / 周开盘价和当前 drawdown 状态。

    所有时间戳用 UTC 毫秒。
    """

    daily_threshold_pct: float = 0.03   # 3% 日熝断
    weekly_threshold_pct: float = 0.08  # 8% 周熝断
    state: DrawdownState = DrawdownState.NORMAL

    # 内部状态
    _day_open_equity: float = 0.0
    _week_open_equity: float = 0.0
    _last_ts_ms: int = 0
    _current_equity: float = 0.0

    def record_equity(self, ts_ms: int, equity: float) -> None:
        """喂一次净值快照。

        触发的状态变更：
        - 跨 UTC 日 → 重置 day_open + 自动从 ``DAILY_BREACH`` 恢复
        - 跨 UTC 周（周一） → 重置 week_open（不自动恢复 ``WEEKLY_BREACH``）
        - 当日 drawdown > daily_threshold_pct → 进入 ``DAILY_BREACH``
        - 当周 drawdown > weekly_threshold_pct → 进入 ``WEEKLY_BREACH``（覆盖 daily）
        """
        self._current_equity = equity

        if self._day_open_equity == 0.0:
            # 第一次 record，初始化
            self._day_open_equity = equity
            self._week_open_equity = equity
            self._last_ts_ms = ts_ms
            return

        # 日界：UTC 0:00 边界
        prev_day = _utc_day(self._last_ts_ms)
        cur_day = _utc_day(ts_ms)
        if cur_day != prev_day:
            self._day_open_equity = equity
            # 跨日：自动从日熝断恢复（但周熝断不动）
            if self.state == DrawdownState.DAILY_BREACH:
                self.state = DrawdownState.NORMAL

        # 周界：跨过周一 UTC 0:00
        prev_week = _utc_week(self._last_ts_ms)
        cur_week = _utc_week(ts_ms)
        if cur_week != prev_week:
            self._week_open_equity = equity
            # 周界不自动恢复 WEEKLY_BREACH，必须手动 acknowledge

        self._last_ts_ms = ts_ms

        # 检查 drawdown
        if self.state == DrawdownState.WEEKLY_BREACH:
            return  # 周熝断已锁定，等手动恢复

        weekly_dd = 1 - equity / max(self._week_open_equity, 1e-9)
        if weekly_dd >= self.weekly_threshold_pct:
            self.state = DrawdownState.WEEKLY_BREACH
            return

        daily_dd = 1 - equity / max(self._day_open_equity, 1e-9)
        if daily_dd >= self.daily_threshold_pct:
            self.state = DrawdownState.DAILY_BREACH

    def acknowledge_weekly_breach(self) -> None:
        """人工 review 后调，把 WEEKLY_BREACH 重置为 NORMAL。"""
        if self.state == DrawdownState.WEEKLY_BREACH:
            self.state = DrawdownState.NORMAL
            self._week_open_equity = self._current_equity

    @property
    def daily_drawdown(self) -> float:
        if self._day_open_equity <= 0:
            return 0.0
        return 1 - self._current_equity / self._day_open_equity

    @property
    def weekly_drawdown(self) -> float:
        if self._week_open_equity <= 0:
            return 0.0
        return 1 - self._current_equity / self._week_open_equity


def _utc_day(ts_ms: int) -> int:
    """毫秒时间戳 → UTC 日序号（自纪元）。"""
    return ts_ms // 86_400_000


def _utc_week(ts_ms: int) -> int:
    """毫秒时间戳 → UTC ISO 周序号（年×100 + 周号）。"""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return iso_year * 100 + iso_week


class DrawdownCheck(RiskCheck):
    """drawdown 风控 check：熝断期间拒绝所有新订单。"""

    name = "drawdown"

    def __init__(self, tracker: DrawdownTracker) -> None:
        self.tracker = tracker

    def check(self, intent: RiskIntent) -> RiskCheckResult:
        if self.tracker.state == DrawdownState.WEEKLY_BREACH:
            return RiskCheckResult.reject(
                f"weekly DD {self.tracker.weekly_drawdown:.2%} > "
                f"{self.tracker.weekly_threshold_pct:.2%}; manual review needed",
                check_name=self.name,
            )
        if self.tracker.state == DrawdownState.DAILY_BREACH:
            return RiskCheckResult.reject(
                f"daily DD {self.tracker.daily_drawdown:.2%} > "
                f"{self.tracker.daily_threshold_pct:.2%}; auto-reset at 00:00 UTC",
                check_name=self.name,
            )
        return RiskCheckResult.approve(intent.size, check_name=self.name)


class AccountDrawdownTracker(DrawdownTracker):
    """账户级熝断 tracker。语义上与 ``DrawdownTracker`` 完全一致，
    用作单例: ``LiveMonitor`` 实例化一份, 每次 alloc_refresh 喂 OKX
    ``totalEq``, 并通过 ``AccountDrawdownCheck`` 注入到所有策略的
    risk pipeline。

    与 per-strategy 的 ``DrawdownTracker`` 区分:
    - per-strategy: 每个策略独立, 监控该策略自己的 PnL (Phase 1 接入 PnLTracker)
    - account-level (本类): 全账户单例, 任一策略触发后所有策略 kill-switch
    """


class AccountDrawdownCheck(RiskCheck):
    """账户级熝断 check。共享同一个 ``AccountDrawdownTracker``;
    任一策略命中即拒绝。日志前缀 ``account_drawdown`` 区分于
    ``drawdown`` (per-strategy)。
    """

    name = "account_drawdown"

    def __init__(self, tracker: AccountDrawdownTracker) -> None:
        self.tracker = tracker

    def check(self, intent: RiskIntent) -> RiskCheckResult:
        if self.tracker.state == DrawdownState.WEEKLY_BREACH:
            return RiskCheckResult.reject(
                f"account weekly DD {self.tracker.weekly_drawdown:.2%} > "
                f"{self.tracker.weekly_threshold_pct:.2%}; manual review needed",
                check_name=self.name,
            )
        if self.tracker.state == DrawdownState.DAILY_BREACH:
            return RiskCheckResult.reject(
                f"account daily DD {self.tracker.daily_drawdown:.2%} > "
                f"{self.tracker.daily_threshold_pct:.2%}; auto-reset at 00:00 UTC",
                check_name=self.name,
            )
        return RiskCheckResult.approve(intent.size, check_name=self.name)


__all__ = [
    "AccountDrawdownCheck",
    "AccountDrawdownTracker",
    "DrawdownCheck",
    "DrawdownState",
    "DrawdownTracker",
]
