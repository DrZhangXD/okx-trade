"""Unit tests for FundingXS three-layer defense helpers (2026-05-26)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from okx_trade.strategies._isolated_helpers import (
    compute_edge_score,
    compute_leverage,
    outlier_check,
)


# ---------------------------------------------------------------------------
# compute_leverage
# ---------------------------------------------------------------------------
class TestComputeLeverage:
    def test_zero_edge_returns_base(self) -> None:
        assert compute_leverage(0.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 2.0

    def test_one_sigma_edge_returns_5x(self) -> None:
        # base=2 + slope=3 * |1| = 5
        assert compute_leverage(1.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 5.0

    def test_two_sigma_edge_returns_8x(self) -> None:
        assert compute_leverage(2.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 8.0

    def test_three_sigma_edge_clipped_to_hi(self) -> None:
        # base=2 + slope=3 * 3 = 11 → clipped to 10
        assert compute_leverage(3.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 10.0

    def test_negative_edge_uses_abs(self) -> None:
        assert compute_leverage(-1.0, base=2.0, slope=3.0, lo=2.0, hi=10.0) == 5.0

    def test_lo_clip(self) -> None:
        # if base were below lo somehow (config error), still clip to lo
        assert compute_leverage(0.0, base=1.0, slope=3.0, lo=2.0, hi=10.0) == 2.0
