# Plan 1: Funding Rate Historical Data Infrastructure

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `funding_carry`, `funding_cross_section`, `funding_skew_momentum`, and `basis_arb` (funding leg) fully backtestable via `scripts/backtest.py`, by adding historical funding-rate downloading, parquet caching, and a `feed_funding_panel()` injection hook on each funding-aware strategy.

**Architecture:** OKX REST already exposes `get_funding_rate_history_extended()` (1095 samples = 1 year × 3/day). We add a parallel data-loader path that pulls per-instrument funding history and writes parquet under `${catalog}/funding/<inst_id>/`. Each funding-aware strategy gets a new pure-data hook `feed_funding_panel()` that pre-loads ts→rate pairs into the same internal buffer the live REST polling currently fills. Backtest path: download → load panel → inject into strategy config → strategy looks up by `bar.ts_event` on each bar instead of hitting REST.

**Tech Stack:** Python 3.12+ asyncio, pyarrow/parquet, OKXRestClient (existing), Nautilus Trader (strategy only).

**Roadmap reference:** [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md) — Plan 1.

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `src/okx_trade/backtest/funding_data.py` | New module: `download_historical_funding_rates`, `write_funding_parquet`, `load_funding_panel`, `FundingPanel` dataclass | create |
| `src/okx_trade/backtest/data_loader.py` | Add `prepare_funding_panel()` one-stop helper alongside existing `prepare_backtest_catalog()` | modify |
| `src/okx_trade/strategies/funding_carry.py` | Add `feed_funding_panel()` method + `funding_panel` config field; route `_check_funding_async` through `_funding_source` abstraction | modify |
| `src/okx_trade/strategies/funding_cross_section.py` | Same `feed_funding_panel()` pattern (multi-inst) | modify |
| `src/okx_trade/strategies/funding_skew_momentum.py` | Same `feed_funding_panel()` pattern + replace `_bootstrap_history` REST call with panel-lookup when panel present | modify |
| `src/okx_trade/strategies/basis_arb.py` | Add `feed_funding_panel()` (uses funding for entry-signal filtering) | modify |
| `scripts/backtest.py` | Register `funding_carry`, `funding_cross_section`, `funding_skew_momentum`, `basis_arb` in `SUPPORTED_STRATEGIES`; add 4 `_run_*` helpers | modify |
| `tests/unit/backtest/__init__.py` | New package marker | create |
| `tests/unit/backtest/test_funding_data.py` | Unit tests for downloader / writer / loader | create |
| `tests/unit/strategies/test_funding_panel_injection.py` | Test all 4 strategies accept and use injected panel | create |
| `tests/integration/test_backtest_funding_strategies.py` | End-to-end smoke: tiny fixture → run each strategy → assert finite stats | create |
| `docs/strategy_roadmap.md` | Mark 4 strategies as "backtestable" | modify |

---

## Conventions

- All new files: `from __future__ import annotations` at top.
- Type hints: PEP 604.
- Dataclasses: `@dataclass(frozen=True, slots=True)`.
- Parquet schema for funding: columns `[ts_ms: int64, funding_rate: float64, realized_rate: float64, next_ts_ms: int64]`. Partition: `${catalog}/funding/<inst_id>/<YYYYMM>.parquet`.
- Tests at `tests/unit/<mirror>` and `tests/integration/`.
- Commit per task: `<type>(<scope>): <subject>`.

---

## Task 1: Add `FundingPanel` dataclass + parquet schema docstring

**Files:**
- Create: `src/okx_trade/backtest/funding_data.py`
- Test:   `tests/unit/backtest/__init__.py` (empty), `tests/unit/backtest/test_funding_data.py`

- [ ] **Step 1: Create package marker**

```bash
touch tests/unit/backtest/__init__.py
```

- [ ] **Step 2: Write failing test for `FundingPanel` lookup**

Create `tests/unit/backtest/test_funding_data.py`:

```python
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
```

- [ ] **Step 3: Run to confirm failure**

Run: `pytest tests/unit/backtest/test_funding_data.py -v`
Expected: `ImportError: cannot import name 'FundingPanel'`

- [ ] **Step 4: Implement `FundingPanel`**

Create `src/okx_trade/backtest/funding_data.py`:

```python
"""Funding rate historical data: download + parquet cache + in-memory panel.

Parquet schema (per instrument):
    ts_ms        int64    funding settlement timestamp (UTC ms epoch)
    funding_rate float64  realized 8h funding rate (e.g. 0.0001 = 0.01%/8h)
    realized_rate float64 realizedRate per OKX (often == funding_rate)
    next_ts_ms   int64    nextFundingTime in ms

Partition: ${catalog}/funding/<inst_id>/<YYYYMM>.parquet
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class FundingPanel:
    """In-memory funding-rate time series for one instrument.

    Used as backtest replacement for live REST polling. Strategies look up the
    most recent settled rate at a given bar timestamp via ``rate_at_or_before``.
    """

    inst_id: str
    ts_ms: list[int]
    rates: list[float]

    def __post_init__(self) -> None:
        if len(self.ts_ms) != len(self.rates):
            raise ValueError("ts_ms and rates length mismatch")
        # sorted-ascending assumed; cheap guard
        for i in range(1, len(self.ts_ms)):
            if self.ts_ms[i] < self.ts_ms[i - 1]:
                raise ValueError("ts_ms must be sorted ascending")

    def rate_at_or_before(self, ts_ms: int) -> float | None:
        """Return funding rate settled at or before ``ts_ms``.

        Returns ``None`` if ``ts_ms`` is before the earliest sample.
        """
        if not self.ts_ms or ts_ms < self.ts_ms[0]:
            return None
        idx = bisect_right(self.ts_ms, ts_ms) - 1
        return self.rates[idx]
```

