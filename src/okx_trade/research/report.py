"""Markdown report rendering for FactorGrade."""
from __future__ import annotations

from datetime import datetime, timezone

from .grade import FactorGrade


_DECAY_HORIZONS = (1, 2, 4, 8, 16, 32)


def render_grade_report(g: FactorGrade) -> str:
    """Render a FactorGrade as a self-contained markdown report."""
    def _iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    period_days = round((g.panel_end_ms - g.panel_start_ms) / 86_400_000)
    decay_header = " | ".join(str(h) for h in _DECAY_HORIZONS)
    decay_row = " | ".join(f"{x:.4f}" for x in g.ic_decay)
    verdict_badge = "PASS" if g.verdict == "pass" else "FAIL"

    return f"""# Factor Grade: {g.factor_id}

- Period: {_iso(g.panel_start_ms)} → {_iso(g.panel_end_ms)} ({period_days}d, {g.n_periods} periods × {g.n_instruments} inst)
- Horizon: {g.horizon_bars} bars
- Graded at: {_iso(g.graded_at_ms)}

## IC

| metric | value |
|---|---|
| ic_mean | {g.ic_mean:.4f} |
| ic_std | {g.ic_std:.4f} |
| ir | {g.ir:.4f} |
| t_stat | {g.ic_t_stat:.4f} |
| positive_rate | {g.ic_positive_rate:.4f} |

## Decay (IC by horizon, bars)

| {decay_header} |
|{('|'.join(['---'] * len(_DECAY_HORIZONS)))}|
| {decay_row} |

## Long-Short Spread (top-K vs bot-K)

| metric | value |
|---|---|
| gross (per period) | {g.long_short_spread:.6f} |
| net after fees | {g.net_ls_spread_after_fees:.6f} |
| turnover (avg) | {g.turnover_avg:.4f} |
| autocorr (lag-1) | {g.autocorr_1:.4f} |

## Verdict: {verdict_badge}
"""


__all__ = ["render_grade_report"]
