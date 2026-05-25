"""Tests for funding rate historical data infrastructure."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from okx_trade.backtest.funding_data import FundingPanel
from okx_trade.backtest.funding_data import download_historical_funding_rates
from okx_trade.models.common import FundingRate


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


def test_funding_panel_rejects_length_mismatch():
    with pytest.raises(ValueError, match="length mismatch"):
        FundingPanel(inst_id="BTC-USDT-SWAP", ts_ms=[1, 2], rates=[0.0001])


def _make_fr(ts_ms: int, rate: str) -> FundingRate:
    """Construct a minimal FundingRate model for tests."""
    return FundingRate(
        instType="SWAP",
        instId="BTC-USDT-SWAP",
        fundingRate=Decimal(rate),
        fundingTime=ts_ms,
        nextFundingRate=Decimal(rate),
        nextFundingTime=ts_ms + 8 * 3600 * 1000,
    )


@pytest.mark.asyncio
async def test_download_historical_funding_rates_calls_extended_and_sorts():
    rest = AsyncMock()
    # Return out-of-order to verify sort
    rest.public.get_funding_rate_history_extended = AsyncMock(
        return_value=[_make_fr(3000, "0.0003"), _make_fr(1000, "0.0001"), _make_fr(2000, "0.0002")],
    )
    panel = await download_historical_funding_rates(rest, "BTC-USDT-SWAP", total=3)
    rest.public.get_funding_rate_history_extended.assert_awaited_once_with("BTC-USDT-SWAP", total=3)
    assert panel.inst_id == "BTC-USDT-SWAP"
    assert panel.ts_ms == [1000, 2000, 3000]
    assert panel.rates == [0.0001, 0.0002, 0.0003]


def test_funding_panel_rejects_unsorted_timestamps():
    with pytest.raises(ValueError, match="sorted ascending"):
        FundingPanel(inst_id="BTC-USDT-SWAP", ts_ms=[2, 1], rates=[0.0001, 0.0002])
