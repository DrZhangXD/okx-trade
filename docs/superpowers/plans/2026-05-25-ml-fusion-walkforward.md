# Plan 4: ml_fusion Walk-Forward Training Pipeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a `scripts/ml_fusion_retrain.py` that materializes a trained XGBoost model file consumable by `MLFusionStrategy`, plus register `ml_fusion` in `scripts/backtest.py` so users can run `--strategy ml_fusion` end-to-end after a one-time training run.

**Architecture:** ml_fusion's strategy class already provides `load_model()`, `save_model()`, and `build_feature_row()` helpers. What's missing is the **training script** that: (1) downloads historical bars (+ funding from Plan 1) for the configured instrument set, (2) walks forward to build train/test splits via existing `walk_forward_splits()`, (3) fits XGBoost per fold, (4) picks the best fold by OOS accuracy/IC, (5) refits on the full window and pickles the model. Backtest registration is then mechanical.

**Tech Stack:** xgboost ≥ 2.0 (optional dep `[ml-fusion]`), numpy, existing `walk_forward.py`, `okx_trade.backtest.data_loader`, Plan 1's `prepare_funding_panel`.

**Dependencies:** This plan **depends on Plan 1** (funding panel) — feature row uses `funding_z` which requires historical funding rates.

**Roadmap reference:** [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md) — Plan 4.

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `src/okx_trade/strategies/ml_fusion_training.py` | Pure-function trainer: `build_training_panel`, `fit_walk_forward`, `select_best_model` | create |
| `scripts/ml_fusion_retrain.py` | CLI entry: download data → train → save pickle | create |
| `src/okx_trade/strategies/ml_fusion.py` | (optional) add `feed_funding_panel()` mirroring Plan 1's funding strategies (features need funding_z) | modify |
| `scripts/backtest.py` | Register `ml_fusion` + `_run_ml_fusion` helper | modify |
| `tests/unit/strategies/test_ml_fusion_training.py` | Unit tests for trainer pure functions | create |
| `tests/unit/strategies/test_ml_fusion_panel_injection.py` | Test `feed_funding_panel()` accepted | create |
| `tests/integration/test_ml_fusion_retrain.py` | End-to-end: synthetic panel → trained model → loadable | create |
| `pyproject.toml` | (no change — xgboost already in `[ml-fusion]` optional extra) | — |
| `docs/strategy_roadmap.md` | Mark ml_fusion as backtestable | modify |

---

## Conventions

Standard conventions per [2026-05-25-backtest-completeness-roadmap.md](2026-05-25-backtest-completeness-roadmap.md). All xgboost imports must be **lazy** (inside functions, not module-top) so non-`[ml-fusion]` installs don't import-error.

---

## Task 1: `build_training_panel` — convert bars + funding to (X, y) arrays

**Files:**
- Create: `src/okx_trade/strategies/ml_fusion_training.py`
- Create: `tests/unit/strategies/test_ml_fusion_training.py`

- [ ] **Step 1: Failing test for panel builder**

```python
"""Tests for ml_fusion walk-forward training pure functions."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.strategies.ml_fusion_training import build_training_panel


def test_build_training_panel_aligns_features_and_labels():
    """Given synthetic closes for 2 instruments × 200 bars, build (X, y) panel.

    Labels are next-4h direction (binary), so y has length N-4.
    """
    bars_by_inst = {
        "BTC-USDT-SWAP": [(1_700_000_000_000 + i * 3_600_000, 60_000 + i * 10) for i in range(200)],
        "ETH-USDT-SWAP": [(1_700_000_000_000 + i * 3_600_000, 3_000 + i * 0.5) for i in range(200)],
    }
    funding_by_inst = {
        "BTC-USDT-SWAP": [(1_700_000_000_000 + i * 8 * 3_600_000, 0.0001 + 0.00001 * (i % 3)) for i in range(30)],
        "ETH-USDT-SWAP": [(1_700_000_000_000 + i * 8 * 3_600_000, 0.0002) for i in range(30)],
    }
    X, y, feature_names = build_training_panel(
        bars_by_inst, funding_by_inst, horizon_hours=4, warmup_bars=50,
    )
    assert X.ndim == 2 and X.shape[0] == y.shape[0]
    assert X.shape[1] == len(feature_names)
    assert set(y.tolist()) <= {0, 1}  # binary labels
    assert "momentum_24h" in feature_names or any("momentum" in n for n in feature_names)
```

- [ ] **Step 2: Run, confirm fails (ImportError)**

- [ ] **Step 3: Implement**

