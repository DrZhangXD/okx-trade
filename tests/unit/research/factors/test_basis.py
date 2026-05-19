"""Tests for basis factors."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry


@pytest.fixture(autouse=True)
def _isolate():
    # Ensure basis module is loaded with clean registry
    # This fixture runs before each test
    import sys
    clear_registry()
    # Remove the module from cache if it exists, then reimport
    if "okx_trade.research.factors.basis" in sys.modules:
        del sys.modules["okx_trade.research.factors.basis"]
    import okx_trade.research.factors.basis  # noqa: F401
    yield
    clear_registry()


def _panel(T: int, basis_value: float = 0.05) -> FactorPanel:
    return FactorPanel(
        inst_ids=("BTC", "ETH"), timestamps_ms=tuple(range(T)),
        close=np.ones((T, 2)) * 100.0,
        volume_usdt=np.ones((T, 2)) * 1e6,
        funding_rate=None, open_interest=None,
        basis_apr=np.ones((T, 2)) * basis_value,
    )


def test_basis_apr_passes_through() -> None:
    p = _panel(10, basis_value=0.08)
    p.basis_apr[5, 0] = 0.15
    out = compute_factor("basis_apr", p)
    assert out[5, 0] == pytest.approx(0.15)


def test_basis_z_30d_normalizes_basis_over_window() -> None:
    T = 800
    p = _panel(T, basis_value=0.05)
    p.basis_apr[T - 1, 0] = 0.50  # big spike
    out = compute_factor("basis_z_30d", p)
    assert out[T - 1, 0] > 5.0
