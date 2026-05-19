"""Tests for volatility factors."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry


@pytest.fixture(autouse=True)
def _isolate():
    # Ensure volatility module is loaded with clean registry
    # This fixture runs before each test
    import sys
    clear_registry()
    # Remove the module from cache if it exists, then reimport
    if "okx_trade.research.factors.volatility" in sys.modules:
        del sys.modules["okx_trade.research.factors.volatility"]
    import okx_trade.research.factors.volatility  # noqa: F401
    yield
    clear_registry()


def _vol_panel(T: int, sigma: float = 0.01, seed: int = 0) -> FactorPanel:
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(0, sigma, size=(T, 2))
    prices = 100.0 * np.exp(np.cumsum(log_rets, axis=0))
    return FactorPanel(
        inst_ids=("A", "B"), timestamps_ms=tuple(range(T)),
        close=prices, volume_usdt=np.ones((T, 2)) * 1e6,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_rv_pct_365d_yields_value_in_unit_interval() -> None:
    p = _vol_panel(365 * 24 + 200)
    out = compute_factor("rv_pct_365d", p)
    last = out[-1]
    assert np.all((last >= 0.0) & (last <= 1.0))


def test_rv_skew_up_down_constant_price_is_nan() -> None:
    T = 800
    p = FactorPanel(
        inst_ids=("A",), timestamps_ms=tuple(range(T)),
        close=np.ones((T, 1)) * 100.0, volume_usdt=np.ones((T, 1)),
        funding_rate=None, open_interest=None, basis_apr=None,
    )
    out = compute_factor("rv_skew_up_down", p)
    assert np.isnan(out[-1, 0])


def test_vol_of_vol_30d_is_nonneg() -> None:
    p = _vol_panel(1000)
    out = compute_factor("vol_of_vol_30d", p)
    last = out[-1]
    assert np.all(np.isnan(last) | (last >= 0.0))
