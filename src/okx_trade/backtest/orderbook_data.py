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


