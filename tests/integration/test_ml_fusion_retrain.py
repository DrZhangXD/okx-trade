"""Integration test for ml_fusion: synthetic-panel training pipeline + CLI error paths.

Builds a real synthetic panel, runs the full train → fit_walk_forward → refit_on_full
pipeline, saves and reloads the pickled model. Also covers the backtest CLI's
not-found-model error path via subprocess.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def synthetic_panel():
    """Build a small but learnable bars+funding fixture for 3 instruments × 500 bars."""
    base_ts = 1_700_000_000_000
    rng = np.random.default_rng(11)
    bars: dict[str, list[tuple[int, float]]] = {}
    funding: dict[str, list[tuple[int, float]]] = {}
    for inst_idx, inst in enumerate(["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]):
        price = 60_000.0 + inst_idx * 100
        rows: list[tuple[int, float]] = []
        for t in range(500):
            # tiny drift + iid noise → labels not 50/50, model can learn
            price *= float(np.exp(0.0002 + 0.004 * rng.standard_normal()))
            rows.append((base_ts + t * 3_600_000, price))
        bars[inst] = rows
        funding[inst] = [
            (base_ts + j * 8 * 3_600_000, 0.0001 + 0.00003 * (j % 7))
            for j in range(60)
        ]
    return bars, funding


def test_train_pipeline_produces_loadable_model(tmp_path, synthetic_panel):
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion import load_model, save_model
    from okx_trade.strategies.ml_fusion_training import (
        build_training_panel, fit_walk_forward, refit_on_full, select_best_fold,
    )

    bars, funding = synthetic_panel
    X, y, names = build_training_panel(bars, funding, horizon_hours=4, warmup_bars=200)
    assert X.shape[0] > 100, "expected enough training rows after warmup"
    assert X.shape[1] == len(names)
    # FeatureRow schema = 10 columns (matches deployed MLFusionStrategy inference)
    assert len(names) == 10

    folds = fit_walk_forward(
        X, y, feature_names=names,
        train_size=200, test_size=50, seed=42,
    )
    assert len(folds) >= 1
    best = select_best_fold(folds)
    assert best.test_metrics.n_samples == 50

    model = refit_on_full(X, y, feature_names=names, seed=42)
    out = tmp_path / "ml_fusion_model.pkl"
    save_model(model, out)
    assert out.exists() and out.stat().st_size > 100

    loaded = load_model(out)
    assert loaded is not None
    pred = loaded.predict(X[:8])
    assert pred.shape == (8,)
    # predictions should be 0 or 1
    assert set(pred.tolist()) <= {0, 1}


def _run_backtest_cli(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "backtest.py"), *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT),
    )


def test_run_ml_fusion_errors_when_model_missing():
    res = _run_backtest_cli([
        "--strategy", "ml_fusion",
        "--instrument-ids", "BTC-USDT-SWAP,ETH-USDT-SWAP",
        "--ml-model-path", "/nonexistent/ml_fusion.pkl",
    ])
    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    assert "ml_fusion model not found" in combined, combined


def test_run_ml_fusion_errors_on_single_instrument():
    res = _run_backtest_cli([
        "--strategy", "ml_fusion",
        "--instrument-ids", "BTC-USDT-SWAP",  # only 1 inst
        "--ml-model-path", "/nonexistent/ml_fusion.pkl",
    ])
    assert res.returncode != 0
    combined = (res.stdout or "") + (res.stderr or "")
    # The "≥ 2 instruments" check happens BEFORE the model-not-found check
    assert "至少需要 2 个标的" in combined, combined
