"""Tests for research/runtime.py — async wrappers around fetch + grade."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from okx_trade.models.common import FundingRate
from okx_trade.models.market import Candle, OpenInterestPoint, Ticker
from okx_trade.research.runtime import (
    cmd_eval,
    cmd_fetch,
    cmd_grade_all,
    parse_horizon_to_bars,
    parse_ymd_to_ms,
    resolve_universe,
)
from okx_trade.research.store import FactorStore


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Reload all factor modules so the registry is populated for each test."""
    from okx_trade.research.registry import clear_registry

    clear_registry()
    for sub in ("momentum", "funding_oi", "basis", "volatility", "flow"):
        mod = importlib.import_module(f"okx_trade.research.factors.{sub}")
        importlib.reload(mod)
    yield


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubMarket:
    candles_by_inst: dict[str, list[Candle]]
    tickers: list[Ticker]

    async def get_candles_extended(self, inst_id, bar, *, total):
        return self.candles_by_inst.get(inst_id, [])

    async def get_tickers(self, inst_type):
        return self.tickers


@dataclass
class _StubPublic:
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


def _candle(ts: int, close: float, vol_usdt: float = 1e6) -> Candle:
    return Candle(
        ts=ts, open=Decimal("100"), high=Decimal("100"),
        low=Decimal("100"), close=Decimal(str(close)),
        volume=Decimal("1"), volume_ccy=Decimal("1"),
        volume_ccy_quote=Decimal(str(vol_usdt)), confirm=True,
    )


def _ticker(inst_id: str, vol_24h: float) -> Ticker:
    return Ticker(
        instId=inst_id, instType="SWAP",
        last="100", lastSz="0", askPx="100.1", askSz="1",
        bidPx="99.9", bidSz="1", open24h="99", high24h="101",
        low24h="98", volCcy24h=str(vol_24h), vol24h=str(vol_24h),
        sodUtc0="99", sodUtc8="99", ts="1700000000000",
    )


# ---------------------------------------------------------------------------
# parse_ymd_to_ms / parse_horizon_to_bars
# ---------------------------------------------------------------------------


def test_parse_ymd_to_ms_returns_utc_midnight() -> None:
    from datetime import datetime, timezone

    ms = parse_ymd_to_ms("2026-05-19")
    # Round-trip back to date string in UTC
    assert (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        == "2026-05-19"
    )
    # Midnight: ms must be exact multiple of 86_400_000
    assert ms % 86_400_000 == 0


def test_parse_horizon_to_bars_handles_d_h_m() -> None:
    assert parse_horizon_to_bars("1d", bar_minutes=60) == 24
    assert parse_horizon_to_bars("4h", bar_minutes=60) == 4
    assert parse_horizon_to_bars("1h", bar_minutes=60) == 1
    assert parse_horizon_to_bars("30m", bar_minutes=15) == 2
    # 1d at 5m bars = 1440/5 = 288 bars
    assert parse_horizon_to_bars("1d", bar_minutes=5) == 288


def test_parse_horizon_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parse_horizon_to_bars("forever")


# ---------------------------------------------------------------------------
# resolve_universe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_universe_topN_filters_and_sorts_by_volume() -> None:
    rest = _StubRest(
        market=_StubMarket(
            candles_by_inst={},
            tickers=[
                _ticker("BTC-USDT-SWAP", vol_24h=1e9),
                _ticker("ETH-USDT-SWAP", vol_24h=5e8),
                _ticker("SOL-USDT-SWAP", vol_24h=2e8),
                _ticker("DOGE-USDT-SWAP", vol_24h=1e8),
                _ticker("XYZ-USDC-SWAP", vol_24h=1e10),  # USDC, filtered out
            ],
        ),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    ids = await resolve_universe(rest, "top3", settle_ccy="USDT")
    assert ids == ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]


