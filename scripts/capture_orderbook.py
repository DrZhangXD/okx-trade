"""Capture OKX books5 WS snapshots → parquet for backtest replay.

Usage:
    python scripts/capture_orderbook.py --inst-id BTC-USDT-SWAP \\
        --catalog ./data --downsample-sec 1 --duration-hours 168

Writes one parquet file per UTC day under
    ${catalog}/books5/<inst_id>/<YYYYMMDD>.parquet
Flushes every 60 seconds to limit data loss on crash.

WS API notes
------------
OKXWSClient.subscribe() is an async context manager that yields an async
iterator of raw WS frames.  The inner loop iterates over that stream.
OrderBook (from parse_okx_books5_frame) has:
  - .ts  : int  — millisecond timestamp (OKX field name is "ts")
  - .bids/.asks : list[OrderBookLevel] with .price/.size (Decimal)
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
from okx_trade.ws.client import OKXWSClient  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capture OKX books5 WS snapshots to parquet for backtest replay."
    )
    p.add_argument("--inst-id", required=True,
                   help="OKX instrument ID, e.g. BTC-USDT-SWAP")
    p.add_argument("--catalog", default="./data",
                   help="Catalog root directory (default: ./data)")
    p.add_argument("--downsample-sec", type=int, default=1,
                   help="Keep at most 1 frame per N seconds (default 1)")
    p.add_argument("--duration-hours", type=float, default=168,
                   help="Total capture duration in hours (default 168 = 7 days)")
    p.add_argument("--flush-sec", type=int, default=60,
                   help="Flush buffer to parquet every N seconds (default 60)")
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    catalog = Path(args.catalog)
    settings = OKXSettings()
    stop_at = time.time() + args.duration_hours * 3600

    buffer: deque[OrderbookFrame] = deque()
    last_kept_ms: int = 0
    last_flush = time.time()

    stop_event = asyncio.Event()

    def _on_signal(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_signal)

    print(
        f"Starting capture: inst_id={args.inst_id} "
        f"downsample={args.downsample_sec}s "
        f"duration={args.duration_hours}h "
        f"catalog={catalog}",
        flush=True,
    )

    # OKXWSClient.subscribe() is an @asynccontextmanager that yields an
    # async iterator of raw WS frames (dict[str, Any]).
    async with OKXWSClient(settings) as ws:
        async with ws.subscribe("books5", instId=args.inst_id) as stream:
            async for frame in stream:
                if stop_event.is_set() or time.time() >= stop_at:
                    break

                # parse_okx_books5_frame returns OrderBook or None
                book = parse_okx_books5_frame(frame, args.inst_id)
                if book is None:
                    continue

                # OrderBook.ts is the millisecond timestamp (OKX field name)
                ts_ms = int(book.ts)

                # Downsample: skip frames arriving faster than downsample_sec
                if ts_ms - last_kept_ms < args.downsample_sec * 1000:
                    continue
                last_kept_ms = ts_ms

                # OrderBookLevel.price/.size are Decimal — convert to float
                buffer.append(OrderbookFrame(
                    inst_id=args.inst_id,
                    ts_ms=ts_ms,
                    bids=[[float(b.price), float(b.size)] for b in book.bids],
                    asks=[[float(a.price), float(a.size)] for a in book.asks],
                ))

                now = time.time()
                if now - last_flush >= args.flush_sec:
                    _flush(buffer, catalog)
                    last_flush = now

    # Final flush — ensure no buffered data is lost on clean exit or signal
    if buffer:
        _flush(buffer, catalog)
        print(f"final flush: {len(buffer)} frames", flush=True)


def _flush(buffer: deque[OrderbookFrame], catalog: Path) -> None:
    """Write buffered frames to parquet and clear the buffer."""
    if not buffer:
        return
    frames = list(buffer)
    written = write_orderbook_parquet(frames, catalog_path=catalog)
    print(
        f"flushed {len(frames)} frames → {[str(p) for p in written]} "
        f"at {time.strftime('%H:%M:%S')}",
        flush=True,
    )
    buffer.clear()


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
