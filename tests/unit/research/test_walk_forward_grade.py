"""Tests for walk_forward_grade: rolling OOS IC for one factor."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor
from okx_trade.research.walk_forward_grade import walk_forward_grade


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    yield
    clear_registry()


def _panel(T: int, N: int = 5, *, perfect: bool, seed: int = 0):
    rng = np.random.default_rng(seed)
    signal = rng.normal(0, 0.01, (T, N))
    close = np.ones((T, N)) * 100.0
    for t in range(1, T):
        close[t] = close[t - 1] * (1.0 + signal[t - 1])
    vol = signal.copy() if perfect else rng.normal(0, 0.01, (T, N))
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)), timestamps_ms=tuple(range(T)),
        close=close, volume_usdt=vol,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_walk_forward_grade_returns_per_window_grades() -> None:
    @register_factor(id="oracle_wf", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _panel(T=600, perfect=True)
    grades = walk_forward_grade(
        "oracle_wf", panel, horizon_bars=1,
        train_window=200, test_window=100,
    )
    assert len(grades) == 4  # (600 - 200) / 100 = 4 windows
    # Perfect predictor should have high IC in every OOS test window
    assert all(g.ic_mean > 0.5 for g in grades)