```python
"""ml_fusion walk-forward training: pure functions, no strategy state.

Builds training panels from bars + funding history, fits XGBoost per
walk-forward fold, selects the model with best OOS accuracy, and refits
on the full training window for production.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..backtest.walk_forward import ClassificationMetrics, evaluate_binary, walk_forward_splits
from ._features import momentum, percentile_rank, realized_vol


@dataclass(frozen=True, slots=True)
class TrainedModel:
    """Result of a single walk-forward fold."""

    model: Any  # xgboost Booster — opaque to keep this dep-light
    feature_names: list[str]
    fold_index: int
    train_metrics: ClassificationMetrics
    test_metrics: ClassificationMetrics


def build_training_panel(
    bars_by_inst: dict[str, list[tuple[int, float]]],
    funding_by_inst: dict[str, list[tuple[int, float]]],
    *,
    horizon_hours: int = 4,
    warmup_bars: int = 50,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Build (X, y) panel for binary classification.

    Args:
        bars_by_inst: ``inst_id -> [(ts_ms, close), ...]`` sorted ascending.
        funding_by_inst: ``inst_id -> [(ts_ms, rate), ...]`` sorted ascending.
        horizon_hours: forward window for label (close_{t+H} > close_t → 1).
        warmup_bars: skip the first N bars per inst (insufficient features).

    Returns:
        ``(X, y, feature_names)``. X shape ``(N_total, F)``, y shape ``(N_total,)``.
    """
    feature_names = ["momentum_24h", "momentum_72h", "realized_vol_24h",
                     "percentile_rank_168h", "funding_z_30d"]
    X_rows: list[list[float]] = []
    y_rows: list[int] = []
    for inst_id, bars in bars_by_inst.items():
        closes = [c for _, c in bars]
        if len(closes) < warmup_bars + horizon_hours + 1:
            continue
        funding = funding_by_inst.get(inst_id, [])
        funding_rates = [r for _, r in funding]
        for i in range(warmup_bars, len(closes) - horizon_hours):
            window = closes[: i + 1]
            row = [
                momentum(window, 24) or 0.0,
                momentum(window, 72) or 0.0,
                realized_vol(window, 24) or 0.0,
                percentile_rank(window, 168) or 0.5,
                _funding_z(funding_rates, 30) if funding_rates else 0.0,
            ]
            label = 1 if closes[i + horizon_hours] > closes[i] else 0
            X_rows.append(row)
            y_rows.append(label)
    return np.array(X_rows, dtype=np.float64), np.array(y_rows, dtype=np.int64), feature_names


def _funding_z(rates: list[float], window: int) -> float:
    if len(rates) < window:
        return 0.0
    tail = np.array(rates[-window:])
    mean, std = float(tail.mean()), float(tail.std())
    if std == 0:
        return 0.0
    return float((tail[-1] - mean) / std)
```

- [ ] **Step 4: Run, commit**

```bash
git add src/okx_trade/strategies/ml_fusion_training.py tests/unit/strategies/test_ml_fusion_training.py
git commit -m "feat(ml_fusion): add build_training_panel for walk-forward training"
```

---

## Task 2: `fit_walk_forward` — wrap XGBoost over walk_forward_splits

**Files:**
- Modify: `src/okx_trade/strategies/ml_fusion_training.py`
- Modify: `tests/unit/strategies/test_ml_fusion_training.py`

- [ ] **Step 1: Failing test (small synthetic)**

```python
def test_fit_walk_forward_returns_one_model_per_fold():
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion_training import fit_walk_forward

    rng = np.random.default_rng(42)
    X = rng.standard_normal((300, 5))
    # y is biased on X[:, 0] so the model can actually learn
    y = (X[:, 0] + rng.standard_normal(300) * 0.5 > 0).astype(np.int64)
    feature_names = [f"f{i}" for i in range(5)]
    folds = fit_walk_forward(
        X, y, feature_names=feature_names,
        train_size=100, test_size=30, seed=42,
    )
    assert len(folds) >= 5  # (300 - 100) / 30 = ~6 folds
    assert all(f.test_metrics.accuracy > 0.55 for f in folds)  # learning happened
```

- [ ] **Step 2: Implement**