- [ ] **Step 5: Run to confirm pass**

Run: `pytest tests/unit/backtest/test_funding_data.py -v`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/backtest/funding_data.py tests/unit/backtest/__init__.py tests/unit/backtest/test_funding_data.py
git commit -m "feat(backtest): add FundingPanel for in-memory funding rate lookup"
```

---

## Task 2: Add `download_historical_funding_rates` (REST → list)

**Files:**
- Modify: `src/okx_trade/backtest/funding_data.py`
- Test:   `tests/unit/backtest/test_funding_data.py`

- [ ] **Step 1: Write failing test using a fake REST client**

Append to `tests/unit/backtest/test_funding_data.py`:

```python
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from okx_trade.backtest.funding_data import download_historical_funding_rates
from okx_trade.models.market import FundingRate


def _make_fr(ts_ms: int, rate: str) -> FundingRate:
    """Construct a minimal FundingRate model for tests."""
    return FundingRate(
        inst_type="SWAP",
        inst_id="BTC-USDT-SWAP",
        funding_rate=Decimal(rate),
        funding_time=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
        next_funding_rate=Decimal(rate),
        next_funding_time=datetime.fromtimestamp((ts_ms + 8 * 3600 * 1000) / 1000, tz=timezone.utc),
        realized_rate=Decimal(rate),
        method="current_period",
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
```

- [ ] **Step 2: Run to confirm failure**

Run: `pytest tests/unit/backtest/test_funding_data.py::test_download_historical_funding_rates_calls_extended_and_sorts -v`
Expected: `ImportError: cannot import name 'download_historical_funding_rates'`

- [ ] **Step 3: Implement downloader**

Append to `src/okx_trade/backtest/funding_data.py`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..rest.client import OKXRestClient


async def download_historical_funding_rates(
    client: "OKXRestClient",
    inst_id: str,
    *,
    total: int = 1095,
) -> FundingPanel:
    """Download up to ``total`` historical funding rates for ``inst_id``.

    Uses the existing ``_extended`` paginated wrapper (handles 100/page +
    deduplication). Returns a sorted ``FundingPanel``.
    """
    rates = await client.public.get_funding_rate_history_extended(inst_id, total=total)
    sorted_rates = sorted(rates, key=lambda r: int(r.funding_time.timestamp() * 1000))
    return FundingPanel(
        inst_id=inst_id,
        ts_ms=[int(r.funding_time.timestamp() * 1000) for r in sorted_rates],
        rates=[float(r.funding_rate) for r in sorted_rates],
    )
```

- [ ] **Step 4: Run test**

Run: `pytest tests/unit/backtest/test_funding_data.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/backtest/funding_data.py tests/unit/backtest/test_funding_data.py
git commit -m "feat(backtest): add download_historical_funding_rates wrapper"
```

---

## Task 3: Add parquet write/read helpers

**Files:**
- Modify: `src/okx_trade/backtest/funding_data.py`
- Test:   `tests/unit/backtest/test_funding_data.py`

- [ ] **Step 1: Write failing test for roundtrip parquet**

Append:

```python
def test_funding_panel_parquet_roundtrip(tmp_path):
    from okx_trade.backtest.funding_data import write_funding_parquet, read_funding_parquet

    panel = FundingPanel(
        inst_id="BTC-USDT-SWAP",
        ts_ms=[1_700_000_000_000, 1_700_028_800_000, 1_700_057_600_000],
        rates=[0.0001, 0.0002, 0.00015],
    )
    written_paths = write_funding_parquet(panel, catalog_path=tmp_path)
    assert len(written_paths) >= 1
    assert all(p.suffix == ".parquet" for p in written_paths)
    assert all(p.exists() for p in written_paths)

    loaded = read_funding_parquet(panel.inst_id, catalog_path=tmp_path)
    assert loaded.ts_ms == panel.ts_ms
    assert loaded.rates == panel.rates
```

- [ ] **Step 2: Run, confirm fails with ImportError**

Run: `pytest tests/unit/backtest/test_funding_data.py::test_funding_panel_parquet_roundtrip -v`

- [ ] **Step 3: Implement writer + reader**

Append to `src/okx_trade/backtest/funding_data.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


_FUNDING_SCHEMA = pa.schema([
    pa.field("ts_ms", pa.int64()),
    pa.field("funding_rate", pa.float64()),
])


def _partition_key(ts_ms: int) -> str:
    """Bucket a timestamp into YYYYMM partition key (UTC)."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y%m")


def write_funding_parquet(
    panel: FundingPanel,
    *,
    catalog_path: Path,
) -> list[Path]:
    """Write panel to ``${catalog_path}/funding/<inst_id>/<YYYYMM>.parquet``.

    Splits by month so partial refresh is cheap. Overwrites existing month files.
    Returns the list of files written.
    """
    base = catalog_path / "funding" / panel.inst_id
    base.mkdir(parents=True, exist_ok=True)

    # Bucket rows by month
    buckets: dict[str, tuple[list[int], list[float]]] = {}
    for ts, rate in zip(panel.ts_ms, panel.rates, strict=True):
        key = _partition_key(ts)
        if key not in buckets:
            buckets[key] = ([], [])
        buckets[key][0].append(ts)
        buckets[key][1].append(rate)

    written: list[Path] = []
    for key, (ts_list, rate_list) in buckets.items():
        table = pa.Table.from_arrays(
            [pa.array(ts_list, type=pa.int64()), pa.array(rate_list, type=pa.float64())],
            schema=_FUNDING_SCHEMA,
        )
        path = base / f"{key}.parquet"
        pq.write_table(table, path, compression="snappy")
        written.append(path)
    return written


def read_funding_parquet(
    inst_id: str,
    *,
    catalog_path: Path,
) -> FundingPanel:
    """Read all monthly parquet files for ``inst_id`` and return a merged panel."""
    base = catalog_path / "funding" / inst_id
    if not base.exists():
        raise FileNotFoundError(f"no funding cache at {base}")
    files = sorted(base.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet files in {base}")
    tables = [pq.read_table(f) for f in files]
    merged = pa.concat_tables(tables)
    # Sort by ts_ms (months may not be in lex order if user backfilled out-of-order)
    sort_idx = pa.compute.sort_indices(merged, sort_keys=[("ts_ms", "ascending")])
    sorted_table = merged.take(sort_idx)
    return FundingPanel(
        inst_id=inst_id,
        ts_ms=sorted_table["ts_ms"].to_pylist(),
        rates=sorted_table["funding_rate"].to_pylist(),
    )
```

- [ ] **Step 4: Run test**

Run: `pytest tests/unit/backtest/test_funding_data.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/backtest/funding_data.py tests/unit/backtest/test_funding_data.py
git commit -m "feat(backtest): add monthly-partitioned funding rate parquet cache"
```

---

## Task 4: Add `prepare_funding_panel()` one-stop helper

**Files:**
- Modify: `src/okx_trade/backtest/data_loader.py` (append new function at end)
- Test:   `tests/unit/backtest/test_funding_data.py`

- [ ] **Step 1: Write failing test for cache-or-download behavior**

Append:

```python
@pytest.mark.asyncio
async def test_prepare_funding_panel_uses_cache_when_present(tmp_path):
    from okx_trade.backtest.data_loader import prepare_funding_panel
    from okx_trade.backtest.funding_data import FundingPanel, write_funding_parquet

    cached = FundingPanel(
        inst_id="BTC-USDT-SWAP",
        ts_ms=[1_700_000_000_000],
        rates=[0.0001],
    )
    write_funding_parquet(cached, catalog_path=tmp_path)

    rest = AsyncMock()  # should NOT be called
    panel = await prepare_funding_panel(
        rest, "BTC-USDT-SWAP", total=100, catalog_path=tmp_path, reuse_cache=True,
    )
    assert panel.ts_ms == cached.ts_ms
    rest.public.get_funding_rate_history_extended.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_funding_panel_downloads_when_no_cache(tmp_path):
    from okx_trade.backtest.data_loader import prepare_funding_panel

    rest = AsyncMock()
    rest.public.get_funding_rate_history_extended = AsyncMock(
        return_value=[_make_fr(1_700_000_000_000, "0.0001")],
    )
    panel = await prepare_funding_panel(
        rest, "BTC-USDT-SWAP", total=100, catalog_path=tmp_path, reuse_cache=True,
    )
    assert panel.ts_ms == [1_700_000_000_000]
    # Should have written cache to disk
    assert (tmp_path / "funding" / "BTC-USDT-SWAP").exists()
```

- [ ] **Step 2: Run, confirm fails**

Run: `pytest tests/unit/backtest/test_funding_data.py -v`

- [ ] **Step 3: Implement helper**

Append to `src/okx_trade/backtest/data_loader.py` (after `prepare_backtest_catalog`):

```python
from .funding_data import (
    FundingPanel,
    download_historical_funding_rates,
    read_funding_parquet,
    write_funding_parquet,
)


async def prepare_funding_panel(
    client: "OKXRestClient",
    inst_id: str,
    *,
    total: int = 1095,
    catalog_path: Path,
    reuse_cache: bool = True,
) -> FundingPanel:
    """One-stop: read parquet cache, else download via REST and write cache.

    Mirrors ``prepare_backtest_catalog`` ergonomics. Always returns a sorted panel.
    """
    if reuse_cache:
        try:
            return read_funding_parquet(inst_id, catalog_path=catalog_path)
        except FileNotFoundError:
            pass
    panel = await download_historical_funding_rates(client, inst_id, total=total)
    write_funding_parquet(panel, catalog_path=catalog_path)
    return panel
```

- [ ] **Step 4: Run, confirm pass**

Run: `pytest tests/unit/backtest/test_funding_data.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/backtest/data_loader.py tests/unit/backtest/test_funding_data.py
git commit -m "feat(backtest): add prepare_funding_panel cache-or-download helper"
```

---

## Task 5: Add `feed_funding_panel()` hook to `funding_carry`

**Files:**
- Modify: `src/okx_trade/strategies/funding_carry.py`
- Test:   `tests/unit/strategies/test_funding_panel_injection.py`

- [ ] **Step 1: Create test scaffolding**

Create `tests/unit/strategies/test_funding_panel_injection.py`:

```python
"""Tests for funding-panel injection across funding-aware strategies."""
from __future__ import annotations

import pytest

from okx_trade.backtest.funding_data import FundingPanel


def test_funding_carry_strategy_accepts_panel_via_feed_method():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.funding_carry import FundingCarryStrategy, FundingCarryConfig

    cfg = FundingCarryConfig(
        spot_instrument_id="BTC-USDT.OKX",
        perp_instrument_id="BTC-USDT-SWAP.OKX",
        spot_bar_type="BTC-USDT.OKX-1-HOUR-LAST-EXTERNAL",
    )
    strat = FundingCarryStrategy(cfg)
    panel = FundingPanel(
        inst_id="BTC-USDT-SWAP",
        ts_ms=[1_700_000_000_000, 1_700_028_800_000],
        rates=[0.0001, 0.00015],
    )
    strat.feed_funding_panel(panel)
    # Internal lookup goes through panel, not REST
    assert strat._funding_source_kind == "panel"
    assert strat._funding_panel is panel
```

- [ ] **Step 2: Run, confirm fails**

Run: `pytest tests/unit/strategies/test_funding_panel_injection.py::test_funding_carry_strategy_accepts_panel_via_feed_method -v`
Expected: `AttributeError: ... has no attribute 'feed_funding_panel'`

- [ ] **Step 3: Implement hook**

Modify `src/okx_trade/strategies/funding_carry.py`:

In `FundingCarryStrategy.__init__`, after the existing `self._funding_fetcher = None` line (around line 223), add:

```python
            self._funding_panel: "FundingPanel | None" = None
            self._funding_source_kind: str = "rest"  # "rest" (live) | "panel" (backtest)
```

Add module-level import (top of file, after the existing TYPE_CHECKING block):

```python
if TYPE_CHECKING:
    from ..backtest.funding_data import FundingPanel
```

Add new method on `FundingCarryStrategy` (insert after `on_start`):

```python
        def feed_funding_panel(self, panel: "FundingPanel") -> None:
            """Inject a pre-loaded funding-rate panel for backtest.

            When called, ``_check_funding_async`` will look up rates from the panel
            (keyed by latest bar timestamp) instead of hitting REST.
            """
            self._funding_panel = panel
            self._funding_source_kind = "panel"
```

Modify `_check_funding_async` (around line 309). Replace the REST-call block:

```python
        async def _check_funding_async(self) -> None:
            """Pull funding rate (REST live, or panel for backtest) and act."""
            try:
                rate_8h = await self._fetch_current_funding()
                if rate_8h is None:
                    return  # no data yet
                action = funding_carry_decision(rate_8h, self._has_position, self._params)
                self.log.info(
                    f"funding={rate_8h:+.4%}/8h action={action.value} pos={self._has_position}"
                )
                if action == CarryAction.ENTER:
                    self._enter_carry()
                elif action == CarryAction.EXIT:
                    self._exit_carry()
            except Exception as exc:
                self.log.error(f"funding check failed: {exc}")

        async def _fetch_current_funding(self) -> float | None:
            """Return latest 8h funding rate from the active source."""
            if self._funding_source_kind == "panel" and self._funding_panel is not None:
                # Use the latest bar timestamp we have seen
                if self._last_check_ts_ns <= 0:
                    return None
                ts_ms = int(self._last_check_ts_ns // 1_000_000)
                return self._funding_panel.rate_at_or_before(ts_ms)
            # REST path (live)
            if self._rest is None:
                from ..rest.client import OKXRestClient
                self._rest = OKXRestClient(self._rest_settings)
                await self._rest.__aenter__()
            perp_inst_id_str = self.perp_id.symbol.value
            fr = await self._rest.public.get_funding_rate(perp_inst_id_str)
            return float(fr.funding_rate)
```

- [ ] **Step 4: Run test**

Run: `pytest tests/unit/strategies/test_funding_panel_injection.py::test_funding_carry_strategy_accepts_panel_via_feed_method -v`
Expected: pass.

- [ ] **Step 5: Run the existing funding_carry unit tests to ensure no regression**

Run: `pytest tests/unit/test_strategy_funding_carry.py -v`
Expected: all pre-existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/strategies/funding_carry.py tests/unit/strategies/test_funding_panel_injection.py
git commit -m "feat(funding_carry): add feed_funding_panel injection for backtest"
```

---

## Task 6: Add `feed_funding_panel()` to `funding_cross_section`

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py`
- Test:   `tests/unit/strategies/test_funding_panel_injection.py`

- [ ] **Step 1: Read the strategy to find the REST poll site**

Run: `grep -n "get_funding_rate\|_poll\|_check_funding" src/okx_trade/strategies/funding_cross_section.py`

Identify the async method that polls funding (typically `_check_funding_loop` or similar) and the dict that stores `inst_id → rate`.

- [ ] **Step 2: Write failing test**

Append to `tests/unit/strategies/test_funding_panel_injection.py`:

```python
def test_funding_cross_section_accepts_multi_inst_panels():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.funding_cross_section import (
        FundingCrossSectionStrategy, FundingCrossSectionConfig,
    )
    # Use the minimal viable config — adapt instrument_ids field name to match impl
    cfg = FundingCrossSectionConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        beta_bar_type_template="{inst}-1-DAY-LAST-EXTERNAL",
    )
    strat = FundingCrossSectionStrategy(cfg)
    panels = {
        "BTC-USDT-SWAP": FundingPanel(
            inst_id="BTC-USDT-SWAP",
            ts_ms=[1_700_000_000_000], rates=[0.0001],
        ),
        "ETH-USDT-SWAP": FundingPanel(
            inst_id="ETH-USDT-SWAP",
            ts_ms=[1_700_000_000_000], rates=[0.0002],
        ),
    }
    strat.feed_funding_panel(panels)
    assert strat._funding_source_kind == "panel"
    assert set(strat._funding_panels) == set(panels)
```

- [ ] **Step 3: Run, confirm fails**

Run: `pytest tests/unit/strategies/test_funding_panel_injection.py::test_funding_cross_section_accepts_multi_inst_panels -v`

- [ ] **Step 4: Implement multi-inst hook**

In `src/okx_trade/strategies/funding_cross_section.py`:

Add to `__init__`:

```python
            self._funding_panels: dict[str, "FundingPanel"] = {}
            self._funding_source_kind: str = "rest"
```

Add method:

```python
        def feed_funding_panel(self, panels: dict[str, "FundingPanel"]) -> None:
            """Inject pre-loaded funding panels keyed by inst_id (no venue suffix)."""
            self._funding_panels = dict(panels)
            self._funding_source_kind = "panel"
```

Modify the strategy's funding-poll site: when `_funding_source_kind == "panel"`, look up `self._funding_panels[inst_id].rate_at_or_before(ts_ms)` instead of `get_funding_rate`.

(Apply the same `_fetch_current_funding(inst_id)` abstraction pattern as Task 5.)

- [ ] **Step 5: Run test**

Run: `pytest tests/unit/strategies/test_funding_panel_injection.py::test_funding_cross_section_accepts_multi_inst_panels -v`
Expected: pass.

- [ ] **Step 6: Run pre-existing funding_xs tests**

Run: `pytest tests/unit/test_strategy_funding_xs.py -v`

- [ ] **Step 7: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py tests/unit/strategies/test_funding_panel_injection.py
git commit -m "feat(funding_cross_section): add multi-inst feed_funding_panel hook"
```

---

## Task 7: Add `feed_funding_panel()` to `funding_skew_momentum`

**Files:**
- Modify: `src/okx_trade/strategies/funding_skew_momentum.py`
- Test:   `tests/unit/strategies/test_funding_panel_injection.py`

- [ ] **Step 1: Locate `_bootstrap_history` REST call**

Run: `grep -n "get_funding_rate_history\|_bootstrap\|_funding_history" src/okx_trade/strategies/funding_skew_momentum.py`

- [ ] **Step 2: Write failing test**

Append:

```python
def test_funding_skew_momentum_panel_preloads_history_window():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.funding_skew_momentum import (
        FundingSkewMomentumStrategy, FundingSkewMomentumConfig,
    )
    cfg = FundingSkewMomentumConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
    )
    strat = FundingSkewMomentumStrategy(cfg)
    # 100 funding samples — strategy needs 90 minimum for z-score
    ts_ms = [1_700_000_000_000 + i * 8 * 3_600_000 for i in range(100)]
    rates = [0.0001 + (i % 5) * 0.00001 for i in range(100)]
    panel = FundingPanel(inst_id="BTC-USDT-SWAP", ts_ms=ts_ms, rates=rates)
    strat.feed_funding_panel({"BTC-USDT-SWAP": panel})
    # The strategy's internal history deque should be pre-populated from the panel
    assert len(strat._funding_history.get("BTC-USDT-SWAP", [])) == 90  # capped at maxlen=90
