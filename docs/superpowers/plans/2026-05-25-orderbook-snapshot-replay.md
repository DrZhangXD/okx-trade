# Plan 2: Orderbook Snapshot Capture + Replay (ob_imbalance backtest)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable `ob_imbalance` to run in backtest by (a) capturing live OKX `books5` WS snapshots to parquet, and (b) replaying those snapshots into the strategy's existing `process_orderbook()` hook alongside a bar-driven runner.

**Architecture:** OKX has **no historical orderbook REST endpoint**. We use a capture-and-replay approach: a long-running `scripts/capture_orderbook.py` writes 1s-downsampled `books5` frames to `${catalog}/books5/<inst>/<YYYYMMDD>.parquet`. For backtest, a thin `OrderbookReplayRunner` wraps the NT `BacktestNode` with a pre-bar hook that fires all orderbook frames whose `ts_ms` falls within the current bar, calling `strategy.process_orderbook(book)` before `on_bar`. The strategy doesn't change — it already supports `subscribe_books5=False` for backtest mode and exposes `process_orderbook()` as an injection point.

**Tech Stack:** Python 3.12+ asyncio, websockets, pyarrow/parquet, Nautilus Trader (BacktestNode), OKX models.OrderBook.

**Roadmap reference:** [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md) — Plan 2.

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `src/okx_trade/backtest/orderbook_data.py` | New: `OrderbookFrame` dataclass, `write_orderbook_parquet`, `read_orderbook_parquet`, `OrderbookReplayStream` | create |
| `src/okx_trade/backtest/orderbook_runner.py` | New: `run_orderbook_replay_backtest()` wrapping NT BacktestNode + pre-bar hook | create |
| `scripts/capture_orderbook.py` | New: long-running WS capture writing parquet files | create |
| `scripts/backtest.py` | Register `ob_imbalance` in `SUPPORTED_STRATEGIES`; add `_run_ob_imbalance` | modify |
| `tests/unit/backtest/test_orderbook_data.py` | Roundtrip + downsampling + replay-stream tests | create |
| `tests/unit/backtest/test_orderbook_runner.py` | Mock-bar + mock-book interleave test | create |
| `tests/integration/test_backtest_ob_imbalance.py` | E2E smoke with a tiny captured fixture (committed) | create |
| `tests/integration/fixtures/books5_btc_swap.parquet` | ~500 rows of synthetic captured data | create |
| `docs/strategy_roadmap.md` | Mark ob_imbalance as backtestable | modify |

---

## Conventions

All conventions from [2026-05-25-funding-rate-backtest-data.md](2026-05-25-funding-rate-backtest-data.md) apply.

**New parquet schema (orderbook frame):**

```
ts_ms       int64        snapshot timestamp UTC ms
bids        list[list]   [[price, size], ...] for top N levels (default 5)
asks        list[list]   [[price, size], ...]
```

Compression: `lz4` (faster than snappy, books5 frames are repetitive).

Partition: `${catalog}/books5/<inst_id>/<YYYYMMDD>.parquet` (daily, since this is high-volume data).

---

## Task 1: `OrderbookFrame` dataclass + parquet roundtrip

**Files:**
- Create: `src/okx_trade/backtest/orderbook_data.py`
- Create: `tests/unit/backtest/test_orderbook_data.py`

- [ ] **Step 1: Write failing roundtrip test**

