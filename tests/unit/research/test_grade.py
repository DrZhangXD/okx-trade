"""Tests for grade_factor: synthetic panel where ground truth is known."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.grade import GradeThresholds, grade_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    yield
    clear_registry()


def _make_panel(T: int, N: int, *, perfect_predictor: bool, seed: int = 0):
    """Build a panel where future close[t+1] = close[t] * (1 + signal[t]).

    If perfect_predictor=True, panel.volume_usdt[t] == signal[t]; we register a factor
    that returns volume_usdt to get IC ≈ 1.0. Otherwise random.
    """
    rng = np.random.default_rng(seed)
    signal = rng.normal(0, 0.01, size=(T, N))
    close = np.ones((T, N)) * 100.0
    for t in range(1, T):
        close[t] = close[t - 1] * (1.0 + signal[t - 1])
    if perfect_predictor:
        vol = signal.copy()
    else:
        vol = rng.normal(0, 0.01, size=(T, N))
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)), timestamps_ms=tuple(range(T)),
        close=close, volume_usdt=vol,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_perfect_predictor_yields_ic_near_one() -> None:
    @register_factor(id="oracle", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _make_panel(T=200, N=10, perfect_predictor=True)
    g = grade_factor("oracle", panel, horizon_bars=1)
    assert g.ic_mean > 0.9
    assert g.verdict == "pass"


def test_random_factor_yields_ic_near_zero_and_fails() -> None:
    @register_factor(id="noise", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _make_panel(T=200, N=10, perfect_predictor=False, seed=42)
    g = grade_factor("noise", panel, horizon_bars=1)
    assert abs(g.ic_mean) < 0.15
    assert g.verdict == "fail"


def test_long_low_direction_flips_score() -> None:
    """A long_low factor whose values negatively correlate with fwd-ret should pass."""
    @register_factor(id="inverted_oracle", category="t", description="",
                     direction="long_low", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return -p.volume_usdt.copy()  # negate the perfect signal

    panel = _make_panel(T=200, N=10, perfect_predictor=True)
    g = grade_factor("inverted_oracle", panel, horizon_bars=1)
    # After direction flip, IC mean should still be near +1
    assert g.ic_mean > 0.9


def test_custom_thresholds_override_default() -> None:
    @register_factor(id="weak", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p):
        # Mild predictor: 30% true signal + 70% noise
        rng = np.random.default_rng(1)
        return 0.3 * p.volume_usdt + 0.7 * rng.normal(0, 0.01, p.volume_usdt.shape)

    panel = _make_panel(T=500, N=20, perfect_predictor=True, seed=1)
    g_strict = grade_factor("weak", panel, horizon_bars=1,
                            thresholds=GradeThresholds(ic_t_stat=10.0, ir=2.0,
                                                       ic_positive_rate=0.95,
                                                       net_after_fees=0.01,
                                                       autocorr_1=0.9))
    assert g_strict.verdict == "fail"

    g_loose = grade_factor("weak", panel, horizon_bars=1,
                           thresholds=GradeThresholds(ic_t_stat=0.0, ir=-1.0,
                                                      ic_positive_rate=0.0,
                                                      net_after_fees=-1.0,
                                                      autocorr_1=-1.0))
    assert g_loose.verdict == "pass"


def test_decay_returns_six_horizons() -> None:
    @register_factor(id="oracle2", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _make_panel(T=300, N=10, perfect_predictor=True)
    g = grade_factor("oracle2", panel, horizon_bars=1)
    assert len(g.ic_decay) == 6
