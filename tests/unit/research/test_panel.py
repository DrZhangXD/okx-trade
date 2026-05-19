"""Tests for FactorPanel: shape invariants + builder."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.panel import FactorPanel, panel_from_dicts


def test_factor_panel_shapes_match() -> None:
    p = FactorPanel(
        inst_ids=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        timestamps_ms=(1_700_000_000_000, 1_700_003_600_000),
        close=np.array([[10.0, 1.0], [11.0, 1.1]]),
        volume_usdt=np.array([[1e6, 1e5], [1.1e6, 1.05e5]]),
        funding_rate=None,
        open_interest=None,
        basis_apr=None,
    )
    assert p.t == 2
    assert p.n == 2
    assert p.close.shape == (2, 2)


def test_factor_panel_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match="shape"):
        FactorPanel(
            inst_ids=("A", "B"),
            timestamps_ms=(1, 2, 3),  # T=3 but close says T=2
            close=np.array([[1.0, 2.0], [3.0, 4.0]]),
            volume_usdt=np.array([[1.0, 1.0], [1.0, 1.0]]),
            funding_rate=None, open_interest=None, basis_apr=None,
        )


def test_panel_from_dicts_aligns_timestamps_outer_join() -> None:
    # BTC has 3 bars (1000, 2000, 3000), ETH only has 2 (2000, 3000)
    by_inst = {
        "BTC": {
            "close":  [(1000, 10.0), (2000, 11.0), (3000, 12.0)],
            "volume_usdt": [(1000, 100.0), (2000, 110.0), (3000, 120.0)],
        },
        "ETH": {
            "close":  [(2000, 1.0), (3000, 1.1)],
            "volume_usdt": [(2000, 10.0), (3000, 11.0)],
        },
    }
    p = panel_from_dicts(by_inst)
    assert p.inst_ids == ("BTC", "ETH")
    assert p.timestamps_ms == (1000, 2000, 3000)
    # ETH at ts=1000 should be NaN
    assert np.isnan(p.close[0, 1])
    assert p.close[1, 1] == 1.0
    assert p.close[2, 0] == 12.0
