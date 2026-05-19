"""Tests for research/data.py fetch_panel."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from okx_trade.models.common import FundingRate
from okx_trade.models.market import Candle, OpenInterestPoint
from okx_trade.research.data import fetch_panel


@dataclass
class _StubMarket:
    """Stubs get_candles_extended."""
    candles_by_inst: dict[str, list[Candle]]

    async def get_candles_extended(self, inst_id, bar, *, total):
        return self.candles_by_inst.get(inst_id, [])


@dataclass
class _StubPublic:
    """Stubs funding + OI history."""
    funding_by_inst: dict[str, list[FundingRate]]
    oi_by_inst: dict[str, list[OpenInterestPoint]]

    async def get_funding_rate_history_extended(self, inst_id, *, total):
        return self.funding_by_inst.get(inst_id, [])

    async def get_open_interest_history_extended(self, inst_id, *, period, total):
        return self.oi_by_inst.get(inst_id, [])


@dataclass
class _StubRest:
    market: _StubMarket
    public: _StubPublic


def _candle(ts: int, close: float, vol_usdt: float) -> Candle:
    return Candle(
        ts=ts, open=Decimal("100"), high=Decimal("100"),
        low=Decimal("100"), close=Decimal(str(close)),
        volume=Decimal("1"), volume_ccy=Decimal("1"),
        volume_ccy_quote=Decimal(str(vol_usdt)), confirm=True,
    )


@pytest.mark.asyncio
async def test_fetch_panel_assembles_close_and_volume(tmp_path: Path) -> None:
    candles = {
        "BTC-USDT-SWAP": [_candle(1000, 100.0, 1e6), _candle(2000, 110.0, 1.1e6)],
        "ETH-USDT-SWAP": [_candle(1000, 2.0, 5e5), _candle(2000, 2.1, 5.5e5)],
    }
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    panel = await fetch_panel(
        rest_client=rest,  # type: ignore[arg-type]
        inst_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        start_ms=1000, end_ms=2000,
        bar="1H",
        include=("close", "volume_usdt"),
        cache_dir=tmp_path,
    )
    assert panel.inst_ids == ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    assert panel.timestamps_ms == (1000, 2000)
    assert panel.close[1, 0] == pytest.approx(110.0)
    assert panel.close[1, 1] == pytest.approx(2.1)


@pytest.mark.asyncio
async def test_fetch_panel_caches_to_parquet_and_reuses(tmp_path: Path) -> None:
    candles = {"BTC-USDT-SWAP": [_candle(1000, 100.0, 1e6)]}
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    p1 = await fetch_panel(rest_client=rest, inst_ids=["BTC-USDT-SWAP"],  # type: ignore[arg-type]
                            start_ms=1000, end_ms=1000, bar="1H",
                            include=("close", "volume_usdt"), cache_dir=tmp_path)
    # Cache file exists
    cache_files = list(tmp_path.glob("*.parquet"))
    assert len(cache_files) == 1
    # Second call with empty stub: should still return same data via cache
    rest_empty = _StubRest(
        market=_StubMarket(candles_by_inst={}),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    p2 = await fetch_panel(rest_client=rest_empty, inst_ids=["BTC-USDT-SWAP"],  # type: ignore[arg-type]
                            start_ms=1000, end_ms=1000, bar="1H",
                            include=("close", "volume_usdt"), cache_dir=tmp_path)
    np.testing.assert_array_equal(p1.close, p2.close)


@pytest.mark.asyncio
async def test_fetch_panel_includes_funding_when_requested(tmp_path: Path) -> None:
    candles = {"BTC-USDT-SWAP": [_candle(1000, 100.0, 1e6), _candle(2000, 100.0, 1e6)]}
    funding = {"BTC-USDT-SWAP": [
        FundingRate(instType="SWAP", instId="BTC-USDT-SWAP",
                    fundingRate="0.0001", fundingTime=1000),
        FundingRate(instType="SWAP", instId="BTC-USDT-SWAP",
                    fundingRate="0.0002", fundingTime=2000),
    ]}
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst=funding, oi_by_inst={}),
    )
    panel = await fetch_panel(rest_client=rest, inst_ids=["BTC-USDT-SWAP"],  # type: ignore[arg-type]
                              start_ms=1000, end_ms=2000, bar="1H",
                              include=("close", "volume_usdt", "funding_rate"),
                              cache_dir=tmp_path)
    assert panel.funding_rate is not None
    assert panel.funding_rate[0, 0] == pytest.approx(0.0001)
    assert panel.funding_rate[1, 0] == pytest.approx(0.0002)


@pytest.mark.asyncio
async def test_fetch_panel_includes_basis_apr_from_spot_pair(tmp_path: Path) -> None:
    """basis_apr requires fetching spot pair (BTC-USDT-SWAP → BTC-USDT) close."""
    candles = {
        "BTC-USDT-SWAP": [_candle(1000, 102.0, 1e6), _candle(2000, 103.0, 1e6)],
        # spot pair, slightly lower → perp at premium
        "BTC-USDT":      [_candle(1000, 100.0, 1e6), _candle(2000, 100.0, 1e6)],
    }
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    panel = await fetch_panel(
        rest_client=rest, inst_ids=["BTC-USDT-SWAP"],  # type: ignore[arg-type]
        start_ms=1000, end_ms=2000, bar="1H",
        include=("close", "volume_usdt", "basis_apr"),
        cache_dir=tmp_path,
    )
    assert panel.basis_apr is not None
    # basis = (perp - spot) / spot
    assert panel.basis_apr[0, 0] == pytest.approx((102 - 100) / 100)  # 0.02
    assert panel.basis_apr[1, 0] == pytest.approx((103 - 100) / 100)  # 0.03


@pytest.mark.asyncio
async def test_fetch_panel_basis_apr_skipped_for_non_swap_inst(tmp_path: Path) -> None:
    """A spot or futures inst (no '-SWAP' suffix) has no spot pair → basis_apr=None."""
    candles = {"BTC-USDT": [_candle(1000, 100.0, 1e6)]}
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    panel = await fetch_panel(
        rest_client=rest, inst_ids=["BTC-USDT"],  # type: ignore[arg-type]
        start_ms=1000, end_ms=1000, bar="1H",
        include=("close", "volume_usdt", "basis_apr"),
        cache_dir=tmp_path,
    )
    # No spot derivation possible → basis_apr never set → outer-join → None
    assert panel.basis_apr is None


@pytest.mark.asyncio
async def test_fetch_panel_basis_apr_skipped_when_spot_pair_404(tmp_path: Path) -> None:
    """Some perps have no spot counterpart (e.g. obscure index perps).
    Spot fetch may raise — fetch_panel should swallow and continue without basis."""

    class _FailingMarket(_StubMarket):
        async def get_candles_extended(self, inst_id, bar, *, total):
            if inst_id == "FOOBAR-USDT":
                raise RuntimeError("instrument not found")
            return self.candles_by_inst.get(inst_id, [])

    rest = _StubRest(
        market=_FailingMarket(candles_by_inst={
            "FOOBAR-USDT-SWAP": [_candle(1000, 100.0, 1e6)],
        }),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    panel = await fetch_panel(
        rest_client=rest, inst_ids=["FOOBAR-USDT-SWAP"],  # type: ignore[arg-type]
        start_ms=1000, end_ms=1000, bar="1H",
        include=("close", "volume_usdt", "basis_apr"),
        cache_dir=tmp_path,
    )
    # No basis_apr was populated because spot lookup failed
    assert panel.basis_apr is None
    # But close still arrived
    assert panel.close[0, 0] == pytest.approx(100.0)
