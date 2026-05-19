"""Tests for funding + open-interest factors."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, get_factor


@pytest.fixture(autouse=True)
def _isolate():
    # Ensure funding_oi module is loaded with clean registry
    # This fixture runs before each test
    import sys
    clear_registry()
    # Remove the module from cache if it exists, then reimport
    if "okx_trade.research.factors.funding_oi" in sys.modules:
        del sys.modules["okx_trade.research.factors.funding_oi"]
    import okx_trade.research.factors.funding_oi  # noqa: F401
    yield
    clear_registry()


def _panel(T: int, N: int = 2, *, with_funding=True, with_oi=True) -> FactorPanel:
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)),
        timestamps_ms=tuple(range(T)),
        close=np.ones((T, N)) * 100.0,
        volume_usdt=np.ones((T, N)) * 1e6,
        funding_rate=(np.ones((T, N)) * 0.0001 if with_funding else None),
        open_interest=(np.ones((T, N)) * 1000.0 if with_oi else None),
        basis_apr=None,
    )


def test_funding_current_passes_through() -> None:
    p = _panel(10)
    p.funding_rate[5, 0] = 0.0005
    out = compute_factor("funding_current", p)
    assert out[5, 0] == pytest.approx(0.0005)


def test_funding_z_30d_normalizes_by_window() -> None:
    T = 800
    p = _panel(T)
    # Make funding mostly 0.0001 but spike at t=T-1
    p.funding_rate[:] = 0.0001
    p.funding_rate[T - 1, 0] = 0.0010
    out = compute_factor("funding_z_30d", p)
    assert out[T - 1, 0] > 5.0  # huge z-score


def test_oi_change_1d_is_24h_delta_ratio() -> None:
    T = 50
    p = _panel(T)
    p.open_interest[:] = 1000.0
    p.open_interest[T - 1, 0] = 1100.0  # +10% jump at last bar
    out = compute_factor("oi_change_1d", p)
    assert out[T - 1, 0] == pytest.approx(0.10)


def test_oi_to_volume_ratio_uses_24h_avg_volume() -> None:
    T = 30
    p = _panel(T)
    p.open_interest[:] = 5e6
    p.volume_usdt[:] = 1e6
    out = compute_factor("oi_to_volume_ratio", p)
    # OI/avg_vol = 5e6 / 1e6 = 5.0
    assert out[T - 1, 0] == pytest.approx(5.0)


def test_factors_raise_when_required_field_missing() -> None:
    p_no_funding = _panel(50, with_funding=False)
    with pytest.raises(ValueError, match="funding_rate"):
        compute_factor("funding_current", p_no_funding)
