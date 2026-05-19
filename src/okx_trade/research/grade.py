"""Factor evaluation: cross-sectional IC + decay + turnover + L/S spread (net of fees)."""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .compute import compute_factor
from .panel import FactorPanel
from .registry import get_factor

_DECAY_HORIZONS = (1, 2, 4, 8, 16, 32)
_FEE_BPS_PER_LEG = 5.0  # OKX taker, round-trip = 2 legs


@dataclass(frozen=True, slots=True)
class GradeThresholds:
    ic_t_stat: float = 2.0
    ir: float = 0.3
    ic_positive_rate: float = 0.55
    net_after_fees: float = 0.0  # gross > 0 after fee deduction
    autocorr_1: float = 0.3


@dataclass(frozen=True, slots=True)
class FactorGrade:
    factor_id: str
    panel_start_ms: int
    panel_end_ms: int
    horizon_bars: int
    ic_mean: float
    ic_std: float
    ir: float
    ic_t_stat: float
    ic_positive_rate: float
    ic_decay: list[float]
    turnover_avg: float
    autocorr_1: float
    long_short_spread: float
    net_ls_spread_after_fees: float
    n_periods: int
    n_instruments: int
    verdict: str   # "pass" | "fail"
    graded_at_ms: int


def _spearman_row(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation; ignores NaN-paired entries. Returns 0.0 if degenerate."""
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 3:
        return 0.0
    ra = _rankdata(a[mask])
    rb = _rankdata(b[mask])
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    if denom == 0:
        return 0.0
    return float((ra * rb).sum() / denom)


def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average-rank tie handling (matches scipy.stats.rankdata default)."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(a) + 1)
    # Tie correction
    sa = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2.0
            ranks[order[i : j + 1]] = avg
        i = j + 1
    return ranks


def _apply_direction(values: np.ndarray, direction: str) -> np.ndarray:
    return -values if direction == "long_low" else values


def grade_factor(
    factor_id: str,
    panel: FactorPanel,
    *,
    horizon_bars: int,
    top_k: int = 5,
    thresholds: GradeThresholds | None = None,
) -> FactorGrade:
    """Compute IC / decay / turnover / L-S spread for one factor on one panel.

    The factor's ``direction`` (long_high vs long_low) is applied here: long_low
    factors are negated before IC so the metrics always read "high score = expected
    long".
    """
    thresholds = thresholds or GradeThresholds()
    spec = get_factor(factor_id)
    raw = compute_factor(factor_id, panel)
    scored = _apply_direction(raw, spec.direction)

    close = panel.close
    T, N = close.shape

    # Forward returns at evaluation horizon
    fwd_ret = np.full_like(close, np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        fwd_ret[:-horizon_bars] = close[horizon_bars:] / close[:-horizon_bars] - 1.0

    # IC per row from min_history_bars to T-horizon
    start = max(spec.min_history_bars, 1)
    ic_series: list[float] = []
    top_sets: list[set[int]] = []
    bot_sets: list[set[int]] = []
    ls_returns: list[float] = []
    for t in range(start, T - horizon_bars):
        s = scored[t]
        r = fwd_ret[t]
        if np.all(np.isnan(s)) or np.all(np.isnan(r)):
            continue
        ic = _spearman_row(s, r)
        ic_series.append(ic)
        # top/bot sets for turnover + L/S spread
        valid = np.where(~(np.isnan(s) | np.isnan(r)))[0]
        if len(valid) < 2 * top_k:
            top_sets.append(set()); bot_sets.append(set())
            ls_returns.append(0.0)
            continue
        order = valid[np.argsort(s[valid])]  # ascending
        bot = set(order[:top_k].tolist())
        top = set(order[-top_k:].tolist())
        top_sets.append(top); bot_sets.append(bot)
        ls = np.nanmean(r[list(top)]) - np.nanmean(r[list(bot)])
        ls_returns.append(float(ls))

    ic_arr = np.asarray(ic_series, dtype=float)
    n_periods = len(ic_arr)
    if n_periods == 0:
        return _empty_grade(factor_id, panel, horizon_bars)
    ic_mean = float(ic_arr.mean())
    ic_std = float(ic_arr.std(ddof=1)) if n_periods > 1 else 0.0
    if ic_std > 0:
        ir = ic_mean / ic_std
        ic_t_stat = ic_mean * np.sqrt(n_periods) / ic_std
    elif ic_mean > 0:
        # Perfect predictor: zero variance, positive mean → infinite IR/t-stat
        ir = float("inf")
        ic_t_stat = float("inf")
    else:
        ir = 0.0
        ic_t_stat = 0.0
    ic_positive_rate = float((ic_arr > 0).mean())

    # Decay
    decay = []
    for h in _DECAY_HORIZONS:
        if T - h <= start:
            decay.append(float("nan"))
            continue
        fwd_h = np.full_like(close, np.nan)
        fwd_h[:-h] = close[h:] / close[:-h] - 1.0
        ics = []
        for t in range(start, T - h):
            ics.append(_spearman_row(scored[t], fwd_h[t]))
        decay.append(float(np.mean(ics)) if ics else float("nan"))

    # Turnover (average per period; |S_t \ S_{t-1}| / k)
    if len(top_sets) >= 2 and top_k > 0:
        turns: list[float] = []
        for prev, cur in zip(top_sets[:-1], top_sets[1:]):
            if not cur:
                continue
            turns.append(len(cur - prev) / top_k)
        for prev, cur in zip(bot_sets[:-1], bot_sets[1:]):
            if not cur:
                continue
            turns.append(len(cur - prev) / top_k)
        turnover_avg = float(np.mean(turns)) if turns else 0.0
    else:
        turnover_avg = 0.0

    # Autocorr of factor scores (lag-1, cross-instrument-pooled, NaN-safe).
    # Measures signal persistence — how stable rankings are across periods.
    autocorr_1 = _pooled_autocorr1(scored)

    # PnL
    ls_arr = np.asarray(ls_returns, dtype=float)
    long_short_spread = float(np.nanmean(ls_arr)) if ls_arr.size else 0.0
    # Net: subtract fee × 2 legs × turnover per period
    fee_pct = (_FEE_BPS_PER_LEG * 2 / 10_000.0) * turnover_avg
    net = long_short_spread - fee_pct

    verdict = (
        "pass" if (
            ic_t_stat >= thresholds.ic_t_stat
            and ir >= thresholds.ir
            and ic_positive_rate >= thresholds.ic_positive_rate
            and net >= thresholds.net_after_fees
            and autocorr_1 >= thresholds.autocorr_1
        ) else "fail"
    )

    return FactorGrade(
        factor_id=factor_id,
        panel_start_ms=panel.timestamps_ms[0],
        panel_end_ms=panel.timestamps_ms[-1],
        horizon_bars=horizon_bars,
        ic_mean=ic_mean, ic_std=ic_std, ir=ir,
        ic_t_stat=ic_t_stat, ic_positive_rate=ic_positive_rate,
        ic_decay=decay,
        turnover_avg=turnover_avg, autocorr_1=autocorr_1,
        long_short_spread=long_short_spread,
        net_ls_spread_after_fees=net,
        n_periods=n_periods, n_instruments=N,
        verdict=verdict, graded_at_ms=int(time.time() * 1000),
    )


def _pooled_autocorr1(arr: np.ndarray) -> float:
    """Cross-section-pooled lag-1 autocorr; NaN-safe; returns 0.0 if degenerate."""
    if arr.shape[0] < 2:
        return 0.0
    a = arr[:-1].ravel()
    b = arr[1:].ravel()
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 3:
        return 0.0
    a = a[mask] - a[mask].mean()
    b = b[mask] - b[mask].mean()
    denom = np.sqrt((a ** 2).sum() * (b ** 2).sum())
    if denom == 0:
        return 0.0
    return float((a * b).sum() / denom)


def _empty_grade(factor_id: str, panel: FactorPanel, horizon_bars: int) -> FactorGrade:
    return FactorGrade(
        factor_id=factor_id,
        panel_start_ms=panel.timestamps_ms[0],
        panel_end_ms=panel.timestamps_ms[-1],
        horizon_bars=horizon_bars,
        ic_mean=0.0, ic_std=0.0, ir=0.0,
        ic_t_stat=0.0, ic_positive_rate=0.0,
        ic_decay=[float("nan")] * len(_DECAY_HORIZONS),
        turnover_avg=0.0, autocorr_1=0.0,
        long_short_spread=0.0, net_ls_spread_after_fees=0.0,
        n_periods=0, n_instruments=panel.n,
        verdict="fail", graded_at_ms=int(time.time() * 1000),
    )


__all__ = ["FactorGrade", "GradeThresholds", "grade_factor"]
