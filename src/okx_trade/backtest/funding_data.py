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
