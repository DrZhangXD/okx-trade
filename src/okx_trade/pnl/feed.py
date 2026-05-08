"""把 PnL tracker 的数据回灌到 ``RiskHandles``（M5 闭环 M3 占位）。

为什么要单独一个 module？
----------------------
- ``risk/`` 是纯风控逻辑，不知道 PnL 是怎么来的；
- ``pnl/`` 只负责存与算，不知道有哪些风控 check；
- ``feed`` 是两者间的胶水：从 tracker 读统计 → 调 ``handles.kelly.set_stats`` /
  ``handles.correlation.update_strategy_pnl``。策略层在 ``on_start`` 与每日定时调
  此函数即可。

设计
----
- ``min_trades_for_kelly`` 默认 20：样本不够时不更新 Kelly（保留 ``RiskConfig``
  里的默认 ``win_rate=0.5, avg_r=1.0``，等同于"中性"判断）；
- correlation 喂养：tracker 已经按日聚合了，直接把 ``compute_daily_returns``
  的结果塞进 ``CorrelationCheck`` 的 deque 即可；deque 自带 ``maxlen`` LRU；
- 每次调用都**重置 correlation 的 deque**（清空再 append），避免重复喂导致
  CorrelationCheck 看到自己的数据 N 倍。
"""
from __future__ import annotations

from collections import deque

from ..risk.integration import RiskHandles
from .stats import compute_daily_returns, compute_win_rate_avg_r
from .tracker import PnLTracker


def feed_risk_handles(
    tracker: PnLTracker,
    handles: RiskHandles,
    strategy_id: str,
    *,
    min_trades_for_kelly: int = 20,
) -> None:
    """从 tracker 读取统计，回灌到 ``handles``。

    Args:
        tracker: 已记录有该策略数据的 ``PnLTracker``。
        handles: 由 ``build_risk_manager(config)`` 返回的 ``RiskHandles``。
            handle 为 None 的 check 直接跳过。
        strategy_id: 要回灌的策略 id。
        min_trades_for_kelly: 调 ``KellyCheck.set_stats`` 所需最少样本数。

    Side effects:
        - ``handles.kelly`` 不为 None 且样本足 → 调 ``set_stats``；
        - ``handles.correlation`` 不为 None 且日数据≥1 → 重置 deque + 重灌。
    """
    if handles.kelly is not None:
        trades = tracker.get_trades(strategy_id)
        stats = compute_win_rate_avg_r(trades, min_n=min_trades_for_kelly)
        if stats is not None:
            handles.kelly.set_stats(win_rate=stats[0], avg_r=stats[1])

    if handles.correlation is not None:
        equities = tracker.get_equities(strategy_id)
        rets = compute_daily_returns(equities)
        if rets:
            window = handles.correlation.window
            # 重置该策略的 deque，避免多次 feed 累加
            handles.correlation._returns[strategy_id] = deque(
                rets[-window:], maxlen=window,
            )


__all__ = ["feed_risk_handles"]
