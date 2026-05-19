"""Tests for flow factors (spread_avg + taker_buy_ratio).

Note: these factors rely on data not in the base FactorPanel — they require
optional ``spread_bps`` and ``taker_buy_ratio`` panel fields. Tests cover
the proxy implementations that use ``close + volume_usdt`` heuristics.
"""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry


@pytest.fixture(autouse=True)
def _isolate():
    import sys
    clear_registry()
    # Remove the module from cache if it exists, then reimport
    if "okx_trade.research.factors.flow" in sys.modules:
        del sys.modules["okx_trade.research.factors.flow"]
    import okx_trade.research.factors.flow  # noqa: F401
    yield
    clear_registry()


def _panel(T: int = 50) -> FactorPanel:
    return FactorPanel(
        inst_ids=("A", "B"), timestamps_ms=tuple(range(T)),
        close=np.ones((T, 2)) * 100.0,
        volume_usdt=np.ones((T, 2)) * 1e6,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_spread_avg_1d_proxy_returns_nan_when_no_intraday_range() -> None:
    # Constant close → no high/low range → proxy returns NaN
    out = compute_factor("spread_avg_1d", _panel(50))
    assert np.all(np.isnan(out[-1]))


def test_taker_buy_ratio_1d_proxy_neutral_when_no_signed_data() -> None:
    # Without signed volume in panel, proxy returns NaN (no fabrication)
    out = compute_factor("taker_buy_ratio_1d", _panel(50))
    assert np.all(np.isnan(out[-1]))