```python
def fit_walk_forward(
    X: np.ndarray, y: np.ndarray,
    *,
    feature_names: list[str],
    train_size: int = 60 * 24,  # 60 days * 24 hourly samples
    test_size: int = 7 * 24,
    seed: int = 42,
) -> list[TrainedModel]:
    """Fit one XGBoost classifier per walk-forward fold; return all."""
    import xgboost as xgb  # lazy

    folds: list[TrainedModel] = []
    splits = list(walk_forward_splits(len(X), train_size, test_size))
    for fi, (train_r, test_r) in enumerate(splits):
        X_train, y_train = X[list(train_r)], y[list(train_r)]
        X_test, y_test = X[list(test_r)], y[list(test_r)]
        model = xgb.XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            random_state=seed, n_jobs=-1, eval_metric="logloss",
        )
        model.fit(X_train, y_train, verbose=False)
        y_train_pred = model.predict(X_train)
        y_test_pred = model.predict(X_test)
        folds.append(TrainedModel(
            model=model,
            feature_names=feature_names,
            fold_index=fi,
            train_metrics=evaluate_binary(y_train.tolist(), y_train_pred.tolist()),
            test_metrics=evaluate_binary(y_test.tolist(), y_test_pred.tolist()),
        ))
    return folds
```

- [ ] **Step 3: Run, commit**

```bash
git add src/okx_trade/strategies/ml_fusion_training.py tests/unit/strategies/test_ml_fusion_training.py
git commit -m "feat(ml_fusion): add fit_walk_forward producing per-fold XGB models"
```

---

## Task 3: `select_best_model` + final refit

**Files:**
- Modify: `src/okx_trade/strategies/ml_fusion_training.py`

- [ ] **Step 1: Failing test**

```python
def test_select_best_model_picks_highest_oos_accuracy(monkeypatch):
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion_training import (
        TrainedModel, select_best_fold, refit_on_full,
    )
    from okx_trade.backtest.walk_forward import ClassificationMetrics

    folds = [
        TrainedModel(model="A", feature_names=[], fold_index=0,
                     train_metrics=ClassificationMetrics(0.7, 0.7, 0.7, 0.7, 100, 50, 50),
                     test_metrics=ClassificationMetrics(0.55, 0.55, 0.55, 0.55, 30, 15, 15)),
        TrainedModel(model="B", feature_names=[], fold_index=1,
                     train_metrics=ClassificationMetrics(0.65, 0.65, 0.65, 0.65, 100, 50, 50),
                     test_metrics=ClassificationMetrics(0.62, 0.62, 0.62, 0.62, 30, 15, 15)),
    ]
    best = select_best_fold(folds)
    assert best.model == "B"
```

- [ ] **Step 2: Implement**

```python
def select_best_fold(folds: list[TrainedModel]) -> TrainedModel:
    """Pick the fold with highest OOS accuracy. Raises if list is empty."""
    if not folds:
        raise ValueError("no folds to select from")
    return max(folds, key=lambda f: f.test_metrics.accuracy)


def refit_on_full(
    X: np.ndarray, y: np.ndarray, *,
    feature_names: list[str], seed: int = 42,
):
    """Train XGB on the entire panel — used as the final production model."""
    import xgboost as xgb
    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        random_state=seed, n_jobs=-1, eval_metric="logloss",
    )
    model.fit(X, y, verbose=False)
    return model
```

- [ ] **Step 3: Commit**

```bash
git add src/okx_trade/strategies/ml_fusion_training.py tests/unit/strategies/test_ml_fusion_training.py
git commit -m "feat(ml_fusion): add select_best_fold + refit_on_full helpers"
```

---

## Task 4: Add `feed_funding_panel()` to ml_fusion strategy (Plan 1 dependency)

**Files:**
- Modify: `src/okx_trade/strategies/ml_fusion.py`
- Create: `tests/unit/strategies/test_ml_fusion_panel_injection.py`

- [ ] **Step 1: Failing test mirrors Plan 1 Task 6 pattern**

- [ ] **Step 2: Implement same `_funding_source_kind` / `_funding_panels` pattern as funding_cross_section** — see Plan 1 Task 6 for the exact code template.

- [ ] **Step 3: Commit**

```bash
git add src/okx_trade/strategies/ml_fusion.py tests/unit/strategies/test_ml_fusion_panel_injection.py
git commit -m "feat(ml_fusion): add feed_funding_panel for backtest feature lookup"
```

---

## Task 5: `scripts/ml_fusion_retrain.py` — end-to-end CLI

**Files:**
- Create: `scripts/ml_fusion_retrain.py`

- [ ] **Step 1: Implement**

