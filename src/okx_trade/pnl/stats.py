"""PnL 纯函数统计。

把 ``PnLTracker`` 拉出的原始记录转换为风控层需要的统计量：

- ``compute_win_rate_avg_r(trades, min_n)`` → ``(win_rate, avg_r)`` 喂 ``KellyCheck``
- ``compute_daily_returns(equities)`` → ``list[float]`` 喂 ``CorrelationCheck``
- ``compute_sharpe(returns)`` 给 daily_report / monitor

设计原则：纯函数，不依赖 tracker 实例；测试可直接 mock list 输入。
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone

from .tracker import EquitySnapshot, TradeRecord


def compute_win_rate_avg_r(
    trades: Sequence[TradeRecord],
    *,
    min_n: int = 20,
) -> tuple[float, float] | None:
    """从成交序列估 Kelly 参数。

    Args:
        trades: 已实现 PnL 的 trade 序列。
        min_n: 最小样本数；不够返回 None（不更新 Kelly，保留默认值）。
            默认 20，足够对 win_rate / avg_r 做点估计。

    Returns:
        ``(win_rate, avg_r)``：
        - ``win_rate`` ∈ [0, 1]：盈利占比；
        - ``avg_r`` > 0：平均盈利 / 平均亏损（绝对值）；
        - 全胜（无亏损样本）→ ``avg_r`` 取 5.0（保守上限，防 Kelly 拉到极端）；
        - 全负 / 净盈利非正 / 样本不足 → 返回 None（让 Kelly 走默认值）。
    """
    if len(trades) < min_n:
        return None

    wins = [float(t.pnl_usdt) for t in trades if t.pnl_usdt > 0]
    losses = [-float(t.pnl_usdt) for t in trades if t.pnl_usdt < 0]
    n_total = len(trades)
    if n_total == 0 or len(wins) == 0:
        return None

    win_rate = len(wins) / n_total
    avg_win = sum(wins) / len(wins)
    if losses:
        avg_loss = sum(losses) / len(losses)
        if avg_loss <= 0:
            return None
        avg_r = avg_win / avg_loss
    else:
        # 全胜（小样本）：avg_r 用保守上限 5.0，避免 Kelly 算出过大仓位
        avg_r = 5.0

    return win_rate, avg_r


def compute_daily_returns(
    equities: Sequence[EquitySnapshot],
) -> list[float]:
    """从净值快照算每日收益率。

    按 UTC 日切片，每日取最后一笔 equity；相邻两日 ``(today / yesterday) - 1``。

    Args:
        equities: 升序 equity 快照（PnLTracker.get_equities 默认即升序）。

    Returns:
        日收益序列；不足 2 日返回空。
    """
    if len(equities) < 2:
        return []

    by_day: dict[str, float] = {}
    for snap in equities:
        day = datetime.fromtimestamp(snap.ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        # 同一日的多笔，保留最后一笔（因为 equities 已升序）
        by_day[day] = float(snap.equity_usdt)

    sorted_days = sorted(by_day.keys())
    if len(sorted_days) < 2:
        return []

    out: list[float] = []
    prev = by_day[sorted_days[0]]
    for day in sorted_days[1:]:
        cur = by_day[day]
        if prev <= 0:
            out.append(0.0)
        else:
            out.append(cur / prev - 1.0)
        prev = cur
    return out


def compute_sharpe(
    returns: Sequence[float],
    *,
    periods_per_year: int = 365,
) -> float:
    """年化 Sharpe（无风险收益默认 0）。样本少于 2 或零方差返回 0。"""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    return mean / std * math.sqrt(periods_per_year)


def aggregate_pnl_by_strategy(
    trades: Sequence[TradeRecord],
) -> dict[str, float]:
    """按 strategy_id 聚合总 PnL（USDT）。供 daily_report 使用。"""
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        out[t.strategy_id] += float(t.pnl_usdt)
    return dict(out)


__all__ = [
    "aggregate_pnl_by_strategy",
    "compute_daily_returns",
    "compute_sharpe",
    "compute_win_rate_avg_r",
]
