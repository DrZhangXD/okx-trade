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


def compute_edge_score(*args, **kwargs):
    """Stub — implemented in P-Task 4."""
    raise NotImplementedError("P-Task 4")


def outlier_check(*args, **kwargs):
    """Stub — implemented in P-Task 5."""
    raise NotImplementedError("P-Task 5")
