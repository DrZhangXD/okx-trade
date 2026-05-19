"""FactorPanel: aligned (T, N) snapshot of close + funding + OI + basis for a set of instruments.

设计：所有时序数组 shape 严格 (T, N)；NaN 表示该 (t, inst) 缺数据。因子函数自己决定
nan-aware 还是 skip。inst_ids 顺序固定 = numpy 列顺序，与 yaml/sqlite 内的 id 列表一一对应。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class FactorPanel:
    inst_ids: tuple[str, ...]
    timestamps_ms: tuple[int, ...]
    close: np.ndarray
    volume_usdt: np.ndarray
    funding_rate: np.ndarray | None
    open_interest: np.ndarray | None
    basis_apr: np.ndarray | None

    def __post_init__(self) -> None:
        t, n = len(self.timestamps_ms), len(self.inst_ids)
        for name in ("close", "volume_usdt"):
            arr = getattr(self, name)
            if arr.shape != (t, n):
                raise ValueError(
                    f"{name} shape {arr.shape} != expected ({t}, {n})"
                )
        for name in ("funding_rate", "open_interest", "basis_apr"):
            arr = getattr(self, name)
            if arr is not None and arr.shape != (t, n):
                raise ValueError(
                    f"{name} shape {arr.shape} != expected ({t}, {n})"
                )

    @property
    def t(self) -> int:
        return len(self.timestamps_ms)

    @property
    def n(self) -> int:
        return len(self.inst_ids)


def panel_from_dicts(
    by_inst: dict[str, dict[str, list[tuple[int, float]]]],
) -> FactorPanel:
    """Build a FactorPanel from per-instrument per-field timeseries.

    ``by_inst[inst_id][field] = [(ts_ms, value), ...]`` ascending ts.
    All series across all instruments are outer-joined by ts; missing → NaN.

    Supported fields: ``close`` (required), ``volume_usdt`` (required),
    ``funding_rate``, ``open_interest``, ``basis_apr``.
    """
    inst_ids = tuple(sorted(by_inst.keys()))
    all_ts: set[int] = set()
    for fields in by_inst.values():
        for series in fields.values():
            all_ts.update(ts for ts, _ in series)
    timestamps = tuple(sorted(all_ts))
    ts_index = {ts: i for i, ts in enumerate(timestamps)}
    T, N = len(timestamps), len(inst_ids)

    def _alloc(field: str, *, required: bool) -> np.ndarray | None:
        any_present = any(field in by_inst[i] for i in inst_ids)
        if not required and not any_present:
            return None
        arr = np.full((T, N), np.nan, dtype=float)
        for col, inst in enumerate(inst_ids):
            series = by_inst[inst].get(field, [])
            for ts, val in series:
                arr[ts_index[ts], col] = val
        return arr

    close = _alloc("close", required=True)
    volume_usdt = _alloc("volume_usdt", required=True)
    funding_rate = _alloc("funding_rate", required=False)
    open_interest = _alloc("open_interest", required=False)
    basis_apr = _alloc("basis_apr", required=False)

    assert close is not None and volume_usdt is not None  # required=True
    return FactorPanel(
        inst_ids=inst_ids,
        timestamps_ms=timestamps,
        close=close,
        volume_usdt=volume_usdt,
        funding_rate=funding_rate,
        open_interest=open_interest,
        basis_apr=basis_apr,
    )


__all__ = ["FactorPanel", "panel_from_dicts"]
