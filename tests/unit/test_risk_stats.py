"""risk/stats.py 单测：β / 协整 / OU。"""
from __future__ import annotations

import math
import random

import pytest

from okx_trade.risk.stats import (
    adf_pvalue,
    engle_granger_coint,
    linreg,
    ou_fit,
    ou_half_life,
    rolling_beta,
    zscore,
)


class TestLinreg:
    def test_perfect_linear(self) -> None:
        # y = 2 + 3x
        xs = [1, 2, 3, 4, 5]
        ys = [5, 8, 11, 14, 17]
        alpha, beta, r2 = linreg(xs, ys)
        assert alpha == pytest.approx(2.0, abs=1e-9)
        assert beta == pytest.approx(3.0, abs=1e-9)
        assert r2 == pytest.approx(1.0, abs=1e-9)

    def test_insufficient_samples(self) -> None:
        assert linreg([], []) == (0, 0, 0)
        assert linreg([1], [2]) == (0, 0, 0)

    def test_horizontal_data(self) -> None:
        # x 全 1 → s_xx = 0 → beta 退化
        alpha, beta, r2 = linreg([1, 1, 1], [10, 20, 30])
        assert beta == 0.0


class TestRollingBeta:
    def test_beta_close_to_one_for_perfect_correlation(self) -> None:
        # asset 与 market 完全同步
        market = [0.01, -0.005, 0.02, 0.0, -0.01]
        asset = market.copy()
        beta = rolling_beta(asset, market)
        assert beta == pytest.approx(1.0, abs=1e-6)

    def test_beta_two_for_double_movement(self) -> None:
        market = [0.01, -0.005, 0.02, 0.0, -0.01]
        asset = [r * 2 for r in market]
        beta = rolling_beta(asset, market)
        assert beta == pytest.approx(2.0, abs=1e-6)

    def test_window_truncation(self) -> None:
        # 给 30 个数据，限定 window=10
        rng = random.Random(42)
        market = [rng.gauss(0, 0.01) for _ in range(30)]
        asset = [r + rng.gauss(0, 0.001) for r in market]
        beta_full = rolling_beta(asset, market)
        beta_w10 = rolling_beta(asset, market, window=10)
        # 两个都应接近 1，但具体值不同（不同样本）
        assert 0.5 < beta_full < 1.5
        assert 0.5 < beta_w10 < 1.5


class TestZScore:
    def test_value_at_mean_is_zero(self) -> None:
        assert zscore(5.0, [4, 5, 6]) == pytest.approx(0.0)

    def test_value_above_mean(self) -> None:
        # mean=5, std≈1 → (7-5)/1 = 2.0
        z = zscore(7.0, [4, 5, 6])
        assert z == pytest.approx(2.0, abs=1e-3)

    def test_insufficient_history(self) -> None:
        assert zscore(5.0, []) == 0.0
        assert zscore(5.0, [1, 2]) == 0.0  # n < 3


class TestADFAndCointegration:
    def test_random_walk_high_pvalue(self) -> None:
        # 随机游走是单位根 → ADF p 应该高
        rng = random.Random(42)
        rw = [0.0]
        for _ in range(100):
            rw.append(rw[-1] + rng.gauss(0, 1))
        p = adf_pvalue(rw)
        assert p > 0.1  # 不平稳

    def test_white_noise_low_pvalue(self) -> None:
        # 白噪声是平稳的 → p 应该 ≤ 0.1
        rng = random.Random(7)
        wn = [rng.gauss(0, 1) for _ in range(100)]
        p = adf_pvalue(wn)
        assert p < 0.10

    def test_cointegrated_pair(self) -> None:
        # 构造协整对：y = 2x + stationary noise
        rng = random.Random(123)
        x = [0.0]
        for _ in range(150):
            x.append(x[-1] + rng.gauss(0, 1))  # 随机游走
        y = [2 * x[i] + rng.gauss(0, 0.5) for i in range(len(x))]
        beta, pvalue, hl = engle_granger_coint(y, x)
        assert abs(beta - 2.0) < 0.1
        assert pvalue < 0.10
        assert hl >= 0

    def test_non_cointegrated_pair(self) -> None:
        # 两条独立随机游走 → 不协整
        rng = random.Random(7)
        x = [0.0]
        y = [0.0]
        for _ in range(100):
            x.append(x[-1] + rng.gauss(0, 1))
            y.append(y[-1] + rng.gauss(0, 1))
        _, pvalue, _ = engle_granger_coint(x, y)
        # p 较高（非协整），但 simple ADF 不一定每次都给出 > 0.5
        assert pvalue > 0.05  # 至少不显著协整

    def test_insufficient_samples(self) -> None:
        beta, p, hl = engle_granger_coint([1, 2, 3], [4, 5, 6])
        assert (beta, p, hl) == (0.0, 1.0, 0.0)


class TestOUFit:
    def test_stationary_series_has_positive_theta(self) -> None:
        # 构造 OU 过程：dX = 0.5(2 - X)dt + 0.1 dW，μ=2, θ=0.5
        rng = random.Random(99)
        x = 2.0
        series = []
        for _ in range(200):
            x = x + 0.5 * (2.0 - x) + 0.1 * rng.gauss(0, 1)
            series.append(x)
        mu, theta, sigma = ou_fit(series)
        # 噪声大 + 估计有偏，但 θ > 0 + μ 接近 2
        assert theta > 0
        assert abs(mu - 2.0) < 0.5

    def test_random_walk_theta_zero(self) -> None:
        # 随机游走的 b ≈ 1 → θ ≈ 0
        rng = random.Random(7)
        rw = [0.0]
        for _ in range(200):
            rw.append(rw[-1] + rng.gauss(0, 1))
        _, theta, _ = ou_fit(rw)
        assert abs(theta) < 0.1  # 接近 0

    def test_half_life(self) -> None:
        # θ=0.5 → half_life = ln(2)/0.5 ≈ 1.386
        rng = random.Random(99)
        x = 2.0
        series = []
        for _ in range(200):
            x = x + 0.5 * (2.0 - x) + 0.1 * rng.gauss(0, 1)
            series.append(x)
        hl = ou_half_life(series)
        assert 0.5 < hl < 5.0  # 噪声大，但应在合理量级

    def test_empty_series(self) -> None:
        mu, theta, sigma = ou_fit([])
        assert (mu, theta, sigma) == (0.0, 0.0, 0.0)
