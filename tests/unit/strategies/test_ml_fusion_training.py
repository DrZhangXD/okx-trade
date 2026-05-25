"""Unit tests for ml_fusion_training pure functions.

Tests gracefully skip when xgboost is not installed (it's an optional extra).
"""
from __future__ import annotations

import numpy as np
import pytest


def _synth_bars(inst_count: int = 2, T: int = 200) -> dict[str, list[tuple[int, float]]]:
    base_ts = 1_700_000_000_000
    out: dict[str, list[tuple[int, float]]] = {}
    rng = np.random.default_rng(7)
    for i in range(inst_count):
        # log-Brownian walk; drift slightly so labels are not 50/50 noise
        prices = 60_000.0
        bars: list[tuple[int, float]] = []
        for t in range(T):
            prices *= float(np.exp(0.0001 + 0.005 * rng.standard_normal()))
            bars.append((base_ts + t * 3_600_000, prices))
        out[f"BTC-USDT-SWAP" if i == 0 else f"ETH-USDT-SWAP"] = bars
    return out


def _synth_funding(insts: list[str], cycles: int = 30) -> dict[str, list[tuple[int, float]]]:
    base_ts = 1_700_000_000_000
    return {
        inst: [(base_ts + j * 8 * 3_600_000, 0.0001 + 0.00002 * (j % 3)) for j in range(cycles)]
        for inst in insts
    }


def test_build_training_panel_aligns_features_and_labels():
    from okx_trade.strategies._features import FeatureRow
    from okx_trade.strategies.ml_fusion_training import build_training_panel

    bars = _synth_bars(inst_count=2, T=200)
    funding = _synth_funding(list(bars.keys()), cycles=30)
    X, y, feature_names = build_training_panel(
        bars, funding, horizon_hours=4, warmup_bars=50,
    )
    assert X.ndim == 2
    assert X.shape[0] == y.shape[0]
    assert X.shape[1] == len(feature_names)
    # Training feature schema matches the deployed strategy's inference schema
    assert feature_names == FeatureRow.feature_names()
    # Binary labels only
    assert set(y.tolist()) <= {0, 1}
    # Sanity: 2 insts × (200 - 50 - 4) = 292 rows
    assert X.shape[0] == 2 * (200 - 50 - 4)


def test_build_training_panel_empty_when_bars_too_short():
    from okx_trade.strategies.ml_fusion_training import build_training_panel

    bars = {"BTC-USDT-SWAP": [(1_700_000_000_000 + i * 3_600_000, 60_000.0) for i in range(40)]}
    X, y, names = build_training_panel(bars, {}, warmup_bars=50, horizon_hours=4)
    assert X.shape == (0, len(names))
    assert y.shape == (0,)


def test_fit_walk_forward_returns_one_model_per_fold():
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion_training import fit_walk_forward

    rng = np.random.default_rng(42)
    X = rng.standard_normal((300, 5))
    # y biased on X[:, 0] so the model can learn
    y = (X[:, 0] + rng.standard_normal(300) * 0.5 > 0).astype(np.int64)
    feature_names = [f"f{i}" for i in range(5)]
    folds = fit_walk_forward(
        X, y, feature_names=feature_names,
        train_size=100, test_size=30, seed=42,
    )
    # (300 - 100) / 30 = 6.67 → ~6 folds
    assert len(folds) >= 5
    # learning happened
    assert all(f.test_metrics.accuracy > 0.55 for f in folds)


def test_select_best_fold_picks_highest_oos_accuracy():
    from okx_trade.backtest.walk_forward import ClassificationMetrics
    from okx_trade.strategies.ml_fusion_training import TrainedModel, select_best_fold

    folds = [
        TrainedModel(
            model="A", feature_names=[], fold_index=0,
            train_metrics=ClassificationMetrics(0.7, 0.7, 0.7, 0.7, 100, 50, 50),
            test_metrics=ClassificationMetrics(0.55, 0.55, 0.55, 0.55, 30, 15, 15),
        ),
        TrainedModel(
            model="B", feature_names=[], fold_index=1,
            train_metrics=ClassificationMetrics(0.65, 0.65, 0.65, 0.65, 100, 50, 50),
            test_metrics=ClassificationMetrics(0.62, 0.62, 0.62, 0.62, 30, 15, 15),
        ),
    ]
    best = select_best_fold(folds)
    assert best.model == "B"


def test_select_best_fold_raises_on_empty():
    from okx_trade.strategies.ml_fusion_training import select_best_fold
    with pytest.raises(ValueError, match="no folds"):
        select_best_fold([])


def test_refit_on_full_returns_loadable_model(tmp_path):
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion import load_model, save_model
    from okx_trade.strategies.ml_fusion_training import refit_on_full

    rng = np.random.default_rng(99)
    X = rng.standard_normal((150, 4))
    y = (X[:, 0] > 0).astype(np.int64)
    model = refit_on_full(X, y, feature_names=["a", "b", "c", "d"])
    out = tmp_path / "m.pkl"
    save_model(model, out)
    loaded = load_model(out)
    assert loaded is not None
    pred = loaded.predict(X[:5])
    assert pred.shape == (5,)
