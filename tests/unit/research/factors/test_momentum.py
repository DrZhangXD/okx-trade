"""Tests for momentum factors."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, get_factor


@pytest.fixture(autouse=True)
def _isolate():
    # Ensure momentum module is loaded with clean registry
    # This fixture runs before each test
    import sys
    clear_registry()
    # Remove the module from cache if it exists, then reimport
    if "okx_trade.research.factors.momentum" in sys.modules:
        del sys.modules["okx_trade.research.factors.momentum"]
    import okx_trade.research.factors.momentum  # noqa: F401
    yield
    clear_registry()


def _panel(close: np.ndarray) -> FactorPanel:
    T, N = close.shape
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)),
        timestamps_ms=tuple(range(T)),
        close=close,
        volume_usdt=np.ones((T, N)),
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_momentum_1d_at_position_24_equals_24h_return() -> None:
    # 25 hourly bars, single inst, linear price
    closes = np.linspace(100.0, 124.0, 25).reshape(25, 1)
    out = compute_factor("momentum_1d", _panel(closes))
    # Before bar 24 → NaN (insufficient history)
    assert np.isnan(out[23, 0])
    # At bar 24: (124/100) - 1 = 0.24
    assert out[24, 0] == pytest.approx(0.24)


def test_momentum_7d_uses_168_bar_lookback() -> None:
    spec = get_factor("momentum_7d")
    assert spec.min_history_bars == 168
    closes = np.ones((200, 1)) * 100.0
    closes[168:, 0] = 110.0  # 10% jump at bar 168
    out = compute_factor("momentum_7d", _panel(closes))
    # At bar 168, price = 110, ref = closes[0] = 100 → 0.10
    assert out[168, 0] == pytest.approx(0.10)


def test_momentum_risk_adj_7d_divides_by_rv30d() -> None:
    # Constant price → rv=0 → factor should be NaN (no divide by zero)
    closes = np.ones((300, 1)) * 100.0
    out = compute_factor("momentum_risk_adj_7d", _panel(closes))
    assert np.isnan(out[-1, 0])


def test_all_momentum_factors_registered() -> None:
    for fid in ("momentum_1d", "momentum_3d", "momentum_7d", "momentum_risk_adj_7d"):
        spec = get_factor(fid)
        assert spec.category == "momentum"
        assert spec.required_data == ("close",) or spec.required_data == ("close", "volume_usdt")