```

- [ ] **Step 3: Run, confirm fails**

- [ ] **Step 4: Implement**

Add `_funding_panels` dict + `_funding_source_kind` field as in Task 6.

In `feed_funding_panel`, after storing the panels, pre-fill the strategy's internal `_funding_history` deque(s) with the last `maxlen` samples from each panel:

```python
        def feed_funding_panel(self, panels: dict[str, "FundingPanel"]) -> None:
            self._funding_panels = dict(panels)
            self._funding_source_kind = "panel"
            # Pre-fill internal rolling-history deques (replaces async _bootstrap_history)
            for inst_id, panel in panels.items():
                if inst_id not in self._funding_history:
                    self._funding_history[inst_id] = deque(maxlen=self._history_maxlen)
                self._funding_history[inst_id].clear()
                for rate in panel.rates[-self._history_maxlen :]:
                    self._funding_history[inst_id].append(rate)
```

Skip `_bootstrap_history` async call when `_funding_source_kind == "panel"`. (Add guard in `on_start`.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/strategies/test_funding_panel_injection.py::test_funding_skew_momentum_panel_preloads_history_window tests/unit/test_strategy_funding_skew.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/strategies/funding_skew_momentum.py tests/unit/strategies/test_funding_panel_injection.py
git commit -m "feat(funding_skew_momentum): add feed_funding_panel preloading history"
```