@pytest.mark.asyncio
async def test_resolve_universe_comma_list_passthrough() -> None:
    rest = _StubRest(
        market=_StubMarket(candles_by_inst={}, tickers=[]),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    ids = await resolve_universe(rest, "BTC-USDT-SWAP, ETH-USDT-SWAP")
    assert ids == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


@pytest.mark.asyncio
async def test_resolve_universe_single_inst() -> None:
    rest = _StubRest(
        market=_StubMarket(candles_by_inst={}, tickers=[]),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    ids = await resolve_universe(rest, "BTC-USDT-SWAP")
    assert ids == ["BTC-USDT-SWAP"]


@pytest.mark.asyncio
async def test_resolve_universe_rejects_unknown_spec() -> None:
    rest = _StubRest(
        market=_StubMarket(candles_by_inst={}, tickers=[]),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    with pytest.raises(ValueError, match="unknown universe"):
        await resolve_universe(rest, "garbage_no_dashes")


# ---------------------------------------------------------------------------
# cmd_fetch / cmd_eval / cmd_grade_all integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_fetch_returns_populated_panel(tmp_path: Path) -> None:
    rest = _StubRest(
        market=_StubMarket(
            candles_by_inst={
                "BTC-USDT-SWAP": [_candle(1700000000000, 100.0), _candle(1700003600000, 101.0)],
                "BTC-USDT":      [_candle(1700000000000, 99.5), _candle(1700003600000, 100.0)],
            },
            tickers=[],
        ),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    panel = await cmd_fetch(
        rest_client=rest,
        start="2023-11-14", end="2023-11-15",
        universe="BTC-USDT-SWAP",
        bar="1H",
        cache_dir=tmp_path,
    )
    assert panel.n == 1
    assert panel.t >= 1
    # basis_apr was fetched (BTC-USDT-SWAP has matching BTC-USDT spot)
    assert panel.basis_apr is not None


@pytest.mark.asyncio
async def test_cmd_eval_saves_grade_to_store(tmp_path: Path) -> None:
    # 100 hourly bars, deterministic linear price → momentum_1d should yield IC ≈ 1
    closes = [(1700000000000 + i * 3_600_000, 100.0 + i * 0.5) for i in range(100)]
    rest = _StubRest(
        market=_StubMarket(
            candles_by_inst={
                "BTC-USDT-SWAP": [_candle(ts, px) for ts, px in closes],
                "ETH-USDT-SWAP": [_candle(ts, 50 + i * 0.1) for i, (ts, _) in enumerate(closes)],
                "BTC-USDT":      [_candle(ts, px - 0.5) for ts, px in closes],
                "ETH-USDT":      [_candle(ts, 50 + i * 0.1 - 0.05) for i, (ts, _) in enumerate(closes)],
            },
            tickers=[],
        ),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    store = FactorStore(tmp_path / "zoo.db")
    store.init_schema()
    store.upsert_factor(
        id="momentum_1d", category="momentum",
        direction="long_high", description="",
    )

    grade, report_path = await cmd_eval(
        rest_client=rest,
        factor_id="momentum_1d",
        start="2023-11-14", end="2023-11-19",
        universe="BTC-USDT-SWAP,ETH-USDT-SWAP",
        bar="1H", horizon="4h", top_k=1,
        panel_cache=tmp_path / "cache",
        report_dir=tmp_path / "reports",
        store=store,
    )
    assert grade.factor_id == "momentum_1d"
    assert report_path.exists()
    history = store.grade_history("momentum_1d")
    assert len(history) == 1


@pytest.mark.asyncio
async def test_cmd_grade_all_evaluates_every_registered_factor(tmp_path: Path) -> None:
    rest = _StubRest(
        market=_StubMarket(
            candles_by_inst={
                "BTC-USDT-SWAP": [_candle(1700000000000 + i * 3_600_000, 100 + i) for i in range(50)],
                "BTC-USDT":      [_candle(1700000000000 + i * 3_600_000, 99.5 + i) for i in range(50)],
            },
            tickers=[],
        ),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    store = FactorStore(tmp_path / "zoo.db")
    store.init_schema()
    # Upsert all 15 factors
    from okx_trade.research.registry import list_factors
    for spec in list_factors():
        store.upsert_factor(
            id=spec.id, category=spec.category,
            direction=spec.direction, description=spec.description,
        )

    results = await cmd_grade_all(
        rest_client=rest,
        start="2023-11-14", end="2023-11-16",
        universe="BTC-USDT-SWAP", bar="1H",
        horizon="4h", top_k=1,
        panel_cache=tmp_path / "cache",
        report_dir=tmp_path / "reports",
        store=store,
    )
    # Should have evaluated some factors. Factors requiring funding_rate/open_interest
    # will be skipped because the stub doesn't return them, leaving panel attrs as None.
    assert len(results) >= 5  # at minimum: momentum × 4 + basis (basis_apr fetched)
    # All evaluated factors got a grade row in sqlite
    for grade, _ in results:
        assert store.grade_history(grade.factor_id) != []


# ---------------------------------------------------------------------------
# cmd_backtest_portfolio (heavy — uses pytest.importorskip for NT)
# ---------------------------------------------------------------------------


nt = pytest.importorskip("nautilus_trader")


@pytest.mark.asyncio
async def test_cmd_backtest_portfolio_empty_factor_weights_rejected(tmp_path: Path) -> None:
    """yaml without factor_weights → cmd_backtest_portfolio path should fail in strategy
    config validation OR run with no rebalance. We assert the helper at least doesn't
    crash before NT instantiation when given a minimal valid yaml + stubbed rest."""
    from okx_trade.research.runtime import cmd_backtest_portfolio

    yaml_cfg = {"instrument_ids": []}  # empty universe
    rest = _StubRest(
        market=_StubMarket(candles_by_inst={}, tickers=[]),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    with pytest.raises(RuntimeError, match="empty instrument_ids"):
        await cmd_backtest_portfolio(
            rest_client=rest, yaml_cfg=yaml_cfg, bar="1H",
            total_bars=10, catalog_path=tmp_path / "catalog",
        )
