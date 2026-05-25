"""ml_fusion walk-forward training: pure functions, no strategy state.

Builds training panels from bars + funding history using the same
``build_feature_row()`` the deployed strategy calls at inference time
(so the trained model is directly consumable). Fits XGBoost per
walk-forward fold via ``walk_forward_splits``, selects the model with
the highest OOS accuracy, and offers ``refit_on_full`` to produce a
final production model trained on the entire panel.

Importantly, all xgboost imports are lazy so non-``[ml-fusion]`` installs
can still import this module (only fit_walk_forward / refit_on_full
actually need xgboost).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..backtest.walk_forward import (
    ClassificationMetrics,
    evaluate_binary,
    walk_forward_splits,
)
from ._features import FeatureRow
from .ml_fusion import build_feature_row


@dataclass(frozen=True, slots=True)
class TrainedModel:
    """Result of a single walk-forward fold."""

    model: Any  # xgboost XGBClassifier — kept opaque to avoid hard dep at import
    feature_names: list[str]
    fold_index: int
    train_metrics: ClassificationMetrics
    test_metrics: ClassificationMetrics


def _funding_history_up_to(
    funding: list[tuple[int, float]], ts_ms: int, window: int = 90,
) -> tuple[float, list[float]]:
    """Return ``(funding_current, history)`` for ``ts_ms``, restricted to ``window`` samples.

    ``funding_current`` is the most recent rate settled at or before ``ts_ms``;
    ``history`` is the prior ``window`` samples (excluding the current one).
    Returns ``(0.0, [])`` if no samples are available.
    """
    if not funding:
        return 0.0, []
    ts_list = [t for t, _ in funding]
    idx = bisect_right(ts_list, ts_ms) - 1
    if idx < 0:
        return 0.0, []
    current = funding[idx][1]
    history = [r for _, r in funding[max(0, idx - window):idx]]
    return current, history


def build_training_panel(
    bars_by_inst: dict[str, list[tuple[int, float]]],
    funding_by_inst: dict[str, list[tuple[int, float]]],
    *,
    horizon_hours: int = 4,
    warmup_bars: int = 50,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build a binary-classification (X, y) panel by replaying ``build_feature_row``.

    For each instrument and each bar t in ``[warmup_bars, len(bars) - horizon_hours)``:
      * compute features using closes[0..t] and the relevant funding history slice
      * label = 1 if ``closes[t + horizon_hours] > closes[t]`` else 0

    Returns:
        ``(X, y, feature_names)`` where ``X.shape == (N, len(feature_names))``
        and ``feature_names`` matches ``FeatureRow.feature_names()`` so the
        deployed strategy can consume the trained model.
    """
    feature_names = FeatureRow.feature_names()
    # Use BTC closes (if available) for the btc_corr_30d feature.
    btc_inst = next(
        (k for k in bars_by_inst if k.startswith("BTC-USDT-SWAP")), None
    )
    btc_full = [c for _, c in bars_by_inst[btc_inst]] if btc_inst else None

    X_rows: list[list[float]] = []
    y_rows: list[int] = []
    for inst_id, bars in bars_by_inst.items():
        if len(bars) < warmup_bars + horizon_hours + 1:
            continue
        ts_list = [t for t, _ in bars]
        closes_full = [c for _, c in bars]
        funding = funding_by_inst.get(inst_id, [])
        for i in range(warmup_bars, len(closes_full) - horizon_hours):
            closes_to_i = closes_full[: i + 1]
            btc_to_i = btc_full[: i + 1] if btc_full else None
            current, history = _funding_history_up_to(funding, ts_list[i])
            feat = build_feature_row(
                inst_id=inst_id,
                ts_ms=ts_list[i],
                closes=closes_to_i,
                btc_closes=btc_to_i,
                funding_current=current,
                funding_history=history,
            )
            X_rows.append(feat.as_list())
            y_rows.append(1 if closes_full[i + horizon_hours] > closes_full[i] else 0)

    if not X_rows:
        return (
            np.empty((0, len(feature_names)), dtype=np.float64),
            np.empty((0,), dtype=np.int64),
            feature_names,
        )
    return (
        np.asarray(X_rows, dtype=np.float64),
        np.asarray(y_rows, dtype=np.int64),
        feature_names,
    )


def fit_walk_forward(
    X: np.ndarray,
    y: np.ndarray,
    *,
    feature_names: list[str],
    train_size: int = 60 * 24,
    test_size: int = 7 * 24,
    seed: int = 42,
) -> list[TrainedModel]:
    """Fit one XGBoost classifier per walk-forward fold; return all of them."""
    import xgboost as xgb  # lazy

    folds: list[TrainedModel] = []
    splits = list(walk_forward_splits(len(X), train_size, test_size))
    for fi, (train_r, test_r) in enumerate(splits):
        X_train, y_train = X[list(train_r)], y[list(train_r)]
        X_test, y_test = X[list(test_r)], y[list(test_r)]
        model = xgb.XGBClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            random_state=seed,
            n_jobs=-1,
            eval_metric="logloss",
        )
        model.fit(X_train, y_train, verbose=False)
        y_train_pred = model.predict(X_train).astype(int).tolist()
        y_test_pred = model.predict(X_test).astype(int).tolist()
        folds.append(
            TrainedModel(
                model=model,
                feature_names=list(feature_names),
                fold_index=fi,
                train_metrics=evaluate_binary(y_train.tolist(), y_train_pred),
                test_metrics=evaluate_binary(y_test.tolist(), y_test_pred),
            )
        )
    return folds


def select_best_fold(folds: list[TrainedModel]) -> TrainedModel:
    """Pick the fold with the highest OOS accuracy. Raises ValueError if empty."""
    if not folds:
        raise ValueError("no folds to select from")
    return max(folds, key=lambda f: f.test_metrics.accuracy)


def refit_on_full(
    X: np.ndarray,
    y: np.ndarray,
    *,
    feature_names: list[str],  # noqa: ARG001 — kept for API symmetry
    seed: int = 42,
):
    """Train XGB on the entire panel; returns the fitted classifier."""
    import xgboost as xgb  # lazy

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        random_state=seed,
        n_jobs=-1,
        eval_metric="logloss",
    )
    model.fit(X, y, verbose=False)
    return model


__all__ = [
    "TrainedModel",
    "build_training_panel",
    "fit_walk_forward",
    "select_best_fold",
    "refit_on_full",
]