---

## Task 8: Add `feed_funding_panel()` to `basis_arb`

**Files:**
- Modify: `src/okx_trade/strategies/basis_arb.py`
- Test:   `tests/unit/strategies/test_funding_panel_injection.py`

- [ ] **Step 1: Locate funding poll site**

Run: `grep -n "funding\|get_ticker" src/okx_trade/strategies/basis_arb.py`

basis_arb does not currently poll funding (it polls spot+futures tickers); funding integration here is **optional context** for entry filtering. Implement the hook as a no-op-on-empty:

- [ ] **Step 2: Write failing test**

Append:

```python
def test_basis_arb_accepts_funding_panel_for_entry_context():
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.basis_arb import BasisArbStrategy, BasisArbConfig

    cfg = BasisArbConfig(
        spot_instrument_id="BTC-USDT.OKX",
        futures_instrument_id="BTC-USDT-250627.OKX",
        spot_bar_type="BTC-USDT.OKX-1-HOUR-LAST-EXTERNAL",
    )
    strat = BasisArbStrategy(cfg)
    panel = FundingPanel(
        inst_id="BTC-USDT-SWAP",  # context only — basis_arb uses futures, but funding hints regime
        ts_ms=[1_700_000_000_000], rates=[0.0001],
    )
    strat.feed_funding_panel(panel)
    assert strat._funding_panel is panel
```

