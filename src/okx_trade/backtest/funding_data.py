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
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..rest.client import OKXRestClient


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
    sorted_rates = sorted(rates, key=lambda r: r.funding_time)
    return FundingPanel(
        inst_id=inst_id,
        ts_ms=[r.funding_time for r in sorted_rates],
        rates=[float(r.funding_rate) for r in sorted_rates],
    )


from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute
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
    sort_idx = pyarrow.compute.sort_indices(merged, sort_keys=[("ts_ms", "ascending")])
    sorted_table = merged.take(sort_idx)
    return FundingPanel(
        inst_id=inst_id,
        ts_ms=sorted_table["ts_ms"].to_pylist(),
        rates=sorted_table["funding_rate"].to_pylist(),
    )
