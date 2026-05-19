"""Flow factors (spread proxy + taker buy ratio proxy).

v1 returns NaN because OKX candle/REST data does not expose bid-ask spread or signed
volume at bar resolution. P2 will add these via WS persistence; until then these factor
ids exist so the registry is complete but their grade always falls below the threshold.
"""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor


@register_factor(
    id="spread_avg_1d", category="flow",
    description="Trailing 24h avg bid-ask spread in bps (v1: NaN — needs WS persistence)",
    direction="long_low", required_data=("close",),
    min_history_bars=24, rebalance_minutes=240,
)
def spread_avg_1d(panel: FactorPanel) -> np.ndarray:
    return np.full_like(panel.close, np.nan, dtype=float)


@register_factor(
    id="taker_buy_ratio_1d", category="flow",
    description="Aggressor buy / total volume (v1: NaN — needs WS persistence)",
    direction="long_high", required_data=("volume_usdt",),
    min_history_bars=24, rebalance_minutes=240,
)
def taker_buy_ratio_1d(panel: FactorPanel) -> np.ndarray:
    return np.full_like(panel.volume_usdt, np.nan, dtype=float)


__all__ = ["spread_avg_1d", "taker_buy_ratio_1d"]