- [ ] **Step 3: Run, confirm fails**

- [ ] **Step 4: Implement hook (simple — no decision change yet, just store)**

```python
            self._funding_panel: "FundingPanel | None" = None
```

Method:
```python
        def feed_funding_panel(self, panel: "FundingPanel") -> None:
            """Optional funding-context hook for backtest. Stored but not yet used in
            entry decision (reserved for future regime-filter wiring)."""
            self._funding_panel = panel
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/strategies/test_funding_panel_injection.py tests/unit/test_strategy_basis_arb.py -v`

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/strategies/basis_arb.py tests/unit/strategies/test_funding_panel_injection.py
git commit -m "feat(basis_arb): add feed_funding_panel hook for backtest context"
```

---

## Task 9: Register `funding_carry` in `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Add entry to `SUPPORTED_STRATEGIES`**

Locate the dict (around line 45). Add:

```python
SUPPORTED_STRATEGIES = {
    "xs_momentum": ...,  # existing
    "funding_carry": {
        "import_path": "okx_trade.strategies.funding_carry:FundingCarryStrategy",
        "config_path": "okx_trade.strategies.funding_carry:FundingCarryConfig",
        "runner": "_run_funding_carry",
    },
    # ... (funding_xs, funding_skew, basis_arb in later tasks)
}
```

