"""CLI for the factor research lab.

Entry: ``python -m okx_trade.research <subcommand>`` (via __main__.py).

Offline subcommands (no REST):
    list / approve / reject / report

Online subcommands (build an OKXRestClient from env, async):
    fetch / eval / grade-all / backtest-portfolio

The online subcommands delegate to ``research.runtime`` which contains the asyncio
wrappers; cli.py stays focused on argparse + sqlite + yaml I/O.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Sequence

import yaml

from .registry import get_factor, list_factors
from .store import FactorStore

_DEFAULT_DB = Path("var/factor_research/factor_zoo.db")
_DEFAULT_YAML = Path("configs/factor_portfolio.yaml")
_DEFAULT_REPORT_DIR = Path("var/factor_research/reports")
_DEFAULT_PANEL_DIR = Path("var/factor_research/panel")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="okx_trade.research")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default=str(_DEFAULT_DB))
        sp.add_argument("--yaml", default=str(_DEFAULT_YAML))

    pl = sub.add_parser("list"); _common(pl)

    pf = sub.add_parser("fetch")
    pf.add_argument("--start", required=True, help="YYYY-MM-DD")
    pf.add_argument("--end", required=True)
    pf.add_argument("--universe", default="top30")
    pf.add_argument("--bar", default="1H")
    pf.add_argument("--cache-dir", default=str(_DEFAULT_PANEL_DIR))
    _common(pf)

    pe = sub.add_parser("eval")
    pe.add_argument("--factor", required=True)
    pe.add_argument("--start", required=True, help="YYYY-MM-DD")
    pe.add_argument("--end", required=True)
    pe.add_argument("--universe", default="top30")
    pe.add_argument("--bar", default="1H")
    pe.add_argument("--horizon", default="1d", help="1d/4h/1h")
    pe.add_argument("--top-k", type=int, default=5)
    pe.add_argument("--panel-cache", default=str(_DEFAULT_PANEL_DIR))
    pe.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    _common(pe)

    pa = sub.add_parser("grade-all")
    pa.add_argument("--start", required=True, help="YYYY-MM-DD")
    pa.add_argument("--end", required=True)
    pa.add_argument("--universe", default="top30")
    pa.add_argument("--bar", default="1H")
    pa.add_argument("--horizon", default="1d")
    pa.add_argument("--top-k", type=int, default=5)
    pa.add_argument("--panel-cache", default=str(_DEFAULT_PANEL_DIR))
    pa.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    _common(pa)

    pap = sub.add_parser("approve")
    pap.add_argument("--factor", required=True)
    pap.add_argument("--weight", type=float, required=True)
    pap.add_argument("--force", action="store_true",
                     help="approve even if latest grade verdict is fail")
    _common(pap)

    pr = sub.add_parser("reject")
    pr.add_argument("--factor", required=True)
    _common(pr)

    pb = sub.add_parser("backtest-portfolio")
    pb.add_argument("--bar", default="1H")
    pb.add_argument("--total-bars", type=int, default=2000,
                    help="Number of bars to download per instrument (default: 2000)")
    pb.add_argument("--catalog", default="var/backtest_catalog_factor_portfolio",
                    help="NT ParquetDataCatalog path")
    pb.add_argument("--taker-fee-bps", type=float, default=5.0)
    pb.add_argument("--maker-fee-bps", type=float, default=2.0)
    pb.add_argument("--warmup-days", type=int, default=0,
                    help="Pre-populate strategy buffers with N days of history before "
                         "the backtest window (so basis_z_30d / funding_z_30d are warm "
                         "from bar 1). 0 = cold start. 30 = match real-mode warmup.")
    pb.add_argument("--warmup-panel-dir", default=str(_DEFAULT_PANEL_DIR),
                    help="Where to save the fetched warmup panel parquet")
    _common(pb)

    prp = sub.add_parser("report")
    prp.add_argument("--factor", required=True)
    prp.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    _common(prp)

    return p


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd

    # Side-effect import: register all built-in factors
    from . import factors  # noqa: F401

    store = FactorStore(Path(args.db))
    store.init_schema()
    # Auto-upsert all registered factors so they show up in `list`
    for spec in list_factors():
        store.upsert_factor(
            id=spec.id, category=spec.category,
            direction=spec.direction, description=spec.description,
        )

    if cmd == "list":
        return _cmd_list(store)
    if cmd == "approve":
        return _cmd_approve(store, Path(args.yaml), args.factor, args.weight, args.force)
    if cmd == "reject":
        return _cmd_reject(store, Path(args.yaml), args.factor)
    if cmd == "report":
        return _cmd_report(store, Path(args.report_dir), args.factor)
    if cmd == "fetch":
        return _cmd_fetch_online(args)
    if cmd == "eval":
        return _cmd_eval_online(args, store)
    if cmd == "grade-all":
        return _cmd_grade_all_online(args, store)
    if cmd == "backtest-portfolio":
        return _cmd_backtest_portfolio_online(args)

    return 1


# ---------------------------------------------------------------------------
# Online subcommand wrappers (asyncio + REST)
# ---------------------------------------------------------------------------


def _cmd_fetch_online(args: argparse.Namespace) -> int:
    import asyncio

    from .. import OKXRestClient, OKXSettings
    from .runtime import cmd_fetch

    async def _run() -> int:
        async with OKXRestClient(OKXSettings()) as client:
            panel = await cmd_fetch(
                rest_client=client,
                start=args.start, end=args.end,
                universe=args.universe, bar=args.bar,
                cache_dir=Path(args.cache_dir),
            )
        print(
            f"fetched panel: {panel.t} bars × {panel.n} inst — cached under {args.cache_dir}"
        )
        return 0

    return asyncio.run(_run())


def _cmd_eval_online(args: argparse.Namespace, store: FactorStore) -> int:
    import asyncio

    from .. import OKXRestClient, OKXSettings
    from .runtime import cmd_eval

    async def _run() -> int:
        async with OKXRestClient(OKXSettings()) as client:
            grade, report_path = await cmd_eval(
                rest_client=client,
                factor_id=args.factor,
                start=args.start, end=args.end,
                universe=args.universe, bar=args.bar,
                horizon=args.horizon, top_k=args.top_k,
                panel_cache=Path(args.panel_cache),
                report_dir=Path(args.report_dir),
                store=store,
            )
        print(
            f"{grade.factor_id}: IC={grade.ic_mean:+.4f} IR={grade.ir:+.3f} "
            f"t={grade.ic_t_stat:+.2f} → {grade.verdict.upper()} "
            f"(report: {report_path})"
        )
        return 0

    return asyncio.run(_run())


def _cmd_grade_all_online(args: argparse.Namespace, store: FactorStore) -> int:
    import asyncio

    from .. import OKXRestClient, OKXSettings
    from .runtime import cmd_grade_all

    async def _run() -> int:
        async with OKXRestClient(OKXSettings()) as client:
            results = await cmd_grade_all(
                rest_client=client,
                start=args.start, end=args.end,
                universe=args.universe, bar=args.bar,
                horizon=args.horizon, top_k=args.top_k,
                panel_cache=Path(args.panel_cache),
                report_dir=Path(args.report_dir),
                store=store,
            )
        passed = sum(1 for g, _ in results if g.verdict == "pass")
        print(f"grade-all: {len(results)} factors evaluated, {passed} passed threshold")
        return 0

    return asyncio.run(_run())


def _cmd_backtest_portfolio_online(args: argparse.Namespace) -> int:
    import asyncio

    from .. import OKXRestClient, OKXSettings
    from .runtime import cmd_backtest_portfolio

    yaml_path = Path(args.yaml)
    if not yaml_path.exists():
        print(f"[error] yaml not found: {yaml_path}", file=sys.stderr)
        return 1
    yaml_cfg = yaml.safe_load(yaml_path.read_text()) or {}
    if not yaml_cfg.get("factor_weights"):
        print(
            f"[error] {yaml_path} has empty factor_weights; "
            "approve at least one factor first via `python -m okx_trade.research approve ...`",
            file=sys.stderr,
        )
        return 1

    async def _run() -> int:
        async with OKXRestClient(OKXSettings()) as client:
            summary = await cmd_backtest_portfolio(
                rest_client=client,
                yaml_cfg=yaml_cfg,
                bar=args.bar,
                total_bars=args.total_bars,
                catalog_path=Path(args.catalog),
                taker_fee_bps=args.taker_fee_bps,
                maker_fee_bps=args.maker_fee_bps,
                warmup_days=args.warmup_days,
                warmup_panel_dir=Path(args.warmup_panel_dir),
            )
        print("\n=== RESULT ===")
        for k, v in summary.items():
            if k.startswith("_"):
                continue
            print(f"  {k}: {v}")
        return 0

    return asyncio.run(_run())


def _cmd_list(store: FactorStore) -> int:
    rows = store.list_factors()
    for r in rows:
        latest = store.latest_grade(r["id"])
        ic = f"{latest['ic_mean']:.4f}" if latest else "—"
        ir = f"{latest['ir']:.4f}" if latest else "—"
        ver = latest["verdict"] if latest else "—"
        flag = "*" if r["approved"] else " "
        w = f" w={r['approved_weight']:.2f}" if r["approved"] else ""
        print(f"{flag} {r['id']:<28} {r['category']:<10} IC={ic} IR={ir} {ver}{w}")
    return 0


def _cmd_approve(store: FactorStore, yaml_path: Path, factor: str, weight: float,
                 force: bool) -> int:
    try:
        spec = get_factor(factor)
    except KeyError as exc:
        print(f"[error] {exc}", file=sys.stderr); return 1

    latest = store.latest_grade(factor)
    if not force and (latest is None or latest["verdict"] != "pass"):
        print(f"[error] factor {factor!r} has no passing grade; use --force to override",
              file=sys.stderr)
        return 1

    cfg = _load_yaml(yaml_path)
    weights_list = [pair for pair in cfg.get("factor_weights", []) if pair[0] != factor]
    weights_list.append([factor, weight])
    weights_list.sort(key=lambda pair: pair[0])
    cfg["factor_weights"] = weights_list
    _write_yaml(yaml_path, cfg)
    # sqlite update happens AFTER yaml write to avoid divergence (spec §15.4)
    store.approve(factor, weight=weight, ts_ms=int(time.time() * 1000))
    print(f"approved {factor} weight={weight} → {yaml_path}")
    return 0


def _cmd_reject(store: FactorStore, yaml_path: Path, factor: str) -> int:
    cfg = _load_yaml(yaml_path)
    cfg["factor_weights"] = [
        pair for pair in cfg.get("factor_weights", []) if pair[0] != factor
    ]
    _write_yaml(yaml_path, cfg)
    store.reject(factor)
    print(f"rejected {factor}")
    return 0


def _cmd_report(store: FactorStore, report_dir: Path, factor: str) -> int:
    from .report import render_grade_report
    from .grade import FactorGrade

    latest = store.latest_grade(factor)
    if latest is None:
        print(f"[error] no grade history for {factor!r}", file=sys.stderr); return 1
    # FactorGrade has ic_decay (list) but sqlite has scalars only — synthesize 6 NaN placeholders
    g = FactorGrade(
        factor_id=latest["factor_id"],
        panel_start_ms=latest["panel_start_ms"], panel_end_ms=latest["panel_end_ms"],
        horizon_bars=latest["horizon_bars"],
        ic_mean=latest["ic_mean"], ic_std=latest["ic_std"], ir=latest["ir"],
        ic_t_stat=latest["ic_t_stat"], ic_positive_rate=latest["ic_positive_rate"],
        ic_decay=[float("nan")] * 6,
        turnover_avg=latest["turnover_avg"], autocorr_1=latest["autocorr_1"],
        long_short_spread=latest["long_short_spread"],
        net_ls_spread_after_fees=latest["net_ls_spread_after_fees"],
        n_periods=latest["n_periods"], n_instruments=latest["n_instruments"],
        verdict=latest["verdict"], graded_at_ms=latest["graded_at_ms"],
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"{factor}_{time.strftime('%Y-%m-%d')}.md"
    out.write_text(render_grade_report(g))
    print(f"wrote {out}")
    return 0


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {"factor_weights": []}
    return yaml.safe_load(path.read_text()) or {"factor_weights": []}


def _write_yaml(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
