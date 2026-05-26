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


def outlier_check(*args, **kwargs):
    """Stub — implemented in P-Task 5."""
    raise NotImplementedError("P-Task 5")