```python
"""Tests for orderbook snapshot capture + replay infrastructure."""
from __future__ import annotations

import pytest

from okx_trade.backtest.orderbook_data import (
    OrderbookFrame, write_orderbook_parquet, read_orderbook_parquet,
)


def test_orderbook_frame_parquet_roundtrip(tmp_path):
    frames = [
        OrderbookFrame(
            inst_id="BTC-USDT-SWAP", ts_ms=1_700_000_000_000,
            bids=[[60000.0, 1.5], [59999.5, 2.0]], asks=[[60000.5, 1.2], [60001.0, 0.8]],
        ),
        OrderbookFrame(
            inst_id="BTC-USDT-SWAP", ts_ms=1_700_000_001_000,
            bids=[[60001.0, 1.0]], asks=[[60001.5, 1.1]],
        ),
    ]
    paths = write_orderbook_parquet(frames, catalog_path=tmp_path)
    assert all(p.exists() for p in paths)

    loaded = read_orderbook_parquet("BTC-USDT-SWAP", catalog_path=tmp_path)
    assert len(loaded) == 2
    assert loaded[0].ts_ms == frames[0].ts_ms
    assert loaded[0].bids == frames[0].bids
    assert loaded[1].asks == frames[1].asks
```

- [ ] **Step 2: Run, confirm fails (ImportError)**

- [ ] **Step 3: Implement**

```python
"""Orderbook snapshot capture + parquet storage + replay stream."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True, slots=True)
class OrderbookFrame:
    """One 5-level orderbook snapshot at a point in time.

    bids/asks are nested lists ``[[price, size], ...]`` sorted best-first.
    """

    inst_id: str
    ts_ms: int
    bids: list[list[float]]
    asks: list[list[float]]


_FRAME_SCHEMA = pa.schema([
    pa.field("ts_ms", pa.int64()),
    pa.field("bids", pa.list_(pa.list_(pa.float64()))),
    pa.field("asks", pa.list_(pa.list_(pa.float64()))),
])


def _day_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")


def write_orderbook_parquet(
    frames: list[OrderbookFrame], *, catalog_path: Path,
) -> list[Path]:
    """Write frames bucketed by day under ``${catalog}/books5/<inst_id>/<YYYYMMDD>.parquet``.

    Frames must share one inst_id (raises ValueError otherwise).
    """
    if not frames:
        return []
    inst_id = frames[0].inst_id
    if any(f.inst_id != inst_id for f in frames):
        raise ValueError("write_orderbook_parquet: all frames must share inst_id")

    base = catalog_path / "books5" / inst_id
    base.mkdir(parents=True, exist_ok=True)

    by_day: dict[str, list[OrderbookFrame]] = {}
    for f in frames:
        by_day.setdefault(_day_key(f.ts_ms), []).append(f)

    written: list[Path] = []
    for key, day_frames in by_day.items():
        table = pa.Table.from_arrays([
            pa.array([f.ts_ms for f in day_frames], type=pa.int64()),
            pa.array([f.bids for f in day_frames]),
            pa.array([f.asks for f in day_frames]),
        ], schema=_FRAME_SCHEMA)
        path = base / f"{key}.parquet"
        pq.write_table(table, path, compression="lz4")
        written.append(path)
    return written


def read_orderbook_parquet(
    inst_id: str, *, catalog_path: Path,
) -> list[OrderbookFrame]:
    base = catalog_path / "books5" / inst_id
    if not base.exists():
        raise FileNotFoundError(f"no orderbook cache at {base}")
    files = sorted(base.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet files in {base}")
    tables = [pq.read_table(f) for f in files]
    merged = pa.concat_tables(tables)
    sort_idx = pa.compute.sort_indices(merged, sort_keys=[("ts_ms", "ascending")])
    sorted_table = merged.take(sort_idx)
    return [
        OrderbookFrame(
            inst_id=inst_id,
            ts_ms=int(r["ts_ms"]),
            bids=r["bids"], asks=r["asks"],
        )
        for r in sorted_table.to_pylist()
    ]
```

