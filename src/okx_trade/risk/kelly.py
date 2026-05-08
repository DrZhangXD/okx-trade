"""Kelly fraction（保守版：1/4 Kelly）。

Kelly 公式
---------
对赌 R 倍赔率（赢得 R 美元 / 输 1 美元）的二元交易，最优下注比例：

    f* = (p × R - q) / R          # p=胜率, q=1-p, R=avg_win/avg_loss

完整 Kelly **理论上**最大化长期对数收益，但实际有两个问题：
1. **对参数估计极敏感**：``p`` 和 ``R`` 用历史样本估，置信区间很宽，full Kelly
   常常导致仓位过大；
2. **drawdown 心理承受**：full Kelly 一段时间内回撤可能达 50%+，多数人无法承受。

行业惯例是 **Fractional Kelly**：取 ``f = 0.25 × f*``（1/4 Kelly），保留约
80% 的预期长期增长率，但回撤减半，参数误差容忍度大幅提高。

接入方式
-------
- 策略层在 RiskManager 中放 ``KellyCheck(win_rate=0.55, avg_r=1.5, fraction=0.25)``；
- check 把 ``intent.size`` 缩放为 ``base_size × f``（其中 base_size 来自 vol_target
  或固定 risk_pct 的输出）；
- ``win_rate`` 和 ``avg_r`` 由策略离线统计 / 在线滚动估计后传入（M5 的 PnL tracker
  会维护这些）。
"""
from __future__ import annotations

from .base import RiskCheck, RiskCheckResult, RiskIntent


def kelly_fraction(win_rate: float, avg_r: float) -> float:
    """完整 Kelly fraction。

    Args:
        win_rate: 胜率 ∈ [0, 1]。
        avg_r: 平均盈亏比（``avg_win / avg_loss``，必须 > 0）。

    Returns:
        最优下注比例 f*。可能为负（含义：策略期望负收益，不应交易）。

    >>> abs(kelly_fraction(0.55, 1.5) - 0.25) < 0.001
    True
    """
    if not (0 < win_rate < 1):
        return 0.0
    if avg_r <= 0:
        return 0.0
    q = 1 - win_rate
    return (win_rate * avg_r - q) / avg_r


def quarter_kelly_size(
    base_size: float,
    win_rate: float,
    avg_r: float,
    *,
    fraction: float = 0.25,
    max_fraction: float = 0.5,
) -> float:
    """按 fractional Kelly 缩放仓位。

    Args:
        base_size: 策略原始仓位。
        win_rate / avg_r: Kelly 参数（来自策略历史统计）。
        fraction: Kelly 折扣（默认 0.25 = 1/4 Kelly）。
        max_fraction: full Kelly × fraction 后的上限（防极端 win_rate 导致过仓）。

    Returns:
        缩放后仓位。负 Kelly（策略不该交易）→ 0。
    """
    if base_size <= 0:
        return 0.0
    f_full = kelly_fraction(win_rate, avg_r)
    if f_full <= 0:
        return 0.0
    f = min(max_fraction, f_full * fraction)
    return base_size * f / max(f_full, 1e-9)  # 标度回到「相对 full Kelly 的比例」
    # 解释：base_size 假设是 full Kelly 下的仓位（策略已按 risk_pct 算出）；
    # 我们要的就是 base_size × fraction。但若 f_full < fraction，则取 f_full
    # 本身（不能超过 full Kelly）。


class KellyCheck(RiskCheck):
    """Kelly 风控 check。

    Attributes:
        win_rate: 策略胜率（可在线更新——M5 PnL tracker 会调 ``set_stats``）。
        avg_r: 平均盈亏比（同上）。
        fraction: Kelly 折扣，默认 0.25。
        max_fraction: 缩放上限。
    """

    name = "kelly"

    def __init__(
        self,
        *,
        win_rate: float = 0.5,
        avg_r: float = 1.0,
        fraction: float = 0.25,
        max_fraction: float = 0.5,
    ) -> None:
        self.win_rate = win_rate
        self.avg_r = avg_r
        self.fraction = fraction
        self.max_fraction = max_fraction

    def set_stats(self, *, win_rate: float, avg_r: float) -> None:
        """在线更新 Kelly 参数（PnL tracker 每 N 笔交易后调一次）。"""
        self.win_rate = win_rate
        self.avg_r = avg_r

    def check(self, intent: RiskIntent) -> RiskCheckResult:
        new_size = quarter_kelly_size(
            intent.size, win_rate=self.win_rate, avg_r=self.avg_r,
            fraction=self.fraction, max_fraction=self.max_fraction,
        )
        if new_size <= 0:
            return RiskCheckResult.reject(
                f"Kelly={kelly_fraction(self.win_rate, self.avg_r):.3f} (negative or zero)",
                check_name=self.name,
            )
        if abs(new_size - intent.size) / max(intent.size, 1e-9) < 0.05:
            return RiskCheckResult.approve(intent.size, check_name=self.name)
        return RiskCheckResult.scale(
            new_size,
            reason=f"win={self.win_rate:.1%} R={self.avg_r:.2f} "
                   f"f*={kelly_fraction(self.win_rate, self.avg_r):.3f} × {self.fraction}",
            check_name=self.name,
        )


__all__ = [
    "KellyCheck",
    "kelly_fraction",
    "quarter_kelly_size",
]
