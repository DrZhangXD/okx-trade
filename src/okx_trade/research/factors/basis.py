"""Basis (perp vs spot annualized) factors."""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor


@register_factor(
    id="basis_apr", category="basis",
    description="Perp vs spot annualized basis (high = contango → short perp)",
    direction="long_low", required_data=("basis_apr",),
    min_history_bars=1, rebalance_minutes=240,
)
def basis_apr(panel: FactorPanel) -> np.ndarray:
    assert panel.basis_apr is not None
    return panel.basis_apr.copy()


@register_factor(
    id="basis_z_30d", category="basis",
    description="basis_apr z-score over trailing 30 days",
    direction="long_low", required_data=("basis_apr",),
    min_history_bars=720, rebalance_minutes=240,
)
def basis_z_30d(panel: FactorPanel) -> np.ndarray:
    assert panel.basis_apr is not None
    ba = panel.basis_apr
    T = ba.shape[0]
    out = np.full_like(ba, np.nan, dtype=float)
    window = 720
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = ba[t - window + 1 : t + 1]
        mu = np.nanmean(slice_, axis=0)
        sd = np.nanstd(slice_, axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(sd > 0, (ba[t] - mu) / sd, np.nan)
    return out


__all__ = ["basis_apr", "basis_z_30d"]