- [ ] **Step 2: Implement `_run_funding_carry` helper**

Append after the existing `_run_xs_momentum`:

```python
async def _run_funding_carry(args: argparse.Namespace) -> BacktestSummary:
    """Backtest funding_carry: spot + perp delta-neutral funding harvest."""
    from okx_trade.backtest.data_loader import prepare_backtest_catalog, prepare_funding_panel
    from okx_trade.backtest.runner import run_backtest_with_node, build_okx_venue_config
    from okx_trade.backtest.funding_data import FundingPanel

    catalog_path = Path(args.catalog)
    catalog_path.mkdir(parents=True, exist_ok=True)

    async with OKXRestClient(OKXSettings()) as client:
        # Spot bars for price + sizing
        spot_inst, spot_bars = await prepare_backtest_catalog(
            client, args.spot_instrument_id,
            bar_period=args.signal_bar, total=args.total_bars,
            catalog_path=catalog_path, reuse=args.reuse_data,
        )
        # Perp instrument (needed for orders; bars optional)
        perp_inst, _ = await prepare_backtest_catalog(
            client, args.perp_instrument_id,
            bar_period=args.signal_bar, total=args.total_bars,
            catalog_path=catalog_path, reuse=args.reuse_data,
        )
        # Funding panel for the perp
        funding_panel = await prepare_funding_panel(
            client, args.perp_instrument_id, total=args.funding_total,
            catalog_path=catalog_path, reuse_cache=args.reuse_data,
        )

    venue = build_okx_venue_config(starting_balance_usdt=args.equity, leverage=args.leverage)
    data = [
        BacktestDataConfig(
            catalog_path=str(catalog_path),
            data_cls=Bar.fully_qualified_name(),
            instrument_id=spot_inst.id.value,
        ),
    ]
    strat_cfg = ImportableStrategyConfig(
        strategy_path="okx_trade.strategies.funding_carry:FundingCarryStrategy",
        config_path="okx_trade.strategies.funding_carry:FundingCarryConfig",
        config={
            "spot_instrument_id": spot_inst.id.value,
            "perp_instrument_id": perp_inst.id.value,
            "spot_bar_type": f"{spot_inst.id.value}-{_bar_spec(args.signal_bar)}-LAST-EXTERNAL",
            "entry_apr_threshold": args.entry_apr_threshold,
            "exit_apr_threshold": args.exit_apr_threshold,
            "max_position_pct": args.max_position_pct,
            "account_equity_usdt": args.equity,
        },
    )
    summary, node = run_backtest_with_node(
        venue=venue, data=data, strategies=[strat_cfg],
    )
    # Inject funding panel into the running strategy (post-construction)
    for engine in node.get_engines():
        for strategy in engine.trader.strategies():
            if hasattr(strategy, "feed_funding_panel"):
                strategy.feed_funding_panel(funding_panel)
    return summary
```

