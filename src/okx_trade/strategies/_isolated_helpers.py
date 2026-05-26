"""Pure helper functions for FundingXS three-layer defense (2026-05-26).

All functions here are stateless and side-effect free — testable in isolation
without NT runtime, OKX REST, or strategy state.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def compute_leverage(
    edge_score: float,
    *,
    base: float,
    slope: float,
    lo: float,
    hi: float,
) -> float:
    """Map |edge_score| to leverage with linear ramp + clip.

    lever = clip(base + slope * |edge_score|, lo, hi)

    Convention: |edge_score| is in z-score units. With defaults (base=2,
    slope=3, hi=10), |edge|=1σ → 5x; 2σ → 8x; ≥2.67σ → 10x.
    """
    raw = base + slope * abs(edge_score)
    return float(max(lo, min(hi, raw)))


def _zscore(value: float, universe: Sequence[float]) -> float:
    """z-score of ``value`` against ``universe`` mean/std. Returns 0 if
    ``len(universe) < 2`` or std == 0 (no edge can be derived).
    """
    if len(universe) < 2:
        return 0.0
    arr = np.asarray(universe, dtype=float)
    std = float(arr.std(ddof=0))
    if std <= 0:
        return 0.0
    return (value - float(arr.mean())) / std


def compute_edge_score(
    *,
    funding_rate: float,
    funding_universe: Sequence[float],
    basis: float | None,
    basis_universe: Sequence[float] | None,
    direction: str,
    combine_basis: bool,
) -> float:
    """Compute per-leg edge score for leverage selection.

    funding_z  = z(funding_rate, funding_universe)
    basis_z    = z(basis,        basis_universe)  if combine_basis & basis is not None
    raw        = (funding_z + basis_z) / 2   if combine_basis else funding_z
    edge_score = sign(direction) × raw       # direction=short → +1, long → -1

    Convention: short direction wants positive funding/basis (we collect
    funding from longs); long direction wants negative. After ``sign``
    multiply, ``edge_score`` is positive when leg's direction agrees with
    the signal — and ``|edge_score|`` measures conviction.
    """
    funding_z = _zscore(funding_rate, funding_universe)
    if combine_basis and basis is not None and basis_universe is not None:
        basis_z = _zscore(basis, basis_universe)
        raw = (funding_z + basis_z) / 2.0
    else:
        raw = funding_z
    sign = 1.0 if direction == "short" else -1.0
    return float(sign * raw)


def outlier_check(
    *,
    closes: Sequence[float],
    window: int,
    baseline: int,
    warmup: int,
    ratio_threshold: float,
) -> tuple[bool, str]:
    """Decide whether to allow a new leg given recent realized vol.

    Returns ``(allow, reason)``:
      - ``(True, "warmup")``   — not enough history (< ``warmup`` bars).
      - ``(True, "no_baseline")`` — flat baseline (std==0); no filter possible.
      - ``(True, "ok")``       — recent vol within ``ratio_threshold`` of baseline.
      - ``(False, "vol_ratio=R>T")`` — recent vol > baseline × threshold; reject.

    Assumes ``closes`` are 1-minute bar closes for the instrument; both
    ``window`` and ``baseline`` are in bars (= minutes). Default config gives
    window=60 (last 1h), baseline=1440 (last 24h), warmup=1440.

    Caller contract: feed this from a dedicated 1m close buffer (today the
    shared ``VolatilityFilter`` service owns that buffer; strategies feed it
    via ``feed_bar`` and consult via ``vol_filter_allow``). Feeding a 1D
    buffer here will pin the result at ``(True, "warmup")`` because daily
    caches are typically sized < warmup, defeating the guard.
    """
    if len(closes) < warmup:
        return True, "warmup"
    arr = np.asarray(closes, dtype=float)
    log_returns = np.diff(np.log(arr))
    if len(log_returns) < max(window, baseline):
        return True, "warmup"
    recent_vol = float(np.std(log_returns[-window:], ddof=0))
    baseline_vol = float(np.std(log_returns[-baseline:], ddof=0))
    if baseline_vol <= 0:
        return True, "no_baseline"
    ratio = recent_vol / baseline_vol
    if ratio > ratio_threshold:
        return False, f"vol_ratio={ratio:.2f}>{ratio_threshold}"
    return True, "ok"
