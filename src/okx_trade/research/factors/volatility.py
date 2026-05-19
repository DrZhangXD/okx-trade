"""Volatility factors."""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor
from .momentum import _rolling_std


def _log_returns(close: np.ndarray) -> np.ndarray:
    """Bar-to-bar log returns, NaN in row 0."""
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(close[1:] / close[:-1])
    return np.vstack([np.full((1, close.shape[1]), np.nan), lr])


@register_factor(
    id="rv_pct_365d", category="volatility",
    description="Trailing 30d RV as a percentile within trailing 365d RV history",
    direction="long_low", required_data=("close",),
    min_history_bars=365 * 24 + 30 * 24, rebalance_minutes=240,
)
def rv_pct_365d(panel: FactorPanel) -> np.ndarray:
    log_ret = _log_returns(panel.close)
    rv = _rolling_std(log_ret, 30 * 24)  # 30-day RV per bar
    T, N = rv.shape
    out = np.full_like(rv, np.nan, dtype=float)
    window = 365 * 24
    if T < window:
        return out
    for t in range(window - 1, T):
        history = rv[t - window + 1 : t + 1]
        current = rv[t]
        for col in range(N):
            col_hist = history[:, col]
            col_hist = col_hist[~np.isnan(col_hist)]
            if col_hist.size < 5 or np.isnan(current[col]):
                continue
            out[t, col] = float(np.sum(col_hist <= current[col]) / col_hist.size)
    return out


@register_factor(
    id="rv_skew_up_down", category="volatility",
    description="(up_day_rv - down_day_rv) / total_rv over trailing 30d",
    direction="long_high", required_data=("close",),
    min_history_bars=30 * 24, rebalance_minutes=240,
)
def rv_skew_up_down(panel: FactorPanel) -> np.ndarray:
    log_ret = _log_returns(panel.close)
    T, N = log_ret.shape
    out = np.full((T, N), np.nan, dtype=float)
    window = 30 * 24
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = log_ret[t - window + 1 : t + 1]
        up = np.where(slice_ > 0, slice_, np.nan)
        dn = np.where(slice_ < 0, slice_, np.nan)
        rv_up = np.sqrt(np.nanmean(up ** 2, axis=0))
        rv_dn = np.sqrt(np.nanmean(dn ** 2, axis=0))
        total = rv_up + rv_dn
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(total > 0, (rv_up - rv_dn) / total, np.nan)
    return out


@register_factor(
    id="vol_of_vol_30d", category="volatility",
    description="Stdev of daily RV over trailing 30 days",
    direction="long_low", required_data=("close",),
    min_history_bars=60 * 24, rebalance_minutes=240,
)
def vol_of_vol_30d(panel: FactorPanel) -> np.ndarray:
    log_ret = _log_returns(panel.close)
    # Daily RV: 24-bar rolling stdev
    daily_rv = _rolling_std(log_ret, 24)
    return _rolling_std(daily_rv, 30 * 24)


__all__ = ["rv_pct_365d", "rv_skew_up_down", "vol_of_vol_30d"]
