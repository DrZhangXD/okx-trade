"""Option summary capture + replay storage.

Parquet schema (per underlying):
    ts_ms        int64   poll timestamp UTC ms
    inst_id      string  e.g. BTC-USD-251226-100000-P
    mark_price   float64
    mark_iv      float64
    delta        float64
    gamma        float64
    vega         float64
    theta        float64
    underlying   string  e.g. BTC-USD
    exp_time_ms  int64
    strike       float64
    option_type  string  "C" or "P"

Partition: ${catalog}/option_summary/<underlying>/<YYYYMMDD>.parquet
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute
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
    pa.field("ts_ms", pa.int64()),
    pa.field("inst_id", pa.string()),
    pa.field("mark_price", pa.float64()),
    pa.field("mark_iv", pa.float64()),
    pa.field("delta", pa.float64()),
    pa.field("gamma", pa.float64()),
    pa.field("vega", pa.float64()),
    pa.field("theta", pa.float64()),
    pa.field("underlying", pa.string()),
    pa.field("exp_time_ms", pa.int64()),
    pa.field("strike", pa.float64()),
    pa.field("option_type", pa.string()),
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


class OptionSummaryPanel:
    """In-memory lookup over captured option summary snapshots."""

    def __init__(self, snapshots: list[OptionSummarySnapshot]) -> None:
        by_inst: dict[str, list[OptionSummarySnapshot]] = defaultdict(list)
        for s in snapshots:
            by_inst[s.inst_id].append(s)
        self._by_inst: dict[str, list[OptionSummarySnapshot]] = {}
        for inst_id, snaps in by_inst.items():
            self._by_inst[inst_id] = sorted(snaps, key=lambda s: s.ts_ms)

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
