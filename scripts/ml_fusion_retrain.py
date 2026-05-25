"""ml_fusion walk-forward retrain CLI.

Usage:
    python scripts/ml_fusion_retrain.py \\
        --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \\
        --bar-period 1H --total-bars 2000 --funding-total 750 \\
        --train-window-hours 1440 --test-window-hours 168 \\
        --output-path var/ml_fusion_model.pkl --catalog ./data

Requires the optional `[ml-fusion]` extra:
    pip install -e ".[ml-fusion]"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.backtest.data_loader import (  # noqa: E402
    prepare_backtest_catalog, prepare_funding_panel,
)
from okx_trade.strategies.ml_fusion import save_model  # noqa: E402
from okx_trade.strategies.ml_fusion_training import (  # noqa: E402
    build_training_panel, fit_walk_forward, refit_on_full, select_best_fold,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ml_fusion walk-forward retrain CLI")
    p.add_argument("--instrument-ids", required=True,
                   help="comma-separated OKX inst_ids (no venue suffix), e.g. "
                        "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP")
    p.add_argument("--bar-period", default="1H",
                   help="bar period for feature panel (default: 1H)")
    p.add_argument("--total-bars", type=int, default=2000,
                   help="bars per inst to download (default: 2000)")
    p.add_argument("--funding-total", type=int, default=750,
                   help="funding-rate history samples per inst (default: 750 ≈ 8 months)")
    p.add_argument("--train-window-hours", type=int, default=1440,
                   help="walk-forward train window size in bars (default: 1440 = 60 days × 24h)")
    p.add_argument("--test-window-hours", type=int, default=168,
                   help="walk-forward test window size in bars (default: 168 = 7 days × 24h)")
    p.add_argument("--horizon-hours", type=int, default=4,
                   help="forward horizon for binary label (default: 4h)")
    p.add_argument("--warmup-bars", type=int, default=200,
                   help="bars to skip at the start for feature warmup (default: 200)")
    p.add_argument("--output-path", default="var/ml_fusion_model.pkl",
                   help="output pickle path (default: var/ml_fusion_model.pkl)")
    p.add_argument("--catalog", default="./data",
                   help="NT catalog dir (default: ./data)")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    inst_ids = [s.strip() for s in args.instrument_ids.split(",") if s.strip()]
    if len(inst_ids) < 2:
        raise SystemExit("ml_fusion_retrain needs ≥ 2 instrument-ids")
    catalog = Path(args.catalog)
    catalog.mkdir(parents=True, exist_ok=True)

    bars_by_inst: dict[str, list[tuple[int, float]]] = {}
    funding_by_inst: dict[str, list[tuple[int, float]]] = {}

    async with OKXRestClient(OKXSettings()) as client:
        for inst_id in inst_ids:
            print(f"[data] {inst_id}: downloading bars + funding ...")
            _, bars = await prepare_backtest_catalog(
                client, inst_id, args.bar_period,
                total=args.total_bars, catalog_path=catalog,
            )
            bars_by_inst[inst_id] = [
                (int(b.ts_event // 1_000_000), float(b.close)) for b in bars
            ]
            panel = await prepare_funding_panel(
                client, inst_id, total=args.funding_total, catalog_path=catalog,
            )
            funding_by_inst[inst_id] = list(zip(panel.ts_ms, panel.rates, strict=True))
            print(f"        {len(bars_by_inst[inst_id])} bars  /  "
                  f"{len(funding_by_inst[inst_id])} funding samples")

    print("[train] building feature panel ...")
    X, y, feature_names = build_training_panel(
        bars_by_inst, funding_by_inst,
        horizon_hours=args.horizon_hours, warmup_bars=args.warmup_bars,
    )
    if X.shape[0] == 0:
        raise SystemExit(
            "training panel is empty — check --total-bars >= --warmup-bars + --horizon-hours + a few"
        )
    print(f"[train] X.shape={X.shape}  y.shape={y.shape}  "
          f"positive_rate={y.mean():.3f}  features={feature_names}")

    print("[train] walk-forward folds ...")
    folds = fit_walk_forward(
        X, y, feature_names=feature_names,
        train_size=args.train_window_hours,
        test_size=args.test_window_hours,
        seed=args.seed,
    )
    if not folds:
        raise SystemExit(
            f"no walk-forward folds produced — need at least "
            f"{args.train_window_hours + args.test_window_hours} samples; got {X.shape[0]}"
        )
    for f in folds:
        print(f"  fold {f.fold_index}: "
              f"train_acc={f.train_metrics.accuracy:.3f}  "
              f"test_acc={f.test_metrics.accuracy:.3f}  "
              f"n={f.test_metrics.n_samples}")

    best = select_best_fold(folds)
    print(f"[select] best fold {best.fold_index}: oos_acc={best.test_metrics.accuracy:.3f}")

    print("[refit] training final model on full panel ...")
    final_model = refit_on_full(X, y, feature_names=feature_names, seed=args.seed)
    out_path = Path(args.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_model(final_model, out_path)
    print(f"[save] model written to {out_path} ({out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