- [ ] **Step 3: Add CLI args for funding_carry**

In `_parse_args` (after existing strategy-specific flags):

```python
    parser.add_argument("--spot-instrument-id", help="Spot inst (funding_carry / basis_arb)")
    parser.add_argument("--perp-instrument-id", help="Perp inst (funding_carry / *_funding)")
    parser.add_argument("--funding-total", type=int, default=1095,
                        help="Funding rate history depth in samples (default 1095 = 1y)")
    parser.add_argument("--entry-apr-threshold", type=float, default=0.08)
    parser.add_argument("--exit-apr-threshold", type=float, default=0.02)
    parser.add_argument("--max-position-pct", type=float, default=0.30)
```

- [ ] **Step 4: Wire strategy dispatch**

In `main()`, replace the hard-coded `_run_xs_momentum` call with a dispatch table:

```python
    runner_map = {
        "xs_momentum": _run_xs_momentum,
        "funding_carry": _run_funding_carry,
    }
    runner = runner_map[args.strategy]
    summary = asyncio.run(runner(args))
```

- [ ] **Step 5: Smoke test**

Run: `python scripts/backtest.py --strategy funding_carry --spot-instrument-id BTC-USDT --perp-instrument-id BTC-USDT-SWAP --signal-bar 1H --total-bars 500 --catalog ./data --reuse-data`
Expected: completes without exception, prints `BacktestSummary`.

- [ ] **Step 6: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): wire funding_carry into scripts/backtest.py"
```

---

## Task 10: Register `funding_cross_section` and `funding_skew_momentum` in `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Add entries to `SUPPORTED_STRATEGIES`**

```python
    "funding_cross_section": { ... import paths ... },
    "funding_skew_momentum": { ... import paths ... },
```

- [ ] **Step 2: Implement `_run_funding_cross_section`**

Same pattern as Task 9 but:
- Iterate `args.instrument_ids` (already comma-separated CLI arg).
- Build `prepare_backtest_catalog` + `prepare_funding_panel` for each inst.
- Pass `panels: dict[str, FundingPanel]` to `feed_funding_panel`.

```python
async def _run_funding_cross_section(args: argparse.Namespace) -> BacktestSummary:
    catalog_path = Path(args.catalog)
    inst_ids = args.instrument_ids.split(",")
    panels: dict[str, FundingPanel] = {}
    data_configs: list[BacktestDataConfig] = []
    async with OKXRestClient(OKXSettings()) as client:
        for inst_id in inst_ids:
            inst, _ = await prepare_backtest_catalog(
                client, inst_id, bar_period=args.signal_bar,
                total=args.total_bars, catalog_path=catalog_path, reuse=args.reuse_data,
            )
            panels[inst_id] = await prepare_funding_panel(
                client, inst_id, total=args.funding_total,
                catalog_path=catalog_path, reuse_cache=args.reuse_data,
            )
            data_configs.append(BacktestDataConfig(
                catalog_path=str(catalog_path),
                data_cls=Bar.fully_qualified_name(),
                instrument_id=inst.id.value,
            ))
    # ... rest analogous to Task 9, feed_funding_panel(panels)
```

- [ ] **Step 3: Implement `_run_funding_skew_momentum`**

Same pattern as `_run_funding_cross_section`. Difference: needs `funding_total >= 90 * 8 / args.signal_bar_hours` to fill 90-sample history.

- [ ] **Step 4: Register in `runner_map`**

```python
    runner_map = {
        ...,
        "funding_cross_section": _run_funding_cross_section,
        "funding_skew_momentum": _run_funding_skew_momentum,
    }
```

- [ ] **Step 5: Smoke tests**

```bash
python scripts/backtest.py --strategy funding_cross_section \
    --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
    --signal-bar 1D --total-bars 200 --reuse-data
python scripts/backtest.py --strategy funding_skew_momentum \
    --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP \
    --signal-bar 1H --total-bars 500 --reuse-data
```

Both should complete and print summaries.

- [ ] **Step 6: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): wire funding_cross_section + funding_skew_momentum"
```

---

## Task 11: Register `basis_arb` in `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Implement `_run_basis_arb`**

