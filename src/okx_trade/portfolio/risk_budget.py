"""Risk-budgeting allocator：inverse-vol weighting + correlation penalty。

公式
----
1. 各策略 daily_returns 的 std → 反波动率权重 ``w_i ∝ 1/σ_i``；
2. 对每对 (i, j) 算 |ρ_ij|；策略 i 的"平均相关性" ``c_i = mean_j(|ρ_ij|)``；
3. 惩罚因子 ``p_i = 1 - c_i``（c_i 越高，惩罚越重）；
4. 合成权重 ``w'_i = w_i × p_i``；归一化使 ``sum w'_i = 1``。

回退
----
- 任一策略 ``len(daily_returns) < min_history_days`` → 整体回退 ``EqualWeightAllocator``；
- 全部策略零波动 → 回退 equal weight；
- 仅 1 个策略 → 给 100%。

为什么不用 cvxpy / scipy 优化？
---------------------------
- 4 个策略的小规模问题，闭式解就够；
- 引入凸优化器对依赖和测试时间不划算；
- 这个权重也只是冷启动后的"近似"，paper trading 跑稳后可换成更复杂的口径。
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING

from ..pnl import compute_daily_returns
from ..risk.correlation import _pearson
from .base import Allocator
from .equal_weight import EqualWeightAllocator

if TYPE_CHECKING:
    from ..pnl import PnLTracker


def inverse_vol_weights(
    returns_by_strategy: dict[str, Sequence[float]],
) -> dict[str, float]:
    """对每个策略算 std，返回 ``1/std`` 归一化权重。

    全零方差 → 全 0；调用方应回退 equal weight。
    """
    stds: dict[str, float] = {}
    for sid, rets in returns_by_strategy.items():
        if len(rets) < 2:
            stds[sid] = 0.0
            continue
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        stds[sid] = math.sqrt(var)

    inv = {sid: (1.0 / s if s > 0 else 0.0) for sid, s in stds.items()}
    total = sum(inv.values())
    if total <= 0:
        return {sid: 0.0 for sid in inv}
    return {sid: v / total for sid, v in inv.items()}


def correlation_penalty(
    returns_by_strategy: dict[str, Sequence[float]],
) -> dict[str, float]:
    """每个策略的"对其他策略平均相关性"作为惩罚因子的输入；返回 ``1 - mean(|ρ|)``。

    单策略或全相关性数据缺失 → 全部返回 1.0（等同不惩罚）。
    """
    sids = sorted(returns_by_strategy.keys())
    if len(sids) <= 1:
        return {sid: 1.0 for sid in sids}

    out: dict[str, float] = {}
    for i, a in enumerate(sids):
        corrs: list[float] = []
        for j, b in enumerate(sids):
            if i == j:
                continue
            rho = _pearson(list(returns_by_strategy[a]), list(returns_by_strategy[b]))
            corrs.append(abs(rho))
        mean_corr = sum(corrs) / len(corrs) if corrs else 0.0
        out[a] = max(0.0, 1.0 - mean_corr)
    return out


def risk_budget_weights(
    returns_by_strategy: dict[str, Sequence[float]],
) -> dict[str, float] | None:
    """合成 inverse-vol × penalty → 归一化权重。

    Returns:
        ``None`` 若 inverse-vol 全 0（零方差）→ 调用方应回退 equal。
    """
    inv = inverse_vol_weights(returns_by_strategy)
    if sum(inv.values()) <= 0:
        return None
    pen = correlation_penalty(returns_by_strategy)
    raw = {sid: inv[sid] * pen[sid] for sid in inv}
    total = sum(raw.values())
    if total <= 0:
        return None
    return {sid: v / total for sid, v in raw.items()}


class RiskBudgetingAllocator(Allocator):
    """inverse-vol + correlation-penalty allocator（M5 主推 ≥30 日切换）。

    Args:
        min_history_days: 触发使用 risk-budget 口径的最小历史天数；不足回退
            ``EqualWeightAllocator``。默认 30。
    """

    def __init__(self, *, min_history_days: int = 30) -> None:
        self.min_history_days = min_history_days
        self._fallback = EqualWeightAllocator()

    def allocate(
        self,
        strategy_ids: list[str],
        tracker: PnLTracker,
        *,
        total_equity_usdt: Decimal,
    ) -> dict[str, Decimal]:
        if not strategy_ids:
            raise ValueError("strategy_ids must be non-empty")
        if len(strategy_ids) == 1:
            return {strategy_ids[0]: total_equity_usdt}

        returns_by_strategy: dict[str, Sequence[float]] = {}
        for sid in strategy_ids:
            equities = tracker.get_equities(sid)
            rets = compute_daily_returns(equities)
            returns_by_strategy[sid] = rets

        # 任一策略历史不足 → 回退
        if any(len(rets) < self.min_history_days for rets in returns_by_strategy.values()):
            return self._fallback.allocate(
                strategy_ids, tracker, total_equity_usdt=total_equity_usdt,
            )

        weights = risk_budget_weights(returns_by_strategy)
        if weights is None:
            return self._fallback.allocate(
                strategy_ids, tracker, total_equity_usdt=total_equity_usdt,
            )

        out: dict[str, Decimal] = {}
        running = Decimal("0")
        for sid in strategy_ids[:-1]:
            usd = (total_equity_usdt * Decimal(str(weights[sid]))).quantize(Decimal("0.01"))
            out[sid] = usd
            running += usd
        # 最后一个吃残差
        out[strategy_ids[-1]] = total_equity_usdt - running
        return out


__all__ = [
    "RiskBudgetingAllocator",
    "correlation_penalty",
    "inverse_vol_weights",
    "risk_budget_weights",
]