- [ ] **Step 4: Run test**

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/backtest/orderbook_data.py tests/unit/backtest/test_orderbook_data.py
git commit -m "feat(backtest): add OrderbookFrame + parquet daily-partitioned cache"
```

---

## Task 2: `OrderbookReplayStream` (timestamp-ordered iterator)

**Files:**
- Modify: `src/okx_trade/backtest/orderbook_data.py`
- Modify: `tests/unit/backtest/test_orderbook_data.py`

- [ ] **Step 1: Test for stream slicing**

```python
def test_orderbook_replay_stream_yields_frames_within_window(tmp_path):
    from okx_trade.backtest.orderbook_data import OrderbookReplayStream

    frames = [
        OrderbookFrame("BTC-USDT-SWAP", 1_000, [[1.0, 1.0]], [[2.0, 1.0]]),
        OrderbookFrame("BTC-USDT-SWAP", 2_500, [[1.1, 1.0]], [[2.1, 1.0]]),
        OrderbookFrame("BTC-USDT-SWAP", 3_999, [[1.2, 1.0]], [[2.2, 1.0]]),
        OrderbookFrame("BTC-USDT-SWAP", 5_000, [[1.3, 1.0]], [[2.3, 1.0]]),
    ]
    stream = OrderbookReplayStream(frames)
    # Drain frames with ts_ms in [2000, 4000)
    in_window = list(stream.drain_until(4_000))
    assert [f.ts_ms for f in in_window] == [1_000, 2_500, 3_999]
    # Next drain continues from cursor
    assert [f.ts_ms for f in stream.drain_until(10_000)] == [5_000]
```

- [ ] **Step 2: Run, confirm fails**

- [ ] **Step 3: Implement**

Append to `orderbook_data.py`:

```python
from collections.abc import Iterator


class OrderbookReplayStream:
    """Iterator over orderbook frames in timestamp order with windowed drain.

    Used by the replay runner to fire ``process_orderbook(book)`` for every
    frame whose ts_ms falls within the bar's window before the bar fires.
    """

    def __init__(self, frames: list[OrderbookFrame]) -> None:
        self._frames = sorted(frames, key=lambda f: f.ts_ms)
        self._cursor = 0

    def drain_until(self, ts_ms_exclusive: int) -> Iterator[OrderbookFrame]:
        """Yield all frames with ts_ms < ``ts_ms_exclusive``, advance cursor."""
        while self._cursor < len(self._frames) and self._frames[self._cursor].ts_ms < ts_ms_exclusive:
            yield self._frames[self._cursor]
            self._cursor += 1

    def __len__(self) -> int:
        return len(self._frames)

    def remaining(self) -> int:
        return len(self._frames) - self._cursor
