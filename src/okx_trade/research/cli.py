"""CLI for the factor research lab.

Entry: ``python -m okx_trade.research <subcommand>`` (via __main__.py).

Subcommands route to small helper functions; expensive ones (fetch / eval / backtest)
return non-zero and print a clear error if asyncio + REST credentials aren't available
in the current environment, keeping CLI tests cheap.
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
    pe.add_argument("--horizon", default="1d", help="1d/4h/1h")
    pe.add_argument("--top-k", type=int, default=5)
    pe.add_argument("--panel-cache", default=str(_DEFAULT_PANEL_DIR))
    pe.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    _common(pe)

    pa = sub.add_parser("grade-all")
    pa.add_argument("--horizon", default="1d")
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
    pb.add_argument("--start", required=True)
    pb.add_argument("--end", required=True)
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
    if cmd in ("fetch", "eval", "grade-all", "backtest-portfolio"):
        print(
            f"[error] '{cmd}' requires OKX REST + asyncio runtime;\n"
            f"        run it via the wrapper script: scripts/factor_research_smoke.sh\n"
            f"        or call okx_trade.research.cli helpers from a script with\n"
            f"        a constructed OKXRestClient.",
            file=sys.stderr,
        )
        return 2

    return 1


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
