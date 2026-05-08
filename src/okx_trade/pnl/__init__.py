"""PnL 跟踪 + 统计（M5）。

把每笔已实现 PnL 的成交 + 每日 equity 快照存进 SQLite，对外暴露纯函数：

| API | 用途 |
|---|---|
| ``PnLTracker.record_trade`` / ``record_equity`` | 策略写入 |
| ``compute_win_rate_avg_r`` | 喂 ``KellyCheck.set_stats`` |
| ``compute_daily_returns``  | 喂 ``CorrelationCheck.update_strategy_pnl`` |
| ``feed_risk_handles``      | tracker → handles 一站式回灌 |

存储后端是 SQLite（stdlib），跨进程重启不丢数据；纯函数 stats 不依赖 tracker
实例，便于单测。
"""
from __future__ import annotations

from .feed import feed_risk_handles
from .stats import (
    aggregate_pnl_by_strategy,
    compute_daily_returns,
    compute_sharpe,
    compute_win_rate_avg_r,
)
from .tracker import EquitySnapshot, PnLTracker, TradeRecord

__all__ = [
    "EquitySnapshot",
    "PnLTracker",
    "TradeRecord",
    "aggregate_pnl_by_strategy",
    "compute_daily_returns",
    "compute_sharpe",
    "compute_win_rate_avg_r",
    "feed_risk_handles",
]