```python
"""ml_fusion walk-forward retrain CLI.

Usage:
    python scripts/ml_fusion_retrain.py \\
        --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \\
        --bar-period 1H --total-bars 2000 \\
        --funding-total 750 \\
        --train-window-hours 1440 --test-window-hours 168 \\
        --output-path var/ml_fusion_model.pkl \\
        --catalog ./data --reuse-data
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.backtest.data_loader import prepare_backtest_catalog, prepare_funding_panel  # noqa: E402
from okx_trade.strategies.ml_fusion import save_model  # noqa: E402
from okx_trade.strategies.ml_fusion_training import (  # noqa: E402
    build_training_panel, fit_walk_forward, refit_on_full, select_best_fold,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--instrument-ids", required=True)
    p.add_argument("--bar-period", default="1H")
    p.add_argument("--total-bars", type=int, default=2000)
    p.add_argument("--funding-total", type=int, default=750)
    p.add_argument("--train-window-hours", type=int, default=1440)
    p.add_argument("--test-window-hours", type=int, default=168)
    p.add_argument("--horizon-hours", type=int, default=4)
    p.add_argument("--output-path", default="var/ml_fusion_model.pkl")
    p.add_argument("--catalog", default="./data")
    p.add_argument("--reuse-data", action="store_true")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    inst_ids = args.instrument_ids.split(",")
    catalog = Path(args.catalog)
    bars_by_inst: dict[str, list[tuple[int, float]]] = {}
    funding_by_inst: dict[str, list[tuple[int, float]]] = {}
    async with OKXRestClient(OKXSettings()) as client:
        for inst_id in inst_ids:
            print(f"[data] {inst_id}: bars + funding")
            _, bars = await prepare_backtest_catalog(
                client, inst_id, bar_period=args.bar_period,
                total=args.total_bars, catalog_path=catalog, reuse=args.reuse_data,
            )
            bars_by_inst[inst_id] = [(int(b.ts_event // 1_000_000), float(b.close)) for b in bars]
            panel = await prepare_funding_panel(
                client, inst_id, total=args.funding_total,
                catalog_path=catalog, reuse_cache=args.reuse_data,
            )
            funding_by_inst[inst_id] = list(zip(panel.ts_ms, panel.rates, strict=True))

    print("[train] building feature panel...")
    X, y, feature_names = build_training_panel(
        bars_by_inst, funding_by_inst,
        horizon_hours=args.horizon_hours, warmup_bars=200,
    )
    print(f"[train] X.shape={X.shape}  y.shape={y.shape}  positive_rate={y.mean():.3f}")

    print("[train] walk-forward folds...")
    folds = fit_walk_forward(
        X, y, feature_names=feature_names,
        train_size=args.train_window_hours, test_size=args.test_window_hours,
        seed=args.seed,
    )
    for f in folds:
        print(f"  fold {f.fold_index}: train_acc={f.train_metrics.accuracy:.3f} "
              f"test_acc={f.test_metrics.accuracy:.3f} n={f.test_metrics.n_samples}")

    best = select_best_fold(folds)
    print(f"[select] best fold {best.fold_index}: oos_acc={best.test_metrics.accuracy:.3f}")

    print("[refit] training on full panel for production...")
    final_model = refit_on_full(X, y, feature_names=feature_names, seed=args.seed)
    out_path = Path(args.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_model(final_model, out_path)
    print(f"[save] model written to {out_path}")


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
```

- [ ] **Step 2: Smoke test (requires xgboost installed and network)**

```bash
pip install -e ".[ml-fusion]"
python scripts/ml_fusion_retrain.py \
    --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP \
    --bar-period 1H --total-bars 1000 --funding-total 300 \
    --train-window-hours 500 --test-window-hours 100 \
    --output-path /tmp/test_model.pkl --catalog ./data --reuse-data
ls -la /tmp/test_model.pkl  # should exist, > 1 KB
```

- [ ] **Step 3: Commit**

```bash
git add scripts/ml_fusion_retrain.py
git commit -m "feat(scripts): add ml_fusion walk-forward retrain CLI"
```

---

## Task 6: Register `ml_fusion` in `scripts/backtest.py`

**Files:**
- Modify: `scripts/backtest.py`

- [ ] **Step 1: Implement `_run_ml_fusion`**

