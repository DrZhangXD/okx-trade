# Plan 3: Option Summary Capture + live_node Filter (option_vol_selling backtest)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable `option_vol_selling` to (a) start in live mode without loading all ~500 OKX option contracts (currently it does, causing 60s+ startup timeouts), and (b) run in backtest by replaying captured `get_option_summary()` snapshots.

**Architecture:**
1. **Live filter** — `_build_trading_node` already wires `OKXDataClientConfig`. We add a top-level `option_ulys: list[str] | None` field to that config (mirroring the existing `instrument_provider.py` filter contract). When set, only options whose underlying matches load at startup.
2. **Backtest data** — OKX's `get_option_summary(uly)` returns one snapshot per call. There is no history endpoint; we capture by polling every 60s for N days into `${catalog}/option_summary/<uly>/<YYYYMMDD>.parquet`. The strategy gets a `feed_option_snapshots()` hook that exposes lookup-by-timestamp and lookup-by-instrument.

**Tech Stack:** Python 3.12+ asyncio, pyarrow, Nautilus Trader, OKX REST.

**Roadmap reference:** [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md) — Plan 3.

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `src/okx_trade/adapter/config.py` | Add `option_ulys: list[str] | None = None` to `OKXDataClientConfig` | modify |
| `src/okx_trade/runtime/live_node.py` | Pass `option_ulys` from live yaml → `OKXDataClientConfig` | modify |
| `configs/live.yaml` | Document the new `data.option_ulys` field (commented example) | modify |
| `src/okx_trade/backtest/option_data.py` | `OptionSummarySnapshot`, `write_option_parquet`, `read_option_parquet`, `OptionSummaryPanel` | create |
| `scripts/capture_option_summary.py` | Long-running REST poll → parquet | create |
| `src/okx_trade/strategies/option_vol_selling.py` | Add `feed_option_snapshots()` hook + abstract REST source | modify |
| `scripts/backtest.py` | Register `option_vol_selling` + `_run_option_vol_selling` | modify |
| `tests/unit/backtest/test_option_data.py` | Schema + roundtrip + panel lookup | create |
| `tests/unit/strategies/test_option_vol_panel_injection.py` | Strategy accepts panel + uses panel over REST | create |
| `tests/unit/adapter/test_option_uly_filter.py` | Config + provider integration | create (or extend existing adapter tests) |
| `tests/integration/test_backtest_option_vol_selling.py` | E2E with synthetic captured snapshots | create |
| `docs/strategy_roadmap.md` | Mark option_vol_selling as backtestable | modify |

---

## Conventions

Standard conventions per [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md).

**Option-summary parquet schema:**

```
ts_ms        int64       poll timestamp UTC ms
inst_id      string      e.g. "BTC-USD-251226-100000-P"
mark_price   float64
mark_iv      float64
delta        float64
gamma        float64
vega         float64
theta        float64
underlying   string      e.g. "BTC-USD"
exp_time_ms  int64       option expiration UTC ms
strike       float64
option_type  string      "C" or "P"
```

Partition: `${catalog}/option_summary/<underlying>/<YYYYMMDD>.parquet`.

---

## Task 1: Add `option_ulys` to `OKXDataClientConfig`

**Files:**
- Modify: `src/okx_trade/adapter/config.py`
- Create: `tests/unit/adapter/test_option_uly_filter.py`

- [ ] **Step 1: Failing test**

```python
"""Tests for option underlying filter on DataClientConfig."""
from __future__ import annotations

import pytest

from okx_trade.adapter.config import OKXDataClientConfig


def test_data_client_config_accepts_option_ulys():
    cfg = OKXDataClientConfig(
        api_key="x", api_secret="y", passphrase="z", is_demo=True,
        option_ulys=["BTC-USD"],
    )
    assert cfg.option_ulys == ["BTC-USD"]


def test_data_client_config_option_ulys_defaults_none():
    cfg = OKXDataClientConfig(api_key="x", api_secret="y", passphrase="z", is_demo=True)
    assert cfg.option_ulys is None
```

- [ ] **Step 2: Run → fails (TypeError or attribute missing)**

Run: `pytest tests/unit/adapter/test_option_uly_filter.py -v`

- [ ] **Step 3: Add field**

In `src/okx_trade/adapter/config.py`, add to `OKXDataClientConfig`:

