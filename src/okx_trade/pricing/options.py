"""Black-Scholes 期权定价 + Greeks（纯 Python）。

约定：
- 所有 vol 是年化波动率（如 0.5 = 50%）
- ``tenor_years`` 是距到期年化时间（如 30/365 = 30 天）
- ``rate`` 是无风险年化连续复利（crypto 一般用 0；保留参数兼容传统期权）
- ``opt_type`` ∈ ``"C"`` / ``"P"``

为什么纯 Python？
- 不引 numpy/scipy，便于回测 fixture / 单测引入
- 期权数学公式不长，30 行内 OK
- 调用频率不高（每小时计算 ~5 个 strike × 2 types），性能不是瓶颈

对照 Hull《Options, Futures, and Other Derivatives》第 6 章 / Haug《Option Pricing Formulas》。
"""
from __future__ import annotations

import math
from typing import Literal

OptionType = Literal["C", "P"]

_SQRT_2PI = math.sqrt(2 * math.pi)
_INV_SQRT_2 = 1.0 / math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """标准正态分布 CDF：N(x) = 0.5 (1 + erf(x/√2))。"""
    return 0.5 * (1.0 + math.erf(x * _INV_SQRT_2))


def norm_pdf(x: float) -> float:
    """标准正态分布 PDF：φ(x) = e^(-x²/2) / √(2π)。"""
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _d1_d2(
    spot: float, strike: float, tenor_years: float, vol: float, rate: float,
) -> tuple[float, float]:
    """BS d1, d2。退化处理：tenor 或 vol 接近 0 时返回 (+∞, +∞) 或 (-∞, -∞)。"""
    if tenor_years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        # 退化：到期或零波动 → call 价值 = max(S-K, 0)，put = max(K-S, 0)
        # 用 ±inf 让 N(d1) / N(d2) 退化到 0/1
        if spot > strike:
            return float("inf"), float("inf")
        return float("-inf"), float("-inf")
    sigma_sqrt_t = vol * math.sqrt(tenor_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * tenor_years) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    return d1, d2


def bs_price(
    spot: float,
    strike: float,
    tenor_years: float,
    vol: float,
    rate: float = 0.0,
    opt_type: OptionType = "C",
) -> float:
    """BS 理论价。"""
    d1, d2 = _d1_d2(spot, strike, tenor_years, vol, rate)
    disc_k = strike * math.exp(-rate * max(tenor_years, 0.0))
    if opt_type == "C":
        return spot * norm_cdf(d1) - disc_k * norm_cdf(d2)
    return disc_k * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(
    spot: float, strike: float, tenor_years: float, vol: float,
    rate: float = 0.0, opt_type: OptionType = "C",
) -> float:
    """Δ = ∂P/∂S。call ∈ [0, 1]，put ∈ [-1, 0]。"""
    d1, _ = _d1_d2(spot, strike, tenor_years, vol, rate)
    if opt_type == "C":
        return norm_cdf(d1)
    return norm_cdf(d1) - 1.0


def bs_gamma(
    spot: float, strike: float, tenor_years: float, vol: float, rate: float = 0.0,
) -> float:
    """Γ = ∂²P/∂S²。call / put 相同。"""
    if tenor_years <= 0 or vol <= 0 or spot <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, tenor_years, vol, rate)
    return norm_pdf(d1) / (spot * vol * math.sqrt(tenor_years))


def bs_vega(
    spot: float, strike: float, tenor_years: float, vol: float, rate: float = 0.0,
) -> float:
    """Vega = ∂P/∂σ（按 1 单位 vol，即 100%）。习惯上除 100 得每 1% vol 的价格变化。"""
    if tenor_years <= 0 or spot <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, tenor_years, vol, rate)
    return spot * norm_pdf(d1) * math.sqrt(tenor_years)


def bs_theta(
    spot: float, strike: float, tenor_years: float, vol: float,
    rate: float = 0.0, opt_type: OptionType = "C",
) -> float:
    """Θ = ∂P/∂T（注意：约定 T 是到到期的时间，所以 θ 实际是负值，表示时间流逝吃掉价值）。

    返回**每年**的时间衰减；除 365 得每日。
    """
    if tenor_years <= 0 or vol <= 0 or spot <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, tenor_years, vol, rate)
    pdf_d1 = norm_pdf(d1)
    sqrt_t = math.sqrt(tenor_years)
    term1 = -(spot * pdf_d1 * vol) / (2 * sqrt_t)
    disc_k = strike * math.exp(-rate * tenor_years)
    if opt_type == "C":
        return term1 - rate * disc_k * norm_cdf(d2)
    return term1 + rate * disc_k * norm_cdf(-d2)


def implied_vol(
    market_price: float,
    spot: float,
    strike: float,
    tenor_years: float,
    rate: float = 0.0,
    opt_type: OptionType = "C",
    *,
    tol: float = 1e-6,
    max_iter: int = 50,
    init_vol: float = 0.5,
) -> float | None:
    """从 market price 反推 IV（Newton-Raphson）。

    收敛失败（vega 太小 / iter 超限）返回 None。
    """
    if tenor_years <= 0 or spot <= 0 or strike <= 0 or market_price <= 0:
        return None
    # 期权价的理论下限：call ≥ S - K·e^(-rT)；low bound 违反就无解
    intrinsic = max(0.0, spot - strike * math.exp(-rate * tenor_years)) if opt_type == "C" \
        else max(0.0, strike * math.exp(-rate * tenor_years) - spot)
    if market_price < intrinsic - tol:
        return None
    vol = init_vol
    for _ in range(max_iter):
        price = bs_price(spot, strike, tenor_years, vol, rate, opt_type)
        vega = bs_vega(spot, strike, tenor_years, vol, rate)
        if vega < 1e-10:
            return None
        diff = price - market_price
        if abs(diff) < tol:
            return vol
        vol -= diff / vega
        if vol <= 0:
            vol = 0.01
        if vol > 5.0:  # >500% IV，crypto 极端但仍属可能
            vol = 5.0
    return None


def vol_premium(iv: float, realized_vol: float) -> float:
    """波动率溢价 = IV / RV - 1。

    正值 = IV > RV，卖期权有 carry；
    负值 = IV < RV，买期权占便宜。

    realized_vol 为 0 时返回 0（避免除零）。
    """
    if realized_vol <= 0:
        return 0.0
    return iv / realized_vol - 1.0


__all__ = [
    "OptionType",
    "bs_delta",
    "bs_gamma",
    "bs_price",
    "bs_theta",
    "bs_vega",
    "implied_vol",
    "norm_cdf",
    "norm_pdf",
    "vol_premium",
]
