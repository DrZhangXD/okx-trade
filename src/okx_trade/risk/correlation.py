"""滚动 N 日策略 PnL 相关性 + 高相关时降权。

直觉
----
多策略组合的真实风险来自相关性：4 个 ρ=0.9 的策略 ≈ 1 个策略放大 4 倍。
- 滚动 N 日（默认 30）算策略两两 PnL 相关性矩阵；
- 任意一对 ρ > 阈值（默认 0.7）→ 给该策略对降权（仓位 × 0.5）；
- 多策略组合（M5）的资金分配会用风险预算重算，这里只在单策略下单时**临时缩仓**。

设计
----
- ``correlation_matrix(returns_dict)`` 纯函数：dict[strategy_id, list[returns]]
  → numpy ``(N, N)`` 矩阵（手写实现避免引入 numpy 进 risk 层）。
- ``CorrelationCheck`` 维护各策略的 daily PnL 历史（rolling deque）；
- ``check`` 时按 intent.strategy_id 找出与所有其他策略中**最大相关性**，
  超阈值就缩仓。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping

from .base import RiskCheck, RiskCheckResult, RiskIntent


def _pearson(xs: list[float], ys: list[float]) -> float:
    """皮尔逊相关系数（手写避免 numpy 依赖）。"""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(sxx * syy)
    if denom <= 0:
        return 0.0
    return sxy / denom


def correlation_matrix(returns: Mapping[str, list[float]]) -> dict[tuple[str, str], float]:
    """两两相关系数。返回 ``{(id_a, id_b): ρ}``，仅包含 a < b（避免对称重复）。"""
    keys = sorted(returns.keys())
    out: dict[tuple[str, str], float] = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            out[(a, b)] = _pearson(list(returns[a]), list(returns[b]))
    return out


def correlation_weight_adjustment(
    target_strategy: str,
    other_returns: Mapping[str, list[float]],
    target_returns: list[float],
    *,
    threshold: float = 0.7,
    high_corr_scale: float = 0.5,
) -> float:
    """对单策略：找它与其他策略的最大相关性，超阈值返回 ``high_corr_scale``，
    否则返回 1.0（不缩仓）。"""
    max_corr = 0.0
    for other_id, other_rets in other_returns.items():
        if other_id == target_strategy:
            continue
        rho = abs(_pearson(target_returns, list(other_rets)))
        max_corr = max(max_corr, rho)
    return high_corr_scale if max_corr > threshold else 1.0


class CorrelationCheck(RiskCheck):
    """滚动相关性 check。

    Attributes:
        window: 滚动天数，默认 30。
        threshold: 相关性阈值，默认 0.7。
        high_corr_scale: 触发时的仓位缩放因子，默认 0.5。
    """

    name = "correlation"

    def __init__(
        self,
        *,
        window: int = 30,
        threshold: float = 0.7,
        high_corr_scale: float = 0.5,
    ) -> None:
        self.window = window
        self.threshold = threshold
        self.high_corr_scale = high_corr_scale
        self._returns: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.window)
        )

    def update_strategy_pnl(self, strategy_id: str, daily_return: float) -> None:
        """喂某策略当日的 daily return（用 PnL / equity 当代理）。"""
        self._returns[strategy_id].append(daily_return)

    def check(self, intent: RiskIntent) -> RiskCheckResult:
        target_rets = list(self._returns.get(intent.strategy_id, []))
        if len(target_rets) < max(2, self.window // 4):
            # 样本不够，放行
            return RiskCheckResult.approve(intent.size, check_name=self.name)

        other = {sid: list(rets) for sid, rets in self._returns.items()
                 if sid != intent.strategy_id}
        if not other:
            return RiskCheckResult.approve(intent.size, check_name=self.name)

        scale = correlation_weight_adjustment(
            intent.strategy_id, other, target_rets,
            threshold=self.threshold, high_corr_scale=self.high_corr_scale,
        )
        if scale >= 1.0:
            return RiskCheckResult.approve(intent.size, check_name=self.name)
        return RiskCheckResult.scale(
            intent.size * scale,
            reason=f"high corr (>{self.threshold}) with peer; scale={scale}",
            check_name=self.name,
        )


__all__ = [
    "CorrelationCheck",
    "correlation_matrix",
    "correlation_weight_adjustment",
]
