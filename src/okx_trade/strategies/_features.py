"""跨策略特征工程（ml_fusion 用）。

把现有策略的中间状态（动量 / vol / funding skew / regime / book imbalance / ...）
聚合成一行 ``FeatureRow``，供 ml_fusion 当 XGBoost 输入。

设计原则：
- 所有特征**只用过去信息**（避免 look-ahead bias）。
- 计算函数纯函数，无 IO。
- 缺失特征用 0（XGBoost 容忍 NaN，但 0 是稳定 fallback）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Sequence


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """单个 instrument 在某时刻的特征向量。"""

    ts_ms: int
    inst_id: str
    momentum_1d: float = 0.0
    momentum_3d: float = 0.0
    momentum_7d: float = 0.0
    rv_30d: float = 0.0
    rv_pct: float = 0.0          # 当前 RV 在过去 365d 内的分位数
    funding_current: float = 0.0
    funding_z_30d: float = 0.0
    book_imbalance_avg: float = 0.0
    regime_state: int = 0        # 0=trending, 1=mean_reverting, 2=neutral
    btc_corr_30d: float = 0.0

    def as_list(self) -> list[float]:
        """转为 XGBoost feature 向量（顺序固定）。"""
        return [
            self.momentum_1d, self.momentum_3d, self.momentum_7d,
            self.rv_30d, self.rv_pct,
            self.funding_current, self.funding_z_30d,
            self.book_imbalance_avg,
            float(self.regime_state),
            self.btc_corr_30d,
        ]

    @staticmethod
    def feature_names() -> list[str]:
        return [
            "momentum_1d", "momentum_3d", "momentum_7d",
            "rv_30d", "rv_pct",
            "funding_current", "funding_z_30d",
            "book_imbalance_avg",
            "regime_state",
            "btc_corr_30d",
        ]


def momentum(closes: Sequence[float], lookback: int) -> float:
    """简单价格动量：(close_t / close_{t-lookback}) - 1。

    样本不足返回 0。
    """
    if len(closes) < lookback + 1:
        return 0.0
    return closes[-1] / closes[-1 - lookback] - 1.0


def realized_vol(closes: Sequence[float], window: int = 30, periods_per_year: int = 365) -> float:
    """年化 RV = stdev(log returns) × √(periods_per_year)。"""
    if len(closes) < window + 1:
        return 0.0
    log_rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - window, len(closes))
        if i > 0 and closes[i - 1] > 0
    ]
    if len(log_rets) < 2:
        return 0.0
    m = sum(log_rets) / len(log_rets)
    var = sum((r - m) ** 2 for r in log_rets) / (len(log_rets) - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def percentile_rank(value: float, history: Sequence[float]) -> float:
    """value 在 history 中的分位数（0 ~ 1）。history 不足返回 0.5。"""
    n = len(history)
    if n < 5:
        return 0.5
    below = sum(1 for h in history if h <= value)
    return below / n


def correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson 相关系数。xs/ys 长度不同时按短的截断。"""
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs = list(xs)[-n:]
    ys = list(ys)[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    s_xx = sum((x - mx) ** 2 for x in xs)
    s_yy = sum((y - my) ** 2 for y in ys)
    s_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denom = math.sqrt(s_xx * s_yy)
    if denom <= 0:
        return 0.0
    return s_xy / denom


__all__ = [
    "FeatureRow",
    "correlation",
    "momentum",
    "percentile_rank",
    "realized_vol",
]
