"""Tests for compute_factor: applies a registered factor to a panel, validates required_data."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    yield
    clear_registry()


def _panel_with(funding=False) -> FactorPanel:
    T, N = 4, 2
    fr = np.zeros((T, N)) if funding else None
    return FactorPanel(
        inst_ids=("A", "B"), timestamps_ms=(1, 2, 3, 4),
        close=np.arange(T * N, dtype=float).reshape(T, N),
        volume_usdt=np.ones((T, N)),
        funding_rate=fr, open_interest=None, basis_apr=None,
    )


def test_compute_factor_returns_shape_matching_panel() -> None:
    @register_factor(id="identity", category="t", description="",
                     direction="long_high", required_data=("close",),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.close.copy()

    out = compute_factor("identity", _panel_with())
    assert out.shape == (4, 2)
    np.testing.assert_array_equal(out, _panel_with().close)


def test_compute_factor_raises_if_required_data_missing() -> None:
    @register_factor(id="needs_funding", category="t", description="",
                     direction="long_high", required_data=("funding_rate",),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.funding_rate  # type: ignore[return-value]

    with pytest.raises(ValueError, match="funding_rate"):
        compute_factor("needs_funding", _panel_with(funding=False))


def test_compute_factor_raises_if_output_shape_wrong() -> None:
    @register_factor(id="bad_shape", category="t", description="",
                     direction="long_high", required_data=("close",),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return np.zeros((3, 3))  # wrong shape

    with pytest.raises(ValueError, match="shape"):
        compute_factor("bad_shape", _panel_with())