```python
async def _run_ml_fusion(args: argparse.Namespace) -> BacktestSummary:
    """Backtest ml_fusion. Requires a pre-trained model — run
    scripts/ml_fusion_retrain.py first to produce var/ml_fusion_model.pkl.
    """
    from okx_trade.backtest.funding_data import FundingPanel
    model_path = Path(args.ml_model_path)
    if not model_path.exists():
        raise FileNotFoundError(
            f"no ml_fusion model at {model_path}. Run scripts/ml_fusion_retrain.py first."
        )

    inst_ids = args.instrument_ids.split(",")
    catalog_path = Path(args.catalog)
    panels: dict[str, FundingPanel] = {}
    data_configs = []
    async with OKXRestClient(OKXSettings()) as client:
        for inst_id in inst_ids:
            inst, _ = await prepare_backtest_catalog(
                client, inst_id, bar_period=args.signal_bar,
                total=args.total_bars, catalog_path=catalog_path, reuse=args.reuse_data,
            )
            panels[inst_id] = await prepare_funding_panel(
                client, inst_id, total=args.funding_total,
                catalog_path=catalog_path, reuse_cache=args.reuse_data,
            )
            data_configs.append(BacktestDataConfig(
                catalog_path=str(catalog_path), data_cls=Bar.fully_qualified_name(),
                instrument_id=inst.id.value,
            ))
    venue = build_okx_venue_config(starting_balance_usdt=args.equity, leverage=args.leverage)
    strat_cfg = ImportableStrategyConfig(
        strategy_path="okx_trade.strategies.ml_fusion:MLFusionStrategy",
        config_path="okx_trade.strategies.ml_fusion:MLFusionConfig",
        config={
            "instrument_ids": [f"{i}.OKX" for i in inst_ids],
            "bar_type_template": "{inst}-" + _bar_spec(args.signal_bar) + "-LAST-EXTERNAL",
            "model_path": str(model_path),
            "account_equity_usdt": args.equity,
        },
    )
    summary, node = run_backtest_with_node(venue=venue, data=data_configs, strategies=[strat_cfg])
    for engine in node.get_engines():
        for strategy in engine.trader.strategies():
            if hasattr(strategy, "feed_funding_panel"):
                strategy.feed_funding_panel(panels)
    return summary
```

- [ ] **Step 2: Add CLI args**

```python
    parser.add_argument("--ml-model-path", default="var/ml_fusion_model.pkl")
```

- [ ] **Step 3: Smoke test**

```bash
python scripts/backtest.py --strategy ml_fusion \
    --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP --signal-bar 1H --total-bars 1000 \
    --ml-model-path /tmp/test_model.pkl --reuse-data
```

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest.py
git commit -m "feat(backtest): wire ml_fusion (requires pre-trained model)"
```

---

## Task 7: Integration test

**Files:**
- Create: `tests/integration/test_ml_fusion_retrain.py`

- [ ] **Step 1: Test**

```python
"""End-to-end ml_fusion training + load (no network — synthetic panels)."""
from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.integration


def test_retrain_produces_loadable_model(tmp_path):
    pytest.importorskip("xgboost")
    from okx_trade.strategies.ml_fusion import load_model, save_model
    from okx_trade.strategies.ml_fusion_training import (
        build_training_panel, fit_walk_forward, refit_on_full,
    )

    bars = {
        f"INST{i}": [(1_700_000_000_000 + j * 3_600_000, 100 + i + j * 0.1) for j in range(500)]
        for i in range(3)
    }
    funding = {
        f"INST{i}": [(1_700_000_000_000 + j * 8 * 3_600_000, 0.0001 + 0.00001 * (j % 5))
                     for j in range(60)]
        for i in range(3)
    }
    X, y, names = build_training_panel(bars, funding, horizon_hours=4, warmup_bars=50)
    assert X.shape[0] > 100

    folds = fit_walk_forward(X, y, feature_names=names, train_size=200, test_size=50)
    assert len(folds) >= 1

    model = refit_on_full(X, y, feature_names=names)
    out = tmp_path / "model.pkl"
    save_model(model, out)
    loaded = load_model(out)
    assert loaded is not None
    pred = loaded.predict(X[:5])
    assert pred.shape == (5,)
```

- [ ] **Step 2: Commit**

```bash
git add tests/integration/test_ml_fusion_retrain.py
git commit -m "test(ml_fusion): integration smoke for retrain pipeline"
```

---

## Task 8: Update roadmap doc

- [ ] Mark `ml_fusion` as "backtestable (requires `[ml-fusion]` extra + one-time retrain)".
- [ ] Add a note in `README.md` workflow section: "Train ml_fusion model: `python scripts/ml_fusion_retrain.py ...`".
- [ ] Commit.

---

## Self-Review Checklist

- [ ] `pytest tests/unit/strategies/test_ml_fusion_training.py -v` passes with xgboost; skips cleanly without.
- [ ] Retrain CLI produces a loadable pickle.
- [ ] Backtest CLI completes when model exists; emits clear error when it doesn't.

---

## Execution Handoff

**1. Subagent-Driven (recommended)** — `superpowers:subagent-driven-development`. Tasks 1–3 (pure functions) one subagent each; Task 5 (CLI script) one subagent; Tasks 6–8 sequential.

**2. Inline Execution** — `superpowers:executing-plans`.
