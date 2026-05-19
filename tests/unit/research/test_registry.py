"""Tests for the factor registry."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.registry import (
    FactorSpec,
    clear_registry,
    get_factor,
    list_factors,
    register_factor,
)
from okx_trade.research.panel import FactorPanel


@pytest.fixture(autouse=True)
def _isolate_registry():
    clear_registry()
    yield
    clear_registry()


def _toy_panel() -> FactorPanel:
    return FactorPanel(
        inst_ids=("A",), timestamps_ms=(1, 2),
        close=np.array([[1.0], [2.0]]),
        volume_usdt=np.array([[1.0], [1.0]]),
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_register_factor_stores_spec_and_callable() -> None:
    @register_factor(
        id="toy",
        category="test",
        description="toy",
        direction="long_high",
        required_data=("close",),
        min_history_bars=1,
        rebalance_minutes=60,
    )
    def f(panel: FactorPanel) -> np.ndarray:
        return panel.close.copy()

    spec = get_factor("toy")
    assert isinstance(spec, FactorSpec)
    assert spec.id == "toy"
    assert spec.direction == "long_high"
    assert spec.required_data == ("close",)
    assert spec.func(_toy_panel()).shape == (2, 1)


def test_duplicate_registration_raises() -> None:
    @register_factor(id="dup", category="t", description="", direction="long_high",
                     required_data=("close",), min_history_bars=1, rebalance_minutes=60)
    def f(p): return p.close

    with pytest.raises(ValueError, match="already registered"):
        @register_factor(id="dup", category="t", description="", direction="long_high",
                         required_data=("close",), min_history_bars=1, rebalance_minutes=60)
        def g(p): return p.close


def test_invalid_direction_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        @register_factor(id="bad", category="t", description="", direction="up",  # type: ignore[arg-type]
                         required_data=("close",), min_history_bars=1, rebalance_minutes=60)
        def f(p): return p.close


def test_get_factor_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="no_such_factor"):
        get_factor("no_such_factor")


def test_list_factors_returns_sorted_by_id() -> None:
    for fid in ("zeta", "alpha", "mu"):
        @register_factor(id=fid, category="t", description="", direction="long_high",
                         required_data=("close",), min_history_bars=1, rebalance_minutes=60)
        def f(p): return p.close
    ids = [s.id for s in list_factors()]
    assert ids == ["alpha", "mu", "zeta"]


def test_all_builtin_factors_register_on_package_import(
    _isolate_registry: None,  # Use autouse fixture to clear first
) -> None:
    import sys
    # Test that importing okx_trade.research triggers all 15 factor registrations.
    # The autouse fixture has already cleared the registry.
    # Delete factor modules to force re-import (but preserve registry module itself).
    for mod in list(sys.modules.keys()):
        if ("okx_trade.research" in mod and
            mod != "okx_trade.research.registry"):
            del sys.modules[mod]
    # Fresh import will trigger all factor registrations via __init__.py chain
    import okx_trade.research  # noqa: F401
    ids = {s.id for s in list_factors()}
    expected = {
        "momentum_1d", "momentum_3d", "momentum_7d", "momentum_risk_adj_7d",
        "funding_current", "funding_z_30d", "oi_change_1d", "oi_to_volume_ratio",
        "basis_apr", "basis_z_30d",
        "rv_pct_365d", "rv_skew_up_down", "vol_of_vol_30d",
        "spread_avg_1d", "taker_buy_ratio_1d",
    }
    assert ids == expected