```

- [ ] **Step 4: Run, commit**

```bash
git add src/okx_trade/backtest/orderbook_data.py tests/unit/backtest/test_orderbook_data.py
git commit -m "feat(backtest): add OrderbookReplayStream for timestamp-windowed drain"
```

---

## Task 3: WS capture script

**Files:**
- Create: `scripts/capture_orderbook.py`

This script is operationally critical but mostly mechanical. No unit test (it's an entry-point shim around existing WS client).

- [ ] **Step 1: Implement**

```python
"""Capture OKX books5 WS snapshots → parquet for backtest replay.

Usage:
    python scripts/capture_orderbook.py --inst-id BTC-USDT-SWAP \\
        --catalog ./data --downsample-sec 1 --duration-hours 168

Writes one parquet file per UTC day under
    ${catalog}/books5/<inst_id>/<YYYYMMDD>.parquet
Flushes every 60 seconds to limit data loss on crash.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXSettings  # noqa: E402
from okx_trade.backtest.orderbook_data import OrderbookFrame, write_orderbook_parquet  # noqa: E402
from okx_trade.strategies.ob_imbalance import parse_okx_books5_frame  # noqa: E402
from okx_trade.ws.public import OKXPublicWsClient  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--inst-id", required=True)
    p.add_argument("--catalog", default="./data")
    p.add_argument("--downsample-sec", type=int, default=1,
                   help="Keep at most 1 frame per N seconds (default 1)")
    p.add_argument("--duration-hours", type=float, default=168,
                   help="Total capture duration in hours (default 7 days)")
    p.add_argument("--flush-sec", type=int, default=60)
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    catalog = Path(args.catalog)
    settings = OKXSettings()
    stop_at = time.time() + args.duration_hours * 3600

    buffer: deque[OrderbookFrame] = deque()
    last_kept_ms = 0
    last_flush = time.time()

    stop_event = asyncio.Event()
    def _on_signal(*_: object) -> None:
        stop_event.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_signal)

    async with OKXPublicWsClient(settings) as ws:
        await ws.subscribe([{"channel": "books5", "instId": args.inst_id}])
        async for frame in ws:
            if stop_event.is_set() or time.time() >= stop_at:
                break
            book = parse_okx_books5_frame(frame, args.inst_id)
            if book is None:
                continue
            ts_ms = int(book.ts_ms)
            if ts_ms - last_kept_ms < args.downsample_sec * 1000:
                continue
            last_kept_ms = ts_ms
            buffer.append(OrderbookFrame(
                inst_id=args.inst_id,
                ts_ms=ts_ms,
                bids=[[float(b.price), float(b.size)] for b in book.bids],
                asks=[[float(a.price), float(a.size)] for a in book.asks],
            ))
            if time.time() - last_flush >= args.flush_sec:
                write_orderbook_parquet(list(buffer), catalog_path=catalog)
                print(f"flushed {len(buffer)} frames at {time.strftime('%H:%M:%S')}", flush=True)
                buffer.clear()
                last_flush = time.time()
    # Final flush
    if buffer:
        write_orderbook_parquet(list(buffer), catalog_path=catalog)
        print(f"final flush: {len(buffer)} frames")


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
```

- [ ] **Step 2: Manual smoke test (60s capture)**

```bash
timeout 60 python scripts/capture_orderbook.py --inst-id BTC-USDT-SWAP --catalog ./data --duration-hours 0.02
ls -la data/books5/BTC-USDT-SWAP/
```

Expected: parquet file present, ≥ 30 rows (60s at 1Hz).

- [ ] **Step 3: Commit**

```bash
git add scripts/capture_orderbook.py
git commit -m "feat(scripts): add WS books5 capture-to-parquet for backtest replay"
```

---

## Task 4: `run_orderbook_replay_backtest()` runner

**Files:**
- Create: `src/okx_trade/backtest/orderbook_runner.py`
- Create: `tests/unit/backtest/test_orderbook_runner.py`

- [ ] **Step 1: Failing test (mocked)**

```python
"""Tests for orderbook-replay backtest runner."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from okx_trade.backtest.orderbook_data import OrderbookFrame, OrderbookReplayStream


def test_replay_runner_fires_process_orderbook_for_frames_within_bar_window():
    from okx_trade.backtest.orderbook_runner import _replay_books_for_bar

    frames = [
        OrderbookFrame("X", 1_000, [[1.0, 1.0]], [[2.0, 1.0]]),
        OrderbookFrame("X", 1_500, [[1.1, 1.0]], [[2.1, 1.0]]),
        OrderbookFrame("X", 2_500, [[1.2, 1.0]], [[2.2, 1.0]]),
    ]
    stream = OrderbookReplayStream(frames)
    strategy = MagicMock()
    # Drain books with ts_ms < 2000
    _replay_books_for_bar(strategy, stream, bar_ts_ms_exclusive=2_000)
    assert strategy.process_orderbook.call_count == 2