```python
async def _run_basis_arb(args: argparse.Namespace) -> BacktestSummary:
    """Backtest basis_arb (cash-and-carry against dated future).

    Note: until Plan 6 (cross-account margin simulator) lands, this backtest
    uses NT's standard SimulatedExchange with both legs on one MARGIN account,
    which OVERSTATES survivability vs. the real OKX setup. Production deploy
    should wait for Plan 6.
    """
    catalog_path = Path(args.catalog)
    async with OKXRestClient(OKXSettings()) as client:
        spot_inst, _ = await prepare_backtest_catalog(
            client, args.spot_instrument_id, bar_period=args.signal_bar,
            total=args.total_bars, catalog_path=catalog_path, reuse=args.reuse_data,
        )
        futures_inst, _ = await prepare_backtest_catalog(
            client, args.futures_instrument_id, bar_period=args.signal_bar,
            total=args.total_bars, catalog_path=catalog_path, reuse=args.reuse_data,
        )
        # Funding panel optional (basis_arb uses dated future, not perp; but if perp
        # ID provided as `--perp-instrument-id`, fetch it for context)
        funding_panel = None
        if args.perp_instrument_id:
            funding_panel = await prepare_funding_panel(
                client, args.perp_instrument_id, total=args.funding_total,
                catalog_path=catalog_path, reuse_cache=args.reuse_data,
            )
    # ... build venue, data configs, ImportableStrategyConfig ...
    summary, node = run_backtest_with_node(...)
    if funding_panel is not None:
        for engine in node.get_engines():
            for strategy in engine.trader.strategies():
                if hasattr(strategy, "feed_funding_panel"):
                    strategy.feed_funding_panel(funding_panel)
    return summary
```

- [ ] **Step 2: Add CLI arg**

```python
    parser.add_argument("--futures-instrument-id", help="Dated future inst (basis_arb)")
```

- [ ] **Step 3: Register in runner_map + smoke test**

```bash
python scripts/backtest.py --strategy basis_arb \
    --spot-instrument-id BTC-USDT --futures-instrument-id BTC-USDT-250627 \
    --signal-bar 1H --total-bars 500 --reuse-data
```

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): wire basis_arb with caveat note about margin sim"
```

---

## Task 12: Integration smoke test (fixture-based, no network)

**Files:**
- Create: `tests/integration/test_backtest_funding_strategies.py`
- Create: `tests/integration/fixtures/funding_btc_swap.parquet` (small, 200 rows, committed)

- [ ] **Step 1: Generate the fixture parquet**

Create `tests/integration/fixtures/__init__.py` (empty) and a helper script:

```python
# scripts/_gen_funding_fixture.py (one-time helper, not committed if you prefer)
from pathlib import Path
from okx_trade.backtest.funding_data import FundingPanel, write_funding_parquet

panel = FundingPanel(
    inst_id="BTC-USDT-SWAP",
    ts_ms=[1_700_000_000_000 + i * 8 * 3_600_000 for i in range(200)],
    rates=[0.0001 + ((i * 13) % 7 - 3) * 0.00002 for i in range(200)],
)
fixture_dir = Path("tests/integration/fixtures")
write_funding_parquet(panel, catalog_path=fixture_dir)
```

Run once: `python scripts/_gen_funding_fixture.py`. Commit the produced parquet to the repo.

- [ ] **Step 2: Write integration test**

Create `tests/integration/test_backtest_funding_strategies.py`:

```python
"""End-to-end backtest smoke for funding-aware strategies (no network)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_funding_carry_backtest_runs_and_produces_finite_sharpe(tmp_path):
    pytest.importorskip("nautilus_trader")
    from okx_trade.backtest.funding_data import read_funding_parquet

    panel = read_funding_parquet("BTC-USDT-SWAP", catalog_path=FIXTURE_DIR)
    assert len(panel.ts_ms) >= 100

    # ... build minimal BacktestNode with synthetic bars + the panel,
    # run, assert summary.sharpe is finite (math.isfinite).
```

(The full bar-construction boilerplate is mechanical — copy the pattern from `tests/unit/test_backtest_runner.py`.)

- [ ] **Step 3: Run integration tests**

Run: `pytest tests/integration/test_backtest_funding_strategies.py -v -m integration`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_backtest_funding_strategies.py tests/integration/fixtures/
git commit -m "test(backtest): add integration smoke for 4 funding-aware strategies"
```

---

## Task 13: Update strategy roadmap doc

**Files:**
- Modify: `docs/strategy_roadmap.md`

- [ ] **Step 1: Locate the 4 strategy rows**

Run: `grep -n "funding_carry\|funding_cross_section\|funding_skew\|basis_arb" docs/strategy_roadmap.md`

- [ ] **Step 2: Update status column**

For each of the 4, change the "backtest" / status column from "paper validation" / "partial" → "backtestable" with a footnote referencing this plan file.

- [ ] **Step 3: Commit**

```bash
git add docs/strategy_roadmap.md
git commit -m "docs(roadmap): mark 4 funding strategies as backtestable"
```

---

## Self-Review Checklist (run before declaring plan done)

- [ ] All 13 tasks have testable acceptance.
- [ ] No `pytest tests/unit -v` regression vs main.
- [ ] All 4 strategy smoke commands in Tasks 9–11 complete locally.
- [ ] `docs/strategy_roadmap.md` shows 4 newly-backtestable rows.
- [ ] Roadmap status tracker in [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md) flipped to "completed" for Plan 1.

---

## Execution Handoff

Plan complete and saved. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task with two-stage review (REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`).

**2. Inline Execution** — execute tasks in the current session with checkpoints (REQUIRED SUB-SKILL: `superpowers:executing-plans`).

For this plan, subagent-driven is preferred because Tasks 5–8 involve coordinated multi-file edits to strategy classes that benefit from independent review per strategy.
