"""Tests for funding rate historical data infrastructure."""
from __future__ import annotations

import pytest

from okx_trade.backtest.funding_data import FundingPanel


def test_funding_panel_lookup_returns_most_recent_rate_at_or_before_ts():
    panel = FundingPanel(
        inst_id="BTC-USDT-SWAP",
        ts_ms=[1_000, 2_000, 3_000],
        rates=[0.0001, 0.0002, 0.00015],
    )
    assert panel.rate_at_or_before(500) is None  # before earliest
    assert panel.rate_at_or_before(1_000) == 0.0001
    assert panel.rate_at_or_before(1_500) == 0.0001  # latest <= ts
    assert panel.rate_at_or_before(2_500) == 0.0002
    assert panel.rate_at_or_before(10_000) == 0.00015  # after latest -> last