```

- [ ] **Step 2: Implement (replay helper + full runner)**

```python
"""Orderbook-replay backtest: wraps NT BacktestNode with pre-bar book firing."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..models.market import OrderBook, OrderBookLevel
from .orderbook_data import OrderbookFrame, OrderbookReplayStream
from .runner import BacktestSummary, run_backtest_with_node


def _frame_to_orderbook(frame: OrderbookFrame) -> OrderBook:
    """Convert our parquet OrderbookFrame back to the strategy-facing OrderBook model."""
    return OrderBook(
        inst_id=frame.inst_id,
        ts_ms=frame.ts_ms,
        bids=[OrderBookLevel(price=p, size=s) for p, s in frame.bids],
        asks=[OrderBookLevel(price=p, size=s) for p, s in frame.asks],
    )


def _replay_books_for_bar(
    strategy, stream: OrderbookReplayStream, *, bar_ts_ms_exclusive: int,
) -> None:
    """Fire ``process_orderbook(book)`` for every frame with ts_ms < ``bar_ts_ms_exclusive``."""
    for frame in stream.drain_until(bar_ts_ms_exclusive):
        strategy.process_orderbook(_frame_to_orderbook(frame))


@dataclass(frozen=True, slots=True)
class OrderbookReplayConfig:
    inst_id: str
    catalog_path: Path
    bar_period: str = "1m"
    total_bars: int = 1500


def run_orderbook_replay_backtest(
    *, venue, data, strategies, orderbook_frames: list[OrderbookFrame],
) -> BacktestSummary:
    """Run a NT backtest with orderbook frames interleaved.

    After NT's BacktestNode is built but before it runs, we attach a per-bar
    hook to each strategy that drains the orderbook stream up to the bar
    timestamp.

    Implementation note: NT's Strategy class exposes ``on_bar`` as the
    primary callback. We monkey-patch each strategy's ``on_bar`` to first
    drain books, then call the original. Strategies created from
    ImportableStrategyConfig get patched after node.build().
    """
    summary, node = run_backtest_with_node(venue=venue, data=data, strategies=strategies)
    stream = OrderbookReplayStream(orderbook_frames)
    for engine in node.get_engines():
        for strategy in engine.trader.strategies():
            if not hasattr(strategy, "process_orderbook"):
                continue
            original_on_bar = strategy.on_bar

            def _patched_on_bar(bar, *, _orig=original_on_bar, _strat=strategy):
                ts_ms = int(bar.ts_event // 1_000_000)
                _replay_books_for_bar(_strat, stream, bar_ts_ms_exclusive=ts_ms)
                return _orig(bar)

            strategy.on_bar = _patched_on_bar  # type: ignore[assignment]
    return summary
```

> **Note:** NT may rebuild the node on each `run()` — if monkey-patch is lost, override the strategy class instead at registration time. See Task 5 for the working pattern.

- [ ] **Step 3: Run unit test, commit**

```bash
git add src/okx_trade/backtest/orderbook_runner.py tests/unit/backtest/test_orderbook_runner.py
git commit -m "feat(backtest): add orderbook-replay runner with per-bar book firing"
```

---

## Task 5: Register `ob_imbalance` in `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Add CLI args**

```python
    parser.add_argument("--orderbook-instrument-id", help="Inst id for ob_imbalance backtest")
```

- [ ] **Step 2: Implement `_run_ob_imbalance`**

```python
async def _run_ob_imbalance(args: argparse.Namespace) -> BacktestSummary:
    """Backtest ob_imbalance via captured books5 replay."""
    from okx_trade.backtest.data_loader import prepare_backtest_catalog
    from okx_trade.backtest.orderbook_data import read_orderbook_parquet
    from okx_trade.backtest.orderbook_runner import run_orderbook_replay_backtest
    from okx_trade.backtest.runner import build_okx_venue_config

    catalog_path = Path(args.catalog)
    inst_id = args.orderbook_instrument_id
    # Bars (1m) for triggering decisions
    async with OKXRestClient(OKXSettings()) as client:
        inst, _ = await prepare_backtest_catalog(
            client, inst_id, bar_period=args.signal_bar,
            total=args.total_bars, catalog_path=catalog_path, reuse=args.reuse_data,
        )
    # Orderbook frames (no network — must be pre-captured)
    frames = read_orderbook_parquet(inst_id, catalog_path=catalog_path)

    venue = build_okx_venue_config(starting_balance_usdt=args.equity, leverage=args.leverage)
    data = [BacktestDataConfig(
        catalog_path=str(catalog_path), data_cls=Bar.fully_qualified_name(),
        instrument_id=inst.id.value,
    )]
    strat_cfg = ImportableStrategyConfig(
        strategy_path="okx_trade.strategies.ob_imbalance:OBImbalanceStrategy",
        config_path="okx_trade.strategies.ob_imbalance:OBImbalanceConfig",
        config={
            "instrument_id": inst.id.value,
            "bar_type": f"{inst.id.value}-{_bar_spec(args.signal_bar)}-LAST-EXTERNAL",
            "subscribe_books5": False,  # critical: replay path
            "account_equity_usdt": args.equity,
        },
    )
    return run_orderbook_replay_backtest(
        venue=venue, data=data, strategies=[strat_cfg], orderbook_frames=frames,
    )
```

- [ ] **Step 3: Add to runner_map and SUPPORTED_STRATEGIES**

- [ ] **Step 4: Smoke test (requires prior capture)**

```bash
# Step A: capture 1 hour of orderbook (skip if you already have data)
timeout 3600 python scripts/capture_orderbook.py --inst-id BTC-USDT-SWAP --duration-hours 1
# Step B: backtest
python scripts/backtest.py --strategy ob_imbalance \
    --orderbook-instrument-id BTC-USDT-SWAP --signal-bar 1m \
    --total-bars 60 --reuse-data
```

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): wire ob_imbalance via orderbook replay runner"
```

---

## Task 6: Integration smoke test with synthetic fixture

**Files:**
- Create: `tests/integration/test_backtest_ob_imbalance.py`
- Create: `tests/integration/fixtures/books5_btc_swap.parquet` (synthetic, ~200 frames)

- [ ] **Step 1: Generate synthetic fixture**

Helper script (run once, commit the parquet):

```python
from pathlib import Path
import math
from okx_trade.backtest.orderbook_data import OrderbookFrame, write_orderbook_parquet

frames = []
base_ts = 1_700_000_000_000
for i in range(200):
    mid = 60_000 + 50 * math.sin(i / 20)
    spread = 0.5
    frames.append(OrderbookFrame(
        inst_id="BTC-USDT-SWAP",
        ts_ms=base_ts + i * 1_000,
        bids=[[mid - spread - 0.5 * j, 1.0 + (j % 3)] for j in range(5)],
        asks=[[mid + spread + 0.5 * j, 1.0 + (j % 3)] for j in range(5)],
    ))
write_orderbook_parquet(frames, catalog_path=Path("tests/integration/fixtures"))
```

- [ ] **Step 2: Integration test**

```python
"""End-to-end ob_imbalance backtest smoke (no network)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_ob_imbalance_backtest_runs_with_replay_stream(tmp_path):
    pytest.importorskip("nautilus_trader")
    from okx_trade.backtest.orderbook_data import read_orderbook_parquet
    frames = read_orderbook_parquet("BTC-USDT-SWAP", catalog_path=FIXTURE_DIR)
    assert len(frames) >= 100
    # Construct minimal NT bars + venue + run via run_orderbook_replay_backtest
    # Assert: process_orderbook was called > 0, summary.sharpe is finite.
```

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_backtest_ob_imbalance.py tests/integration/fixtures/books5_btc_swap.parquet
git commit -m "test(backtest): add ob_imbalance integration smoke + synthetic fixture"
```

---

## Task 7: Update strategy roadmap

- [ ] Update `docs/strategy_roadmap.md` ob_imbalance row to "backtestable (requires prior books5 capture)".
- [ ] Commit: `docs(roadmap): mark ob_imbalance as backtestable via capture-replay`.

---

## Self-Review Checklist

- [ ] All 7 tasks have testable acceptance.
- [ ] Capture script can run 60s and produce valid parquet.
- [ ] Replay runner correctly interleaves books before bars (verified by mock test).
- [ ] Backtest can replay ≥ 1 hour of captured data without exception.
- [ ] Roadmap row updated.

---

## Execution Handoff

**1. Subagent-Driven (recommended)** — `superpowers:subagent-driven-development`. Tasks 1–4 are mechanical; Tasks 5–6 benefit from subagent review since they touch the runner monkey-patch (a known fragile point).

**2. Inline Execution** — `superpowers:executing-plans`.
