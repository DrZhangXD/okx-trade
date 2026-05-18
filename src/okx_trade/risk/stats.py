"""统计工具：β / 协整 / OU 拟合。

为什么单独一个 module？
- 多个策略（funding_cross_section / stat_arb_pairs / ml_fusion）需要相同的纯统计
- `risk/correlation.py` 已经有手写 Pearson，但更高阶（β、ADF、OU）单独抽离更清晰
- 不强依赖 numpy/scipy/statsmodels：基础 fn 纯 Python；statsmodels 是 optional dep
  （``[stat-arb]`` extra），未装时降级为手写 ADF 单滞后近似
"""
from __future__ import annotations

import math
from typing import Sequence


# ---------------------------------------------------------------------------
# OLS β / linear regression
# ---------------------------------------------------------------------------


def linreg(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """简单 OLS：y = α + β x。

    Returns:
        ``(alpha, beta, r2)``。``r2`` 是判定系数（拟合优度）。
        样本不足 2 返回 ``(0, 0, 0)``。
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0, 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    s_xx = sum((x - mx) ** 2 for x in xs)
    s_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    s_yy = sum((y - my) ** 2 for y in ys)
    if s_xx <= 0:
        return my, 0.0, 0.0
    beta = s_xy / s_xx
    alpha = my - beta * mx
    r2 = (s_xy * s_xy) / (s_xx * s_yy) if s_yy > 0 else 0.0
    return alpha, beta, r2


def rolling_beta(
    asset_returns: Sequence[float],
    market_returns: Sequence[float],
    window: int | None = None,
) -> float:
    """asset vs market 的 β（β = Cov / Var(market)）。

    若 ``window`` 给定，取最近 ``window`` 个点。返回 0 = 中性 / 数据不足。
    """
    if window is not None and window > 0:
        asset_returns = list(asset_returns)[-window:]
        market_returns = list(market_returns)[-window:]
    _, beta, _ = linreg(market_returns, asset_returns)
    return beta


# ---------------------------------------------------------------------------
# Cointegration: Engle-Granger
# ---------------------------------------------------------------------------


def _adf_pvalue_simple(series: Sequence[float]) -> float:
    """**降级版** ADF 检验：单滞后回归 ΔX_t = ρ X_{t-1} + ε_t。

    返回近似 p-value。``statsmodels.tsa.stattools.adfuller`` 才是正统；这里只是
    给没装 statsmodels 的用户用的简化版（精度差，但能区分明显非平稳）。

    回归系数 ρ ≈ 0 → 非平稳（随机游走）；ρ << 0 → 平稳。
    用 t-stat 的近似 p：t < -3.0 → p≈0.01；< -2.5 → 0.05；< -2.0 → 0.10；其他 → 0.5
    """
    n = len(series)
    if n < 20:
        return 1.0
    xs = list(series)[:-1]
    dy = [series[i + 1] - series[i] for i in range(n - 1)]
    _, rho, _ = linreg(xs, dy)
    # 标准差近似（简化）
    pred = [rho * x for x in xs]
    residuals = [dy[i] - pred[i] for i in range(len(dy))]
    rss = sum(r * r for r in residuals)
    sx2 = sum((x - sum(xs) / len(xs)) ** 2 for x in xs)
    if sx2 <= 0 or rss <= 0:
        return 1.0
    se_rho = math.sqrt(rss / (len(dy) - 1) / sx2)
    if se_rho <= 0:
        return 1.0
    t_stat = rho / se_rho
    # 近似查表（Dickey-Fuller t-分布；样本 100 时的近似临界值）
    if t_stat < -3.5:
        return 0.005
    if t_stat < -3.0:
        return 0.02
    if t_stat < -2.5:
        return 0.06
    if t_stat < -2.0:
        return 0.15
    if t_stat < -1.5:
        return 0.30
    return 0.50


def adf_pvalue(series: Sequence[float]) -> float:
    """ADF p-value。优先用 statsmodels，未装则降级。"""
    try:
        from statsmodels.tsa.stattools import adfuller  # type: ignore
        result = adfuller(list(series), autolag="AIC")
        return float(result[1])
    except ImportError:
        return _adf_pvalue_simple(series)
    except Exception:
        return _adf_pvalue_simple(series)


def engle_granger_coint(
    s1: Sequence[float],
    s2: Sequence[float],
) -> tuple[float, float, float]:
    """Engle-Granger 两步协整检验。

    Step 1: OLS s1 = α + β·s2 + ε
    Step 2: ADF test on residuals ε

    Returns:
        ``(hedge_ratio_beta, residual_adf_pvalue, half_life)``
        - ``hedge_ratio_beta``：spread = s1 - β·s2 的对冲比；
        - ``residual_adf_pvalue``：< 0.05 视为协整；
        - ``half_life``：用 OU 拟合估计 spread 半衰期（天 / bar）；NaN 时返回 0。

    样本不足返回 ``(0, 1, 0)``（视为不协整）。
    """
    if len(s1) != len(s2) or len(s1) < 30:
        return 0.0, 1.0, 0.0
    _, beta, _ = linreg(list(s2), list(s1))
    if beta == 0:
        return 0.0, 1.0, 0.0
    spread = [s1[i] - beta * s2[i] for i in range(len(s1))]
    pvalue = adf_pvalue(spread)
    hl = ou_half_life(spread)
    return beta, pvalue, hl


# ---------------------------------------------------------------------------
# OU process fitting
# ---------------------------------------------------------------------------


def ou_fit(spread: Sequence[float]) -> tuple[float, float, float]:
    """估计 OU 参数 dX = θ(μ - X)dt + σdW。

    用闭式解：把 X_{t+1} - X_t = θ(μ - X_t)·Δt + σ√Δt·ε 当作
    AR(1)：X_{t+1} = a + b·X_t + ε，其中
        b = 1 - θ·Δt → θ = (1 - b) / Δt
        a = θ·μ·Δt → μ = a / (θ·Δt)
        σ = std(ε) / √Δt
    这里 Δt = 1（spread 是按 bar 索引等距采样）。

    Returns:
        ``(mu, theta, sigma)``。数据不足返回 ``(mean, 0, std)``（θ=0 表示无回归）。
    """
    n = len(spread)
    if n < 10:
        if n == 0:
            return 0.0, 0.0, 0.0
        m = sum(spread) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in spread) / max(1, n - 1))
        return m, 0.0, sd
    xs = list(spread)[:-1]
    ys = list(spread)[1:]
    a, b, _ = linreg(xs, ys)
    theta = 1.0 - b
    if abs(theta) < 1e-9:
        m = sum(spread) / n
        return m, 0.0, math.sqrt(sum((x - m) ** 2 for x in spread) / (n - 1))
    mu = a / theta
    residuals = [ys[i] - (a + b * xs[i]) for i in range(len(xs))]
    sigma = math.sqrt(sum(r * r for r in residuals) / max(1, len(residuals) - 2))
    return mu, theta, sigma


def ou_half_life(spread: Sequence[float]) -> float:
    """OU 半衰期 = ln(2) / θ（同 spread 的时间单位）。

    ``θ <= 0`` 表示无均值回归，返回 0（视为无效）。
    """
    _, theta, _ = ou_fit(spread)
    if theta <= 0:
        return 0.0
    return math.log(2) / theta


# ---------------------------------------------------------------------------
# Z-score
# ---------------------------------------------------------------------------


def zscore(value: float, history: Sequence[float]) -> float:
    """value 在 history 分布中的 z-score。history 样本不足返回 0。"""
    n = len(history)
    if n < 3:
        return 0.0
    m = sum(history) / n
    var = sum((x - m) ** 2 for x in history) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd <= 0:
        return 0.0
    return (value - m) / sd


__all__ = [
    "adf_pvalue",
    "engle_granger_coint",
    "linreg",
    "ou_fit",
    "ou_half_life",
    "rolling_beta",
    "zscore",
]
