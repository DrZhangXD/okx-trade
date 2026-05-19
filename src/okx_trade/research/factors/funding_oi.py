"""Funding-rate + open-interest factors."""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor
from .momentum import _rolling_std, _trailing_return


@register_factor(
    id="funding_current", category="funding_oi",
    description="Current 8h funding rate (raw, low = expensive shorts → long signal)",
    direction="long_low", required_data=("funding_rate",),
    min_history_bars=1, rebalance_minutes=480,
)
def funding_current(panel: FactorPanel) -> np.ndarray:
    if panel.funding_rate is None:
        raise ValueError("funding_rate required for funding_current")
    return panel.funding_rate.copy()


@register_factor(
    id="funding_z_30d", category="funding_oi",
    description="Funding rate z-score over trailing 30 days (low z = long signal)",
    direction="long_low", required_data=("funding_rate",),
    min_history_bars=720, rebalance_minutes=480,
)
def funding_z_30d(panel: FactorPanel) -> np.ndarray:
    if panel.funding_rate is None:
        raise ValueError("funding_rate required for funding_z_30d")
    fr = panel.funding_rate
    T = fr.shape[0]
    out = np.full_like(fr, np.nan, dtype=float)
    window = 720
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = fr[t - window + 1 : t + 1]
        mu = np.nanmean(slice_, axis=0)
        sd = np.nanstd(slice_, axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(sd > 0, (fr[t] - mu) / sd, np.nan)
    return out


@register_factor(
    id="oi_change_1d", category="funding_oi",
    description="24h change in open interest, (oi_t / oi_{t-24h}) - 1",
    direction="long_high", required_data=("open_interest",),
    min_history_bars=24, rebalance_minutes=240,
)
def oi_change_1d(panel: FactorPanel) -> np.ndarray:
    if panel.open_interest is None:
        raise ValueError("open_interest required for oi_change_1d")
    return _trailing_return(panel.open_interest, 24)


@register_factor(
    id="oi_to_volume_ratio", category="funding_oi",
    description="OI divided by trailing 24h avg volume (high = sticky positioning)",
    direction="long_high", required_data=("open_interest", "volume_usdt"),
    min_history_bars=24, rebalance_minutes=240,
)
def oi_to_volume_ratio(panel: FactorPanel) -> np.ndarray:
    if panel.open_interest is None:
        raise ValueError("open_interest required for oi_to_volume_ratio")
    vol, oi = panel.volume_usdt, panel.open_interest
    T = vol.shape[0]
    out = np.full_like(vol, np.nan, dtype=float)
    if T < 24:
        return out
    for t in range(23, T):
        avg_vol = np.nanmean(vol[t - 23 : t + 1], axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(avg_vol > 0, oi[t] / avg_vol, np.nan)
    return out


__all__ = [
    "funding_current", "funding_z_30d",
    "oi_change_1d", "oi_to_volume_ratio",
]
