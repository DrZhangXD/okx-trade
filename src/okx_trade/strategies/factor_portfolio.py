"""FactorPortfolioStrategy — generic factor synthesizer (linear weighted z-score).

Pure-function layer (NT-independent, used by tests + the NT Strategy class below).
The NT Strategy class is implemented in a follow-up section that imports NT lazily.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ..research.compute import compute_factor
from ..research.panel import FactorPanel
from ..research.registry import get_factor

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class FactorWeight:
    id: str
    weight: float


def cross_section_zscore(vals: np.ndarray) -> np.ndarray:
    """Per-row z-score across instruments. NaN-safe. Returns all-NaN if std=0."""
    mu = np.nanmean(vals)
    sd = np.nanstd(vals)
    if not np.isfinite(sd) or sd == 0:
        return np.full_like(vals, np.nan, dtype=float)
    return (vals - mu) / sd


def synthesize_score(
    panel: FactorPanel,
    weights: list[FactorWeight],
) -> tuple[np.ndarray, list[str]]:
    """Compute each factor's last-row values, z-score, weight, and sum.

    Direction handling: ``long_low`` factors are negated so high score → long.

    Returns:
        (score, missing_ids): score is shape (panel.n,); missing_ids lists weight
        entries whose factor id isn't registered (skipped gracefully).
    """
    accumulated = np.zeros(panel.n, dtype=float)
    used_weight = 0.0
    missing: list[str] = []
    for w in weights:
        try:
            spec = get_factor(w.id)
        except KeyError:
            missing.append(w.id)
            continue
        try:
            arr = compute_factor(w.id, panel)
        except ValueError:
            missing.append(w.id)
            continue
        last = arr[-1].astype(float)
        if spec.direction == "long_low":
            last = -last
        z = cross_section_zscore(last)
        # If a row is all-NaN, skip it (don't pollute accumulated)
        if np.all(np.isnan(z)):
            missing.append(w.id)
            continue
        # NaN entries pass through as 0 contribution; finite entries contribute
        contrib = np.where(np.isnan(z), 0.0, z * w.weight)
        accumulated = accumulated + contrib
        used_weight += w.weight
    if used_weight == 0:
        return np.full(panel.n, np.nan, dtype=float), missing
    return accumulated, missing


def select_top_bot(
    score: np.ndarray, *, top_k_long: int, top_k_short: int,
) -> tuple[list[int], list[int]]:
    """Pick top-K (long) and bot-K (short) indices, skipping NaN scores.

    Returns indices into the panel's ``inst_ids`` array. Longs sorted descending,
    shorts sorted ascending (most-negative first).
    """
    valid = np.where(np.isfinite(score))[0]
    if len(valid) == 0:
        return [], []
    order = valid[np.argsort(score[valid])]  # ascending
    longs = order[-top_k_long:][::-1].tolist() if top_k_long > 0 else []
    shorts = order[:top_k_short].tolist() if top_k_short > 0 else []
    return longs, shorts


__all__ = [
    "FactorWeight",
    "cross_section_zscore",
    "select_top_bot",
    "synthesize_score",
]
