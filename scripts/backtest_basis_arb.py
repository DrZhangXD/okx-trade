"""basis_arb cross-account margin simulator CLI (standalone, no NT).

Uses ``src/okx_trade/backtest/basis_arb_sim.py`` for account-isolation tail
risk that NT's default single MARGIN account hides.

Usage:
    python scripts/backtest_basis_arb.py \\
        --spot-inst BTC-USDT --futures-inst BTC-USDT-250627 \\
        --perp-inst BTC-USDT-SWAP --total-bars 1500 \\
        --equity 10000 --futures-margin-pct 0.5 --mmr 0.005
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.backtest.basis_arb_sim import run_basis_arb_sim  # noqa: E402
from okx_trade.backtest.data_loader import (  # noqa: E402
    prepare_backtest_catalog, prepare_funding_panel,
)
from okx_trade.strategies._signals import BasisArbParams  # noqa: E402


def _days_to_expiry(inst_id: str, ts_ms: int) -> float:
    """Parse YYMMDD suffix from an OKX dated future + return days until expiry.

    e.g. "BTC-USDT-250627" → 2025-06-27 UTC midnight. Returns 0.5 floor to
    avoid divide-by-zero in annualized_basis.
    """
    suffix = inst_id.split("-")[-1]
    if len(suffix) != 6 or not suffix.isdigit():
        raise SystemExit(
            f"--futures-inst {inst_id!r} expected YYMMDD-suffixed (e.g. BTC-USDT-250627)"
        )
    expiry = datetime.strptime("20" + suffix, "%Y%m%d").replace(tzinfo=timezone.utc)
    bar_dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return max(0.5, (expiry - bar_dt).total_seconds() / 86_400)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="basis_arb cross-account margin simulator (standalone)"
    )
    p.add_argument("--spot-inst", required=True, help="e.g. BTC-USDT")
    p.add_argument("--futures-inst", required=True,
                   help="dated future with YYMMDD suffix, e.g. BTC-USDT-250627")
    p.add_argument("--perp-inst", default=None,
                   help="optional perp inst id for funding context (e.g. BTC-USDT-SWAP)")
    p.add_argument("--bar", default="1H")
    p.add_argument("--total-bars", type=int, default=1500)
    p.add_argument("--funding-total", type=int, default=750)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--futures-margin-pct", type=float, default=0.5,
                   help="fraction of equity allocated to the futures sub-account")
    p.add_argument("--mmr", type=float, default=0.005,
                   help="maintenance margin ratio (default 0.5%% ~= OKX tier-1)")
    p.add_argument("--fee-bps", type=float, default=5.0)
    p.add_argument("--entry-apr", type=float, default=0.05)
    p.add_argument("--exit-apr", type=float, default=0.01)
    p.add_argument("--force-close-days", type=int, default=2,
                   help="exit when days-to-expiry < this (prevents settlement-day risk)")
    p.add_argument("--catalog", default="./data")
    p.add_argument("--reuse-data", action="store_true",
                   help="skip REST download if catalog already has bars")
    p.add_argument("--equity-csv", default=None,
                   help="optional path to write the (ts_ms, net_equity) curve as CSV")
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    # Validate YYMMDD suffix BEFORE any REST round-trip so malformed inst ids
    # fail fast without burning bandwidth.
    _suffix = args.futures_inst.split("-")[-1]
    if len(_suffix) != 6 or not _suffix.isdigit():
        raise SystemExit(
            f"--futures-inst {args.futures_inst!r} expected YYMMDD-suffixed "
            "(e.g. BTC-USDT-250627)"
        )

    catalog = Path(args.catalog).resolve()
    catalog.mkdir(parents=True, exist_ok=True)

    spot_nt_bars = []
    fut_nt_bars = []
    funding: dict[str, list[tuple[int, float]]] = {}
    async with OKXRestClient(OKXSettings()) as client:
        if not args.reuse_data:
            print(f"[1/3] downloading bars: {args.spot_inst} + {args.futures_inst} "
                  f"({args.total_bars} × {args.bar})")
            _, spot_nt_bars = await prepare_backtest_catalog(
                client, args.spot_inst, args.bar,
                total=args.total_bars, catalog_path=str(catalog),
            )
            _, fut_nt_bars = await prepare_backtest_catalog(
                client, args.futures_inst, args.bar,
                total=args.total_bars, catalog_path=str(catalog),
            )
        else:
            print("[1/3] reusing catalog (--reuse-data)")

        if args.perp_inst:
            panel = await prepare_funding_panel(
                client, args.perp_inst, total=args.funding_total,
                catalog_path=catalog, reuse_cache=args.reuse_data,
            )
            funding[args.perp_inst] = list(zip(panel.ts_ms, panel.rates, strict=True))
            print(f"        funding samples for {args.perp_inst}: {len(panel.ts_ms)}")

    if not spot_nt_bars or not fut_nt_bars:
        if args.reuse_data:
            raise SystemExit(
                "--reuse-data set but bars not in memory. Re-run without "
                "--reuse-data once to populate the catalog."
            )
        raise SystemExit("REST returned no bars; check --spot-inst / --futures-inst")

    spot_bars = [(int(b.ts_event // 1_000_000), float(b.close)) for b in spot_nt_bars]
    fut_bars = [(int(b.ts_event // 1_000_000), float(b.close)) for b in fut_nt_bars]
    n = min(len(spot_bars), len(fut_bars))
    spot_bars, fut_bars = spot_bars[-n:], fut_bars[-n:]
    dte = [_days_to_expiry(args.futures_inst, ts) for ts, _ in fut_bars]

    print(f"[2/3] running sim: {n} aligned bars, "
          f"starting equity ${args.equity:,.0f}, "
          f"futures sub-acct {args.futures_margin_pct:.0%}")
    result = run_basis_arb_sim(
        spot_bars=spot_bars, futures_bars=fut_bars, funding_panel=funding,
        days_to_expiry_per_bar=dte,
        params=BasisArbParams(
            entry_basis_apr=args.entry_apr,
            exit_basis_apr=args.exit_apr,
            force_close_days_before_expiry=args.force_close_days,
        ),
        starting_cash_usdt=args.equity,
        futures_margin_pct=args.futures_margin_pct,
        mmr=args.mmr,
        fee_bps=args.fee_bps,
    )

    print("[3/3] result:")
    print(f"  net final equity: ${result.net_final_equity:,.2f}  "
          f"(spot ${result.spot_final_equity:,.2f}, "
          f"futures ${result.futures_final_equity:,.2f})")
    print(f"  futures liquidated: {result.futures_liquidated}")
    print(f"  entries={result.n_entries}  exits={result.n_exits}  "
          f"funding cashflow=${result.total_funding_cashflow:,.2f}")
    if args.equity_csv:
        import csv
        out = Path(args.equity_csv).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts_ms", "net_equity"])
            w.writerows(result.equity_curve)
        print(f"  wrote equity curve → {out}")


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
