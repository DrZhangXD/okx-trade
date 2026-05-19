"""fetch_panel: concurrent REST pull + parquet cache → FactorPanel.

Caching: results are keyed by SHA1 of (sorted inst_ids + bar + start + end + include).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np

from .panel import FactorPanel, panel_from_dicts


class _RestClient(Protocol):
    market: object
    public: object


_FUNDING_BARS_NEEDED = lambda T: max(T // 8 + 5, 100)  # 1H bars → 8h funding cycles
_OI_PERIOD = "1H"


def _cache_key(inst_ids: Iterable[str], bar: str, start_ms: int, end_ms: int,
               include: tuple[str, ...]) -> str:
    payload = json.dumps({
        "inst_ids": sorted(inst_ids), "bar": bar,
        "start": start_ms, "end": end_ms, "include": sorted(include),
    }, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _load_cache(cache_path: Path) -> FactorPanel | None:
    if not cache_path.exists():
        return None
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return None
    table = pq.read_table(cache_path)
    meta = json.loads(table.schema.metadata[b"panel_meta"].decode("utf-8"))
    inst_ids = tuple(meta["inst_ids"])
    timestamps = tuple(meta["timestamps"])
    T, N = len(timestamps), len(inst_ids)
    arrays: dict[str, np.ndarray | None] = {}
    for name in ("close", "volume_usdt", "funding_rate", "open_interest", "basis_apr"):
        col = f"{name}_flat"
        if col in table.column_names:
            arrays[name] = np.asarray(table[col].to_pylist(), dtype=float).reshape(T, N)
        else:
            arrays[name] = None
    return FactorPanel(
        inst_ids=inst_ids, timestamps_ms=timestamps,
        close=arrays["close"], volume_usdt=arrays["volume_usdt"],
        funding_rate=arrays["funding_rate"],
        open_interest=arrays["open_interest"], basis_apr=arrays["basis_apr"],
    )


def _save_cache(panel: FactorPanel, cache_path: Path) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return
    cols: dict[str, list] = {}
    for name in ("close", "volume_usdt", "funding_rate", "open_interest", "basis_apr"):
        arr = getattr(panel, name)
        if arr is not None:
            cols[f"{name}_flat"] = arr.flatten().tolist()
    table = pa.table(cols)
    meta = {
        "inst_ids": list(panel.inst_ids),
        "timestamps": list(panel.timestamps_ms),
    }
    table = table.replace_schema_metadata({b"panel_meta": json.dumps(meta).encode("utf-8")})
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, cache_path)


async def fetch_panel(
    *,
    rest_client: _RestClient,
    inst_ids: list[str],
    start_ms: int,
    end_ms: int,
    bar: str = "1H",
    include: tuple[str, ...] = ("close", "volume_usdt"),
    cache_dir: Path | None = None,
) -> FactorPanel:
    """Build a FactorPanel from OKX REST.

    ``cache_dir``: if provided, results are cached as parquet keyed by query params.
    """
    if cache_dir is not None:
        cache_path = cache_dir / f"panel_{_cache_key(inst_ids, bar, start_ms, end_ms, include)}.parquet"
        cached = _load_cache(cache_path)
        if cached is not None:
            return cached

    # Determine bar count required (used as ``total`` cap for the OKX paginator)
    bar_ms = _bar_ms(bar)
    if bar_ms <= 0:
        raise ValueError(f"unsupported bar: {bar!r}")
    n_bars = max(1, (end_ms - start_ms) // bar_ms + 1)

    async def _fetch_one(inst: str) -> tuple[str, dict[str, list[tuple[int, float]]]]:
        fields: dict[str, list[tuple[int, float]]] = {}
        candles = await rest_client.market.get_candles_extended(inst, bar, total=n_bars)
        ts_close: list[tuple[int, float]] = []
        ts_volusdt: list[tuple[int, float]] = []
        for c in candles:
            if c.ts < start_ms or c.ts > end_ms:
                continue
            ts_close.append((c.ts, float(c.close)))
            ts_volusdt.append((c.ts, float(c.volume_ccy_quote)))
        fields["close"] = ts_close
        fields["volume_usdt"] = ts_volusdt

        if "funding_rate" in include:
            frs = await rest_client.public.get_funding_rate_history_extended(
                inst, total=_FUNDING_BARS_NEEDED(n_bars),
            )
            fr_series = [(r.funding_time, float(r.funding_rate)) for r in frs
                         if start_ms <= r.funding_time <= end_ms]
            # Forward-fill funding rate to each bar timestamp (8h cycle, 1H bars)
            fields["funding_rate"] = _ffill_to_bars(fr_series, ts_close)

        if "open_interest" in include:
            oi_pts = await rest_client.public.get_open_interest_history_extended(
                inst, period=_OI_PERIOD, total=n_bars,
            )
            oi_series = [(p.ts, float(p.oi_ccy)) for p in oi_pts
                         if start_ms <= p.ts <= end_ms]
            fields["open_interest"] = oi_series

        if "basis_apr" in include:
            spot_id = _spot_pair_for(inst)
            if spot_id is not None:
                spot_candles = await rest_client.market.get_candles_extended(
                    spot_id, bar, total=n_bars,
                )
                spot_close_by_ts: dict[int, float] = {
                    c.ts: float(c.close) for c in spot_candles
                    if start_ms <= c.ts <= end_ms
                }
                # basis = (perp_close - spot_close) / spot_close, raw premium fraction.
                # For perpetuals this isn't a true APR (no expiry) — it's the carry
                # premium which factor strategies typically normalize via z-score anyway.
                basis_series: list[tuple[int, float]] = []
                for ts, perp_px in ts_close:
                    spot_px = spot_close_by_ts.get(ts)
                    if spot_px is not None and spot_px > 0:
                        basis_series.append((ts, (perp_px - spot_px) / spot_px))
                fields["basis_apr"] = basis_series

        return inst, fields

    results = await asyncio.gather(*(_fetch_one(i) for i in inst_ids))
    by_inst = dict(results)
    panel = panel_from_dicts(by_inst)

    if cache_dir is not None:
        cache_path = cache_dir / f"panel_{_cache_key(inst_ids, bar, start_ms, end_ms, include)}.parquet"
        _save_cache(panel, cache_path)

    return panel


def _spot_pair_for(perp_inst_id: str) -> str | None:
    """Derive the SPOT instrument id from a SWAP id.

    ``BTC-USDT-SWAP`` → ``BTC-USDT``; ``ETH-USDC-SWAP`` → ``ETH-USDC``.
    Returns None for non-SWAP ids (no spot equivalent).
    """
    if not perp_inst_id.endswith("-SWAP"):
        return None
    return perp_inst_id[: -len("-SWAP")]


def _bar_ms(bar: str) -> int:
    s = bar.strip().upper()
    if s.endswith("M"):  # 1m, 5m, 15m → OKX uses lowercase m for minutes
        try:
            return int(s[:-1]) * 60_000
        except ValueError:
            return 0
    if s.endswith("H"):
        return int(s[:-1]) * 3_600_000
    if s.endswith("D"):
        return int(s[:-1]) * 86_400_000
    return 0


def _ffill_to_bars(
    sparse: list[tuple[int, float]],
    bar_ts: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """Forward-fill the latest known sparse value to each bar timestamp."""
    if not sparse or not bar_ts:
        return []
    sparse = sorted(sparse)
    out: list[tuple[int, float]] = []
    j = 0
    last_val: float | None = None
    for ts, _ in bar_ts:
        while j < len(sparse) and sparse[j][0] <= ts:
            last_val = sparse[j][1]
            j += 1
        if last_val is not None:
            out.append((ts, last_val))
    return out


__all__ = ["fetch_panel"]
