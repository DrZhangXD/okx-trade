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
    """Build a snapshot DTO from an OptionSummary record.

    OptionSummary has Greeks + mark_vol (IV) but no mark_price field.
    Strike and expiry are derived from inst_id parsing
    (OKX format: BTC-USD-YYMMDD-STRIKE-C/P).
    """
    parts = item.inst_id.split("-")
    strike = 0.0
    exp_time_ms = 0
    option_type = "?"
    if len(parts) >= 5:
        try:
            strike = float(parts[3])
        except ValueError:
            pass
        try:
            from datetime import datetime, timezone
            exp = datetime.strptime("20" + parts[2], "%Y%m%d").replace(tzinfo=timezone.utc)
            exp_time_ms = int(exp.timestamp() * 1000)
        except (ValueError, IndexError):
            pass
        option_type = parts[4] if parts[4] in ("C", "P") else "?"

    return OptionSummarySnapshot(
        ts_ms=ts_ms,
        inst_id=item.inst_id,
        mark_price=float(getattr(item, "fwd_px", 0) or 0),
        mark_iv=float(item.mark_vol or 0),
        delta=float(item.delta or 0),
        gamma=float(item.gamma or 0),
        vega=float(item.vega or 0),
        theta=float(item.theta or 0),
        underlying=underlying,
        exp_time_ms=exp_time_ms,
        strike=strike,
        option_type=option_type,
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
                buffer.extend(
                    _to_snapshot(it, ts_ms=ts_ms, underlying=args.underlying)
                    for it in items
                )
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
    p = argparse.ArgumentParser(
        description="Poll OKX option summary → parquet for backtest replay."
    )
    p.add_argument("--underlying", required=True,
                   help="OKX option underlying (e.g. BTC-USD)")
    p.add_argument("--interval-sec", type=int, default=60,
                   help="Poll interval in seconds (default: 60)")
    p.add_argument("--duration-hours", type=float, default=168,
                   help="Capture duration in hours (default: 168 = 1 week)")
    p.add_argument("--catalog", default="./data",
                   help="ParquetDataCatalog root path (default: ./data)")
    p.add_argument("--flush-rows", type=int, default=5_000,
                   help="Flush buffer to parquet every N rows (default: 5000)")
    asyncio.run(_main(p.parse_args()))
