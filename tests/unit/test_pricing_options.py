"""Black-Scholes pricer 数值精度单测。

对照 Hull《Options, Futures, and Other Derivatives》第 6 章经典例子。
"""
from __future__ import annotations

import math

import pytest

from okx_trade.pricing.options import (
    bs_delta,
    bs_gamma,
    bs_price,
    bs_theta,
    bs_vega,
    implied_vol,
    norm_cdf,
    norm_pdf,
    vol_premium,
)


class TestNormalDistribution:
    def test_cdf_zero_is_half(self) -> None:
        assert norm_cdf(0.0) == pytest.approx(0.5)

    def test_cdf_extremes(self) -> None:
        assert norm_cdf(-10.0) == pytest.approx(0.0, abs=1e-9)
        assert norm_cdf(10.0) == pytest.approx(1.0, abs=1e-9)

    def test_cdf_one_sigma(self) -> None:
        # N(1) ≈ 0.8413
        assert norm_cdf(1.0) == pytest.approx(0.8413, abs=1e-3)

    def test_pdf_peak_at_zero(self) -> None:
        # φ(0) = 1/√(2π) ≈ 0.3989
        assert norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2 * math.pi))


class TestBSPriceHullExample:
    """Hull 第 14 章 example 14.6: spot=42, K=40, T=0.5, σ=20%, r=10%。

    教材给出 call=4.76, put=0.81。
    """

    def test_call_price(self) -> None:
        p = bs_price(spot=42, strike=40, tenor_years=0.5, vol=0.20, rate=0.10, opt_type="C")
        assert p == pytest.approx(4.7594, abs=0.01)

    def test_put_price(self) -> None:
        p = bs_price(spot=42, strike=40, tenor_years=0.5, vol=0.20, rate=0.10, opt_type="P")
        assert p == pytest.approx(0.8086, abs=0.01)

    def test_put_call_parity(self) -> None:
        # C - P = S - K·e^(-rT)
        spot, strike, T, vol, r = 42, 40, 0.5, 0.20, 0.10
        c = bs_price(spot, strike, T, vol, r, "C")
        p = bs_price(spot, strike, T, vol, r, "P")
        rhs = spot - strike * math.exp(-r * T)
        assert (c - p) == pytest.approx(rhs, abs=1e-6)


class TestBSGreeks:
    """ATM 期权 Greeks（spot=100, K=100, T=1, σ=20%, r=0）。"""

    def test_atm_call_delta_around_half(self) -> None:
        d = bs_delta(100, 100, 1.0, 0.20, opt_type="C")
        # 略大于 0.5 因为 d1 > 0 in absence of dividends
        assert 0.50 < d < 0.60

    def test_atm_put_delta_around_neg_half(self) -> None:
        d = bs_delta(100, 100, 1.0, 0.20, opt_type="P")
        assert -0.50 < d < -0.40

    def test_gamma_positive(self) -> None:
        g = bs_gamma(100, 100, 1.0, 0.20)
        assert g > 0
        assert g == pytest.approx(0.0199, abs=1e-3)  # closed-form

    def test_vega_positive(self) -> None:
        # ATM (S=K=100, T=1, σ=0.20, r=0): d1=0.1, vega = S·φ(0.1)·√T ≈ 39.695
        v = bs_vega(100, 100, 1.0, 0.20)
        assert v > 0
        assert v == pytest.approx(39.695, abs=0.05)

    def test_theta_negative(self) -> None:
        # 时间衰减 → theta < 0 for long options
        t = bs_theta(100, 100, 1.0, 0.20, rate=0.05, opt_type="C")
        assert t < 0


class TestImpliedVol:
    def test_recovers_input_vol(self) -> None:
        # 用 σ=30% 算价 → 用价反推 IV → 应该回到 30%
        true_vol = 0.30
        market_price = bs_price(100, 100, 0.25, true_vol, 0.0, "C")
        iv = implied_vol(market_price, 100, 100, 0.25, opt_type="C")
        assert iv is not None
        assert iv == pytest.approx(true_vol, abs=1e-4)

    def test_below_intrinsic_returns_none(self) -> None:
        # call intrinsic = S - K = 10；价 < 10 不合理
        iv = implied_vol(5.0, spot=100, strike=90, tenor_years=0.5, opt_type="C")
        assert iv is None

    def test_zero_tenor_returns_none(self) -> None:
        assert implied_vol(5.0, 100, 100, 0.0) is None


class TestEdgeCases:
    def test_zero_tenor_price_is_intrinsic(self) -> None:
        # T=0 → 期权价 = max(S-K, 0) for call
        c = bs_price(110, 100, 0.0, 0.30, 0.0, "C")
        # 我们的实现 T=0 退化为 ±inf 然后 N(inf)=1 → spot * 1 - strike * 1 = 10
        assert c == pytest.approx(10.0, abs=1e-6)

    def test_zero_vol_returns_intrinsic(self) -> None:
        # σ=0 + ITM → 退化到 intrinsic
        c = bs_price(110, 100, 1.0, 0.0, 0.0, "C")
        assert c == pytest.approx(10.0, abs=1e-6)


class TestVolPremium:
    def test_positive_when_iv_above_rv(self) -> None:
        # IV=0.5, RV=0.4 → premium = 25%
        assert vol_premium(0.5, 0.4) == pytest.approx(0.25)

    def test_negative_when_iv_below_rv(self) -> None:
        assert vol_premium(0.3, 0.4) == pytest.approx(-0.25)

    def test_zero_rv_returns_zero(self) -> None:
        assert vol_premium(0.5, 0.0) == 0.0