```python
    option_ulys: list[str] | None = None
    """Restrict OPTION loading to these underlyings (e.g. ["BTC-USD"]).
    None = load all (~500 contracts; causes long startup)."""
```

- [ ] **Step 4: Test passes; commit**

```bash
git add src/okx_trade/adapter/config.py tests/unit/adapter/test_option_uly_filter.py
git commit -m "feat(adapter): add OKXDataClientConfig.option_ulys filter"
```

---

## Task 2: Pipe `option_ulys` from config to instrument provider

**Files:**
- Modify: `src/okx_trade/adapter/factories.py` (or wherever the data client is constructed)
- Modify: `src/okx_trade/adapter/instrument_provider.py` (already has filter logic; just ensure default-load path picks up the config)

- [ ] **Step 1: Grep for where InstrumentProvider is built**

```bash
grep -rn "OKXInstrumentProvider\|instrument_provider" src/okx_trade/adapter/ | head
```

- [ ] **Step 2: Failing test (extends Task 1 test file)**

```python
@pytest.mark.asyncio
async def test_instrument_provider_passes_option_ulys_filter(monkeypatch):
    """When option_ulys is set, provider's default load should pass it as filter."""
    from okx_trade.adapter.instrument_provider import OKXInstrumentProvider
    captured: dict = {}

    async def fake_load_all(filters: dict | None = None) -> None:
        captured["filters"] = filters

    provider = OKXInstrumentProvider(rest_client=None, option_ulys=["BTC-USD"])
    monkeypatch.setattr(provider, "_load_filtered", fake_load_all)
    await provider.load_all_async()
    assert captured["filters"] == {"option_ulys": ["BTC-USD"]}
```

- [ ] **Step 3: Wire the field**

- Add `option_ulys` param to `OKXInstrumentProvider.__init__`.
- In `load_all_async`, build the `filters` dict from constructor args when caller didn't pass one.
- In `factories.py` (data client factory), pass `option_ulys=config.option_ulys` when constructing the provider.

- [ ] **Step 4: Run all adapter tests**

Run: `pytest tests/unit/test_adapter_*.py tests/unit/adapter/ -v`

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/adapter/factories.py src/okx_trade/adapter/instrument_provider.py tests/unit/adapter/test_option_uly_filter.py
git commit -m "feat(adapter): pipe option_ulys filter through provider construction"
```

---

## Task 3: Plumb `option_ulys` through `_build_trading_node`

**Files:**
- Modify: `src/okx_trade/runtime/live_node.py` (around line 456 — `data_clients={"OKX": OKXDataClientConfig(...)}`)
- Modify: `configs/live.yaml` (add commented example)

- [ ] **Step 1: Modify `_build_trading_node`**

Insert in the `OKXDataClientConfig(...)` block:

```python
            "OKX": OKXDataClientConfig(
                api_key=creds.get("api_key"),
                api_secret=creds.get("api_secret"),
                passphrase=creds.get("passphrase"),
                is_demo=is_paper,
                http_proxy=creds.get("http_proxy"),
                option_ulys=live_cfg.get("data", {}).get("option_ulys"),
            ),
```

- [ ] **Step 2: Add commented example to `configs/live.yaml`**

```yaml
# data:
#   # When option strategies are enabled, restrict option loading to these
#   # underlyings to avoid loading ~500 contracts at startup. Required for
#   # option_vol_selling — set to ["BTC-USD"] (or whatever the strategy uses).
#   option_ulys: ["BTC-USD"]
```

- [ ] **Step 3: Manual smoke (paper account, ~10s, just verify startup time drops)**

```bash
python scripts/live.py --config configs/live.yaml --strategies option_vol_selling 2>&1 | head -30
# Should log "loaded 30 BTC-USD options" instead of "loaded 487 options".
```

- [ ] **Step 4: Commit**

```bash
git add src/okx_trade/runtime/live_node.py configs/live.yaml
git commit -m "feat(live_node): pipe data.option_ulys from yaml to OKXDataClientConfig"
```

---

## Task 4: `OptionSummarySnapshot` + parquet roundtrip

**Files:**
- Create: `src/okx_trade/backtest/option_data.py`
- Create: `tests/unit/backtest/test_option_data.py`

- [ ] **Step 1: Failing roundtrip test**

```python
"""Tests for option summary capture/replay parquet."""
from __future__ import annotations

import pytest

