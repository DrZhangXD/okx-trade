"""walk_forward_grade: roll a (train_window, test_window) split and grade each test slice.

For factor research we only need the OOS test slice — the factor itself doesn't have
trainable parameters in v1, so the "train" window is just the lookback that the factor
needs to warm up. Returns one FactorGrade per test window.
"""
from __future__ import annotations

import numpy as np

from .grade import FactorGrade, GradeThresholds, grade_factor
from .panel import FactorPanel


def walk_forward_grade(
    factor_id: str,
    panel: FactorPanel,
    *,
    horizon_bars: int,
    train_window: int,
    test_window: int,
    thresholds: GradeThresholds | None = None,
) -> list[FactorGrade]:
    """Rolling-OOS grades. Each window covers ``[start, start + train + test)``;
    the panel slice handed to ``grade_factor`` includes both train and test rows, but
    ``min_history_bars`` (from the factor's spec) ensures only the test segment
    contributes to IC since the warmup period yields NaN scores."""
    T = panel.t
    grades: list[FactorGrade] = []
    start = 0
    while start + train_window + test_window <= T:
        end = start + train_window + test_window
        sub = _slice_panel(panel, start, end)
        grades.append(grade_factor(
            factor_id, sub, horizon_bars=horizon_bars, thresholds=thresholds,
        ))
        start += test_window
    return grades


def _slice_panel(panel: FactorPanel, start: int, end: int) -> FactorPanel:
    def _maybe(arr: np.ndarray | None) -> np.ndarray | None:
        return None if arr is None else arr[start:end]

    return FactorPanel(
        inst_ids=panel.inst_ids,
        timestamps_ms=panel.timestamps_ms[start:end],
        close=panel.close[start:end],
        volume_usdt=panel.volume_usdt[start:end],
        funding_rate=_maybe(panel.funding_rate),
        open_interest=_maybe(panel.open_interest),
        basis_apr=_maybe(panel.basis_apr),
    )


__all__ = ["walk_forward_grade"]
