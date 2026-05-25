"""Warm the factor research panel cache for a given factor_portfolio yaml.

Usage:
    python scripts/factor_panel_warm.py --yaml configs/factor_portfolio.yaml \\
        --total-bars 2000 --bar 1H
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import yaml as _yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.research.data import _bar_ms  # noqa: E402
from okx_trade.research.runtime import ensure_panel_cached  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Warm the factor research panel parquet cache.")
    p.add_argument("--yaml", required=True, help="factor_portfolio.yaml path")
    p.add_argument("--bar", default="1H")
    p.add_argument("--total-bars", type=int, default=2000)
    p.add_argument("--cache-dir", default="./data/research_panel")
    p.add_argument(
        "--include",
        default="close,volume_usdt,funding_rate,open_interest",
        help="comma-separated panel fields to include",
    )
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    cfg = _yaml.safe_load(Path(args.yaml).read_text()) or {}
    raw_inst_ids = cfg.get("instrument_ids") or []
    if not raw_inst_ids:
        raise SystemExit(f"[error] {args.yaml} has no instrument_ids")
    # Strip ".OKX" venue suffix if present (yaml uses ".OKX" but REST uses bare ids).
    inst_ids = [s.split(".")[0] for s in raw_inst_ids]

    bar_ms = _bar_ms(args.bar)
    if bar_ms <= 0:
        raise SystemExit(f"[error] unsupported --bar {args.bar!r}")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.total_bars) * bar_ms
    include = tuple(s.strip() for s in args.include.split(",") if s.strip())
    cache_dir = Path(args.cache_dir)

    async with OKXRestClient(OKXSettings()) as client:
        path = await ensure_panel_cached(
            rest_client=client, inst_ids=inst_ids, bar=args.bar,
            start_ms=start_ms, end_ms=end_ms, include=include,
            cache_dir=cache_dir,
        )
    size = path.stat().st_size
    print(f"[warm] panel cached at {path}  ({size:,} bytes)")


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