from okx_trade.backtest.option_data import OptionSummarySnapshot, write_option_parquet, read_option_parquet


def test_option_summary_parquet_roundtrip(tmp_path):
    snaps = [
        OptionSummarySnapshot(
            ts_ms=1_700_000_000_000, inst_id="BTC-USD-251226-100000-P",
            mark_price=1234.5, mark_iv=0.55, delta=-0.4, gamma=0.0001,
            vega=120.0, theta=-15.0, underlying="BTC-USD",
            exp_time_ms=1_766_793_600_000, strike=100_000.0, option_type="P",
        ),
        OptionSummarySnapshot(
            ts_ms=1_700_000_060_000, inst_id="BTC-USD-251226-100000-P",
            mark_price=1240.0, mark_iv=0.56, delta=-0.41, gamma=0.0001,
            vega=121.0, theta=-15.1, underlying="BTC-USD",
            exp_time_ms=1_766_793_600_000, strike=100_000.0, option_type="P",
        ),
    ]
    write_option_parquet(snaps, catalog_path=tmp_path)
    loaded = read_option_parquet("BTC-USD", catalog_path=tmp_path)
    assert len(loaded) == 2
    assert loaded[0].mark_iv == 0.55
```

- [ ] **Step 2: Implement**

```python
"""Option summary capture + replay storage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True, slots=True)
class OptionSummarySnapshot:
    ts_ms: int
    inst_id: str
    mark_price: float
    mark_iv: float
    delta: float
    gamma: float
    vega: float
    theta: float
    underlying: str
    exp_time_ms: int
    strike: float
    option_type: str  # "C" or "P"


_OPTION_SCHEMA = pa.schema([
    pa.field(name, pa.int64() if name in {"ts_ms", "exp_time_ms"}
             else pa.string() if name in {"inst_id", "underlying", "option_type"}
             else pa.float64())
    for name in (
        "ts_ms", "inst_id", "mark_price", "mark_iv", "delta", "gamma",
        "vega", "theta", "underlying", "exp_time_ms", "strike", "option_type",
    )
])


def _day_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")


def write_option_parquet(
    snapshots: list[OptionSummarySnapshot], *, catalog_path: Path,
) -> list[Path]:
    if not snapshots:
        return []
    by_uly_day: dict[tuple[str, str], list[OptionSummarySnapshot]] = {}
    for s in snapshots:
        by_uly_day.setdefault((s.underlying, _day_key(s.ts_ms)), []).append(s)
    written: list[Path] = []
    for (uly, day), batch in by_uly_day.items():
        base = catalog_path / "option_summary" / uly
        base.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_arrays([
            pa.array([s.ts_ms for s in batch], type=pa.int64()),
            pa.array([s.inst_id for s in batch], type=pa.string()),
            pa.array([s.mark_price for s in batch], type=pa.float64()),
            pa.array([s.mark_iv for s in batch], type=pa.float64()),
            pa.array([s.delta for s in batch], type=pa.float64()),
            pa.array([s.gamma for s in batch], type=pa.float64()),
            pa.array([s.vega for s in batch], type=pa.float64()),
            pa.array([s.theta for s in batch], type=pa.float64()),
            pa.array([s.underlying for s in batch], type=pa.string()),
            pa.array([s.exp_time_ms for s in batch], type=pa.int64()),
            pa.array([s.strike for s in batch], type=pa.float64()),
            pa.array([s.option_type for s in batch], type=pa.string()),
        ], schema=_OPTION_SCHEMA)
        path = base / f"{day}.parquet"
        pq.write_table(table, path, compression="snappy")
        written.append(path)
    return written


def read_option_parquet(
    underlying: str, *, catalog_path: Path,
) -> list[OptionSummarySnapshot]:
    base = catalog_path / "option_summary" / underlying
    if not base.exists():
        raise FileNotFoundError(f"no option cache at {base}")
    files = sorted(base.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet files in {base}")
    merged = pa.concat_tables([pq.read_table(f) for f in files])
    sort_idx = pa.compute.sort_indices(merged, sort_keys=[("ts_ms", "ascending")])
    sorted_table = merged.take(sort_idx)
    return [OptionSummarySnapshot(**r) for r in sorted_table.to_pylist()]
```

- [ ] **Step 3: Commit**

```bash
git add src/okx_trade/backtest/option_data.py tests/unit/backtest/test_option_data.py
git commit -m "feat(backtest): add OptionSummarySnapshot + daily parquet cache"
```

---

## Task 5: `OptionSummaryPanel` lookup-by-timestamp helper

**Files:**
- Modify: `src/okx_trade/backtest/option_data.py`
- Modify: `tests/unit/backtest/test_option_data.py`

- [ ] **Step 1: Failing test**

```python
def test_option_panel_returns_snapshots_at_or_before_ts():
    from okx_trade.backtest.option_data import OptionSummaryPanel

    snaps = [
        OptionSummarySnapshot(
            ts_ms=1_000, inst_id="A", mark_price=1, mark_iv=0.5,
            delta=0, gamma=0, vega=0, theta=0, underlying="BTC-USD",
            exp_time_ms=2_000_000, strike=100, option_type="C",
        ),
        OptionSummarySnapshot(
            ts_ms=2_000, inst_id="A", mark_price=1.1, mark_iv=0.51,
            delta=0, gamma=0, vega=0, theta=0, underlying="BTC-USD",
            exp_time_ms=2_000_000, strike=100, option_type="C",
        ),
        OptionSummarySnapshot(
            ts_ms=1_000, inst_id="B", mark_price=2, mark_iv=0.5,
            delta=0, gamma=0, vega=0, theta=0, underlying="BTC-USD",
            exp_time_ms=2_000_000, strike=110, option_type="C",
        ),
    ]
    panel = OptionSummaryPanel(snaps)
    snap_a = panel.snapshot_at_or_before("A", 1_500)
    assert snap_a is not None and snap_a.mark_price == 1.0  # earlier of two

    snap_a_later = panel.snapshot_at_or_before("A", 2_500)
    assert snap_a_later.mark_price == 1.1

    assert panel.snapshot_at_or_before("A", 500) is None  # before earliest

    chain = panel.chain_at_or_before(1_500)
    assert {s.inst_id for s in chain} == {"A", "B"}
```

- [ ] **Step 2: Implement**

```python
from bisect import bisect_right
from collections import defaultdict


class OptionSummaryPanel:
    """In-memory lookup over captured option summary snapshots."""

    def __init__(self, snapshots: list[OptionSummarySnapshot]) -> None:
        by_inst: dict[str, list[OptionSummarySnapshot]] = defaultdict(list)
        for s in snapshots:
            by_inst[s.inst_id].append(s)
        self._by_inst: dict[str, list[OptionSummarySnapshot]] = {}
        for inst_id, snaps in by_inst.items():
            self._by_inst[inst_id] = sorted(snaps, key=lambda s: s.ts_ms)
        # All timestamps for chain queries
        self._all_ts: list[int] = sorted({s.ts_ms for s in snapshots})

    def snapshot_at_or_before(
        self, inst_id: str, ts_ms: int,
    ) -> OptionSummarySnapshot | None:
        snaps = self._by_inst.get(inst_id)
        if not snaps or ts_ms < snaps[0].ts_ms:
            return None
        ts_list = [s.ts_ms for s in snaps]
        idx = bisect_right(ts_list, ts_ms) - 1
        return snaps[idx]

    def chain_at_or_before(self, ts_ms: int) -> list[OptionSummarySnapshot]:
        """All instruments' snapshots at or before ``ts_ms``."""
        out: list[OptionSummarySnapshot] = []
        for inst_id in self._by_inst:
            s = self.snapshot_at_or_before(inst_id, ts_ms)
            if s is not None:
                out.append(s)
        return out
