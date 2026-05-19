"""Momentum factors: 1d/3d/7d price momentum + risk-adjusted variant.

Panel frequency assumed 1H bars (24 bars = 1 day). Insufficient history → NaN.
"""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor


def _trailing_return(close: np.ndarray, lookback: int) -> np.ndarray:
    """(close_t / close_{t-lookback}) - 1, NaN for t < lookback."""
    T, N = close.shape
    out = np.full_like(close, np.nan, dtype=float)
    if T > lookback:
        ref = close[:-lookback]
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = close[lookback:] / ref
        out[lookback:] = ratio - 1.0
    return out


def _rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
    """Population stdev over trailing ``window`` rows; NaN for rows < window-1."""
    T, N = arr.shape
    out = np.full_like(arr, np.nan, dtype=float)
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = arr[t - window + 1 : t + 1]
        out[t] = np.nanstd(slice_, axis=0)
    return out


@register_factor(
    id="momentum_1d", category="momentum",
    description="24h price momentum (close_t / close_{t-24h}) - 1",
    direction="long_high", required_data=("close",),
    min_history_bars=24, rebalance_minutes=240,
)
def momentum_1d(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 24)


@register_factor(
    id="momentum_3d", category="momentum",
    description="3-day price momentum",
    direction="long_high", required_data=("close",),
    min_history_bars=72, rebalance_minutes=240,
)
def momentum_3d(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 72)


@register_factor(
    id="momentum_7d", category="momentum",
    description="7-day price momentum",
    direction="long_high", required_data=("close",),
    min_history_bars=168, rebalance_minutes=240,
)
def momentum_7d(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 168)


@register_factor(
    id="momentum_risk_adj_7d", category="momentum",
    description="momentum_7d / realized_vol_30d (Sharpe-like)",
    direction="long_high", required_data=("close",),
    min_history_bars=720, rebalance_minutes=240,
)
def momentum_risk_adj_7d(panel: FactorPanel) -> np.ndarray:
    mom = _trailing_return(panel.close, 168)
    # log returns over 30d=720 bars, std as rv proxy
    with np.errstate(divide="ignore", invalid="ignore"):
        log_ret = np.log(panel.close[1:] / panel.close[:-1])
    log_ret = np.vstack([np.full((1, panel.n), np.nan), log_ret])
    rv = _rolling_std(log_ret, 720)
    out = np.full_like(mom, np.nan, dtype=float)
    mask = (rv > 0) & np.isfinite(rv) & np.isfinite(mom)
    out[mask] = mom[mask] / rv[mask]
    return out


@register_factor(
    id="momentum_1d_reversal", category="momentum",
    description="24h momentum but direction=long_low → short high-momentum / long low-momentum (mean reversion)",
    direction="long_low", required_data=("close",),
    min_history_bars=24, rebalance_minutes=240,
)
def momentum_1d_reversal(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 24)


@register_factor(
    id="momentum_7d_reversal", category="momentum",
    description="7d momentum reversal (long_low) — exploit short-term overextension",
    direction="long_low", required_data=("close",),
    min_history_bars=168, rebalance_minutes=240,
)
def momentum_7d_reversal(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 168)


__all__ = [
    "momentum_1d", "momentum_3d", "momentum_7d", "momentum_risk_adj_7d",
    "momentum_1d_reversal", "momentum_7d_reversal",
]
