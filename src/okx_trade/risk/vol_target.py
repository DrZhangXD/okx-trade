"""波动率目标（vol targeting）：把单笔仓位按 N 日 realized vol 反推。

直觉
----
固定风险百分比（如「单笔风险 0.5% 净值」）在低波动期容易仓位过小、收益不足；高波动期
仓位过大、风险失控。把仓位按 1/realized_vol 缩放，**让组合年化波动率收敛到一个目标值**
（如 15%/年，对应每日 1%）。

学术界标准做法（vol-managed portfolio）能把 Sharpe 从 0.8 拉到 1.2-2.0，对 crypto
这类 vol-of-vol 极端的市场尤其有效。

公式
----
``target_vol_per_unit_size = target_vol_annualized / realized_vol_annualized × base_size``

具体：
1. 估算 N 日（默认 20 日）日收益的标准差 ``σ_d``，年化为 ``σ_y = σ_d × √365``；
2. 缩放因子 ``k = target_vol_annualized / σ_y``；
3. 新仓位 = ``base_size × k``，上下截断到 ``[size_min, size_max]``。

为什么 365 而不是 252？
- crypto 7×24 交易，年化用日历天数（365 / 252 都见过，社区习惯用 365 或 24×365）。

策略接入
-------
- 策略层先按自己的 ``risk_pct`` 算 ``base_size``；
- ``VolTargetCheck`` 在 RiskManager pipeline 里拿到 ``RiskIntent``，按其
  ``instrument_id`` 查询自己维护的 ``returns_history``，反推 scale；
- 返回 ``SCALE``（仓位调整）或 ``APPROVE``（vol 在合理范围）。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

from .base import RiskCheck, RiskCheckResult, RiskIntent


def realized_vol_annualized(returns: list[float], *, periods_per_year: int = 365) -> float:
    """N 日 realized vol（年化）。

    Args:
        returns: 日收益序列（``r_t = (close_t / close_{t-1}) - 1``）。
        periods_per_year: 年化乘数；crypto 默认 365（日数据）。换 8h funding 数据
            用 ``3 × 365 = 1095``，etc.

    Returns:
        年化标准差（如 0.60 = 60% APR vol）。返回 0.0 表示样本不足或全是常数。
    """
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return 0.0
    daily_vol = math.sqrt(var)
    return daily_vol * math.sqrt(periods_per_year)


def vol_target_position_size(
    base_size: float,
    realized_vol: float,
    target_vol: float,
    *,
    max_scale: float = 2.0,
    min_scale: float = 0.0,
) -> float:
    """按 vol target 缩放仓位。

    Args:
        base_size: 策略原始仓位（合约张数 / 现货数量）。
        realized_vol: 实际年化波动率（``realized_vol_annualized`` 输出）。
        target_vol: 目标年化波动率（典型 0.15 = 15%）。
        max_scale: 缩放因子上限（防止低 vol 时仓位爆炸；默认 2x base_size）。
        min_scale: 缩放因子下限（默认 0，即可以完全不下单）。

    Returns:
        新仓位（已截断到 [min_scale, max_scale] × base_size）。
    """
    if realized_vol <= 0 or target_vol <= 0 or base_size <= 0:
        return 0.0
    scale = target_vol / realized_vol
    scale = max(min_scale, min(max_scale, scale))
    return base_size * scale


class VolTargetCheck(RiskCheck):
    """vol targeting risk check。

    按 ``instrument_id`` 维护各自的 returns 历史（rolling window）。策略层应在
    每根 bar close 时调 ``update_return(inst_id, ret)`` 喂数据。

    Attributes:
        target_vol_annualized: 目标年化波动率（如 0.15 = 15%）。
        window: 计算 realized vol 的 lookback 窗口（默认 20 日）。
        max_scale / min_scale: 缩放因子的上下限。
        periods_per_year: 年化乘数（crypto 默认 365）。
    """

    name = "vol_target"

    def __init__(
        self,
        *,
        target_vol_annualized: float = 0.15,
        window: int = 20,
        max_scale: float = 2.0,
        min_scale: float = 0.1,
        periods_per_year: int = 365,
    ) -> None:
        self.target_vol = target_vol_annualized
        self.window = window
        self.max_scale = max_scale
        self.min_scale = min_scale
        self.periods_per_year = periods_per_year
        self._returns: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self.window)
        )

    def update_return(self, instrument_id: str, daily_return: float) -> None:
        """喂新一根 bar 的 daily return（``close/prev_close - 1``）。"""
        self._returns[instrument_id].append(daily_return)

    def check(self, intent: RiskIntent) -> RiskCheckResult:
        rets = list(self._returns.get(intent.instrument_id, []))
        if len(rets) < max(2, self.window // 4):
            # 样本不足以估 vol：放行原仓位（让其他 check 决定）
            return RiskCheckResult.approve(intent.size, check_name=self.name)

        realized = realized_vol_annualized(rets, periods_per_year=self.periods_per_year)
        new_size = vol_target_position_size(
            intent.size, realized_vol=realized, target_vol=self.target_vol,
            max_scale=self.max_scale, min_scale=self.min_scale,
        )
        if new_size <= 0:
            return RiskCheckResult.reject(
                f"vol={realized:.2%} → scale=0", check_name=self.name,
            )
        if abs(new_size - intent.size) / max(intent.size, 1e-9) < 0.05:
            # 调整 < 5%，认为 APPROVE 即可（避免 SCALE 把日志刷爆）
            return RiskCheckResult.approve(intent.size, check_name=self.name)
        return RiskCheckResult.scale(
            new_size,
            reason=f"realized={realized:.2%}/y target={self.target_vol:.2%}/y "
                   f"scale={self.target_vol / realized:.2f}",
            check_name=self.name,
        )


__all__ = [
    "VolTargetCheck",
    "realized_vol_annualized",
    "vol_target_position_size",
]