```

- [ ] **Step 3: Run, commit**

```bash
git add src/okx_trade/backtest/option_data.py tests/unit/backtest/test_option_data.py
git commit -m "feat(backtest): add OptionSummaryPanel for timestamped chain lookup"
```

---

## Task 6: Capture script

**Files:**
- Create: `scripts/capture_option_summary.py`

- [ ] **Step 1: Implement**

```python
"""Poll OKX option summary every N seconds → parquet for backtest replay.

Usage:
    python scripts/capture_option_summary.py --underlying BTC-USD \\
        --interval-sec 60 --duration-hours 168 --catalog ./data
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.backtest.option_data import OptionSummarySnapshot, write_option_parquet  # noqa: E402


def _to_snapshot(item, *, ts_ms: int, underlying: str) -> OptionSummarySnapshot:
    return OptionSummarySnapshot(
        ts_ms=ts_ms,
        inst_id=item.inst_id,
        mark_price=float(item.mark_price or 0),
        mark_iv=float(item.mark_vol or 0),
        delta=float(item.delta or 0),
        gamma=float(item.gamma or 0),
        vega=float(item.vega or 0),
        theta=float(item.theta or 0),
        underlying=underlying,
        exp_time_ms=int(item.exp_time.timestamp() * 1000) if item.exp_time else 0,
        strike=float(item.strike or 0),
        option_type=item.option_type or "?",
    )


async def _main(args: argparse.Namespace) -> None:
    catalog = Path(args.catalog)
    stop_at = time.time() + args.duration_hours * 3600
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())

    buffer: list[OptionSummarySnapshot] = []
    async with OKXRestClient(OKXSettings()) as client:
        while not stop_event.is_set() and time.time() < stop_at:
            ts_ms = int(time.time() * 1000)
            try:
                items = await client.public.get_option_summary(args.underlying)
                buffer.extend(_to_snapshot(it, ts_ms=ts_ms, underlying=args.underlying) for it in items)
                if len(buffer) >= args.flush_rows:
                    write_option_parquet(buffer, catalog_path=catalog)
                    print(f"flushed {len(buffer)} rows at {time.strftime('%H:%M:%S')}", flush=True)
                    buffer.clear()
            except Exception as exc:
                print(f"poll failed: {exc}", flush=True)
            await asyncio.sleep(args.interval_sec)
    if buffer:
        write_option_parquet(buffer, catalog_path=catalog)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--underlying", required=True)
    p.add_argument("--interval-sec", type=int, default=60)
    p.add_argument("--duration-hours", type=float, default=168)
    p.add_argument("--catalog", default="./data")
    p.add_argument("--flush-rows", type=int, default=5_000)
    asyncio.run(_main(p.parse_args()))
```

- [ ] **Step 2: Manual smoke test (2 min)**

```bash
timeout 120 python scripts/capture_option_summary.py --underlying BTC-USD --duration-hours 0.04 --interval-sec 30
ls -la data/option_summary/BTC-USD/
```

- [ ] **Step 3: Commit**

```bash
git add scripts/capture_option_summary.py
git commit -m "feat(scripts): add option-summary REST poll → parquet capture"
```

---

## Task 7: Strategy `feed_option_snapshots()` hook

**Files:**
- Modify: `src/okx_trade/strategies/option_vol_selling.py`
- Create: `tests/unit/strategies/test_option_vol_panel_injection.py`

- [ ] **Step 1: Failing test**

```python
def test_option_vol_strategy_accepts_panel():
    import pytest
    pytest.importorskip("nautilus_trader")
    from okx_trade.strategies.option_vol_selling import (
        OptionVolSellingStrategy, OptionVolSellingConfig,
    )
    from okx_trade.backtest.option_data import OptionSummaryPanel, OptionSummarySnapshot

    cfg = OptionVolSellingConfig(
        underlying="BTC-USD",
        perp_instrument_id="BTC-USDT-SWAP.OKX",
        perp_bar_type="BTC-USDT-SWAP.OKX-1-HOUR-LAST-EXTERNAL",
    )
    strat = OptionVolSellingStrategy(cfg)
    panel = OptionSummaryPanel([
        OptionSummarySnapshot(
            ts_ms=1_700_000_000_000, inst_id="BTC-USD-251226-100000-C",
            mark_price=1234, mark_iv=0.55, delta=0.4, gamma=0.0001,
            vega=120, theta=-15, underlying="BTC-USD",
            exp_time_ms=1_766_793_600_000, strike=100_000, option_type="C",
        ),
    ])
    strat.feed_option_snapshots(panel)
    assert strat._option_panel is panel
    assert strat._option_source_kind == "panel"
```

- [ ] **Step 2: Implement hook**

In `option_vol_selling.py` `__init__`:

```python
            self._option_panel: "OptionSummaryPanel | None" = None
            self._option_source_kind: str = "rest"
```

Method:

```python
        def feed_option_snapshots(self, panel: "OptionSummaryPanel") -> None:
            """Inject pre-captured option summary panel for backtest."""
            self._option_panel = panel
            self._option_source_kind = "panel"
```

Refactor the existing `summaries = await self._rest.public.get_option_summary(...)` site (around line 248) to route through `_fetch_option_chain()`:

```python
        async def _fetch_option_chain(self) -> list:
            if self._option_source_kind == "panel" and self._option_panel is not None:
                if self._last_check_ts_ns <= 0:
                    return []
                ts_ms = int(self._last_check_ts_ns // 1_000_000)
                return self._option_panel.chain_at_or_before(ts_ms)
            # REST path (live)
            return await self._rest.public.get_option_summary(self.config.underlying)
```

- [ ] **Step 3: Run tests; commit**

```bash
git add src/okx_trade/strategies/option_vol_selling.py tests/unit/strategies/test_option_vol_panel_injection.py
git commit -m "feat(option_vol_selling): add feed_option_snapshots panel hook"
```

---

## Task 8: Register `option_vol_selling` in `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Implement `_run_option_vol_selling`**

```python
async def _run_option_vol_selling(args: argparse.Namespace) -> BacktestSummary:
    from okx_trade.backtest.option_data import OptionSummaryPanel, read_option_parquet

    catalog_path = Path(args.catalog)
    async with OKXRestClient(OKXSettings()) as client:
        perp_inst, _ = await prepare_backtest_catalog(
            client, args.perp_instrument_id, bar_period=args.signal_bar,
            total=args.total_bars, catalog_path=catalog_path, reuse=args.reuse_data,
        )
    panel = OptionSummaryPanel(read_option_parquet(args.option_underlying, catalog_path=catalog_path))

    venue = build_okx_venue_config(starting_balance_usdt=args.equity, leverage=args.leverage)
    data = [BacktestDataConfig(
        catalog_path=str(catalog_path), data_cls=Bar.fully_qualified_name(),
        instrument_id=perp_inst.id.value,
    )]
    strat_cfg = ImportableStrategyConfig(
        strategy_path="okx_trade.strategies.option_vol_selling:OptionVolSellingStrategy",
        config_path="okx_trade.strategies.option_vol_selling:OptionVolSellingConfig",
        config={
            "underlying": args.option_underlying,
            "perp_instrument_id": perp_inst.id.value,
            "perp_bar_type": f"{perp_inst.id.value}-{_bar_spec(args.signal_bar)}-LAST-EXTERNAL",
            "account_equity_usdt": args.equity,
        },
    )
    summary, node = run_backtest_with_node(venue=venue, data=data, strategies=[strat_cfg])
    for engine in node.get_engines():
        for strategy in engine.trader.strategies():
            if hasattr(strategy, "feed_option_snapshots"):
                strategy.feed_option_snapshots(panel)
    return summary
```

- [ ] **Step 2: Add CLI args + register**

```python
    parser.add_argument("--option-underlying", help="e.g. BTC-USD (for option_vol_selling)")
```

- [ ] **Step 3: Smoke test (requires capture)**

```bash
python scripts/backtest.py --strategy option_vol_selling \
    --option-underlying BTC-USD --perp-instrument-id BTC-USDT-SWAP \
    --signal-bar 1H --total-bars 168 --reuse-data
```

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): wire option_vol_selling via captured option summary"
```

---

## Task 9: Integration test + fixture

**Files:**
- Create: `tests/integration/test_backtest_option_vol_selling.py`
- Create: `tests/integration/fixtures/option_summary_btc.parquet` (synthetic, ~50 instruments × 24 polls)

- [ ] **Step 1: Generate fixture (one-off script, commit parquet)**
- [ ] **Step 2: Integration test asserts: backtest completes, panel.chain_at_or_before nonempty, summary.sharpe finite**
- [ ] **Step 3: Commit**

---

## Task 10: Update roadmap doc

- [ ] Mark `option_vol_selling` as "backtestable (requires prior summary capture + live needs option_ulys filter)".
- [ ] Commit.

---

## Self-Review Checklist

- [ ] `option_ulys` filter actually reduces startup time on paper (Task 3 smoke).
- [ ] All capture/replay roundtrips pass.
- [ ] Backtest produces finite stats on synthetic fixture.

---

## Execution Handoff

**1. Subagent-Driven (recommended)** — `superpowers:subagent-driven-development`. Tasks 1–3 are coupled (config field + provider + node wiring) and benefit from one focused subagent. Tasks 4–6 (data infra) can be parallel. Tasks 7–10 sequential.

**2. Inline Execution** — `superpowers:executing-plans`.
