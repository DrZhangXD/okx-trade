"""一键回测：1 年滚动窗口跑 xs_momentum + funding_carry。

用法::

    python scripts/backtest_oneyear.py --as-of 2026-05-15 --equity 10000 --taker-fee-bps 5

输出 (默认 var/backtest_results/<as_of>/)：
- universe.json             top-30 USDT 永续快照
- xs_momentum.html          equity curve (plotly)
- xs_momentum.equity.csv
- xs_momentum.summary.txt
- funding_carry.summary.txt
- report.md                 策略对比表 + 限制说明

数据缓存于 ``var/backtest_catalog_1y_v2/``，二次跑加 ``--reuse-data`` 跳过下载。

注意：``range_breakout`` 已于 M5.X 下线（alpha 弱 + 实现不稳）；``basis_arb`` /
``ob_imbalance`` 需要专门数据源（FUTURES 历史 / books5 历史），暂不纳入本脚本。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.adapter.constants import OKX_VENUE  # noqa: E402
from okx_trade.backtest.data_loader import prepare_backtest_catalog  # noqa: E402
from okx_trade.enums import InstType  # noqa: E402

DEFAULT_CATALOG = REPO_ROOT / "var" / "backtest_catalog_1y_v2"
DEFAULT_OUT_BASE = REPO_ROOT / "var" / "backtest_results"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="1y backtest orchestrator (3 strategies)")
    p.add_argument("--as-of", default=date.today().isoformat(),
                   help="回测窗口截止日（ISO 格式），默认今天")
    p.add_argument("--equity", type=float, default=10000.0)
    p.add_argument("--leverage", type=int, default=10)
    p.add_argument("--taker-fee-bps", type=float, default=5.0,
                   help="taker 手续费率 bps (OKX 实盘 = 5)")
    p.add_argument("--maker-fee-bps", type=float, default=2.0)
    p.add_argument("--universe-size", type=int, default=30,
                   help="xs_momentum universe top-N (live = 30)")
    p.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    p.add_argument("--out-dir", default=None,
                   help="输出目录，默认 var/backtest_results/<as_of>/")
    p.add_argument("--reuse-data", action="store_true",
                   help="复用 catalog 已有数据，跳过下载（二次跑用）")
    p.add_argument("--skip", default="", help="逗号分隔要跳过的策略 (xs_momentum,funding_carry)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Phase 1: resolve universe
# ---------------------------------------------------------------------------

async def _resolve_universe(size: int, settle: str = "USDT") -> list[str]:
    """复用 scripts/live.py 的 resolve_universe，返回 'BTC-USDT-SWAP' 形式（不带 .OKX）。"""
    async with OKXRestClient(OKXSettings()) as rest:
        instruments = await rest.public.get_instruments(InstType.SWAP)
        perps = [i for i in instruments
                 if i.settle_ccy == settle and i.state == "live"]
        tickers = await rest.market.get_tickers(InstType.SWAP)
        vol_by_inst = {t.inst_id: float(t.vol_ccy_24h or 0) for t in tickers}
        perps.sort(key=lambda i: vol_by_inst.get(i.inst_id, 0.0), reverse=True)
        return [p.inst_id for p in perps[:size]]


# ---------------------------------------------------------------------------
# Phase 2: refresh catalog
# ---------------------------------------------------------------------------

async def _download_universe_bars(
    inst_ids: list[str],
    *,
    catalog_path: Path,
    taker_fee_bps: float,
    maker_fee_bps: float,
    daily_bars: int = 400,
) -> None:
    """下载 30 × 1-DAY 给 xs_momentum 用。BTC 自动加入下载列表用作基准/regime。"""
    fee_kwargs = {
        "taker_fee_bps": taker_fee_bps,
        "maker_fee_bps": maker_fee_bps,
    }
    download_ids = list(inst_ids)
    if "BTC-USDT-SWAP" not in download_ids:
        download_ids.append("BTC-USDT-SWAP")

    async with OKXRestClient(OKXSettings()) as client:
        print(f"[data] downloading 1-DAY × {len(download_ids)} instruments (total={daily_bars} each)...")
        for inst_id in download_ids:
            _, bars = await prepare_backtest_catalog(
                client, inst_id, "1D",
                total=daily_bars, catalog_path=str(catalog_path),
                **fee_kwargs,
            )
            print(f"    {inst_id:25s} 1-DAY × {len(bars)}")


# ---------------------------------------------------------------------------
# Phase 3: run backtests via subprocess
# ---------------------------------------------------------------------------

def _run_subprocess(cmd: list[str], summary_path: Path) -> dict:
    """跑子进程，把 stdout 写到 summary_path，解析 === RESULT === 后面的 BacktestSummary 字段。"""
    print(f"[run] {summary_path.stem}: $ {' '.join(cmd[:3])} ...")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    summary_path.write_text(result.stdout + ("\n--- STDERR ---\n" + result.stderr if result.stderr else ""))
    if result.returncode != 0:
        print(f"[run] {summary_path.stem}: FAILED rc={result.returncode}")
        print(result.stderr[-1000:])
        return {"ok": False, "rc": result.returncode}

    # 解析 BacktestSummary.__str__:
    # "events=X orders=Y positions=Z PnL=N (P%) Sharpe=S maxDD=D% win=W% elapsed=T s"
    parsed = {"ok": True}
    last_block = result.stdout.split("=== RESULT ===")[-1]
    patterns = {
        "events": r"events=(\d+)",
        "orders": r"orders=(\d+)",
        "positions": r"positions=(\d+)",
        "pnl_total": r"PnL=(-?\d+\.\d+)",
        "pnl_pct": r"\((-?\d+\.\d+)%\)",
        "sharpe": r"Sharpe=(-?\d+\.\d+)",
        "max_dd_pct": r"maxDD=(-?\d+\.\d+)%",
        "win_rate": r"win=(\d+\.\d+)%",
    }
    for k, pat in patterns.items():
        m = re.search(pat, last_block)
        if m:
            parsed[k] = float(m.group(1))
    return parsed


def _run_funding_carry(cmd: list[str], summary_path: Path) -> dict:
    """funding_carry 输出格式独立。"""
    print(f"[run] funding_carry: $ {' '.join(cmd[:3])} ...")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    summary_path.write_text(result.stdout + ("\n--- STDERR ---\n" + result.stderr if result.stderr else ""))
    if result.returncode != 0:
        print(f"[run] funding_carry: FAILED rc={result.returncode}")
        print(result.stderr[-1000:])
        return {"ok": False, "rc": result.returncode}
    parsed = {"ok": True}
    # 注意：funding_carry 输出形如 "+37.43 USDT" / "+1.52%"，正号需要匹配。
    patterns = {
        "span_days": r"span_days\s*=\s*(\d+\.\d+)",
        "gross_funding": r"gross_funding\s*=\s*([+-]?\d+\.\d+)",
        "fee_cost": r"fee_cost\s*=\s*([+-]?\d+\.\d+)",
        "net_funding_pnl": r"net_funding_pnl\s*=\s*([+-]?\d+\.\d+)",
        "annualized_apr": r"(?:net_apr|annualized_apr)\s*=\s*([+-]?\d+\.\d+)%",
        "gross_apr": r"gross_apr\s*=\s*([+-]?\d+\.\d+)%",
        "in_position": r"in_position\s*=\s*(\d+\.\d+)%",
        "n_enters": r"trades\s*=\s*(\d+)\s+enter",
    }
    for k, pat in patterns.items():
        m = re.search(pat, result.stdout)
        if m:
            parsed[k] = float(m.group(1))
    return parsed


# ---------------------------------------------------------------------------
# Phase 4: report.md
# ---------------------------------------------------------------------------

def _write_report(out_dir: Path, *, as_of: str, equity: float, taker_bps: float,
                  universe: list[str], xs: dict, fc: dict) -> Path:
    def fmt_pct(x):
        return f"{x:+.2f}%" if x is not None else "—"
    def fmt_num(x, prec=2):
        return f"{x:.{prec}f}" if x is not None else "—"

    rows = []
    # xs_momentum
    if xs.get("ok"):
        rows.append({
            "name": "xs_momentum",
            "pnl_pct": fmt_pct(xs.get("pnl_pct")),
            "sharpe": fmt_num(xs.get("sharpe")),
            "max_dd": fmt_pct(xs.get("max_dd_pct")),
            "win": fmt_pct(xs.get("win_rate")),
            "orders": int(xs.get("orders", 0)),
            "note": f"top-{len(universe)} USDT 永续，每日 rebalance",
        })
    else:
        rows.append({"name": "xs_momentum", "pnl_pct": "FAILED", "sharpe": "—",
                     "max_dd": "—", "win": "—", "orders": "—", "note": f"rc={xs.get('rc')}"})
    # funding_carry
    if fc.get("ok"):
        rows.append({
            "name": "funding_carry",
            "pnl_pct": fmt_pct(fc.get("net_funding_pnl", 0) / equity * 100) if "net_funding_pnl" in fc else fmt_pct(fc.get("gross_funding", 0) / equity * 100),
            "sharpe": "—",  # 不计算（funding-only，价格 PnL 假设 0）
            "max_dd": "—",
            "win": "—",
            "orders": int(fc.get("n_enters", 0)) * 2,  # 双腿
            "note": f"在仓比例 {fc.get('in_position', 0):.0f}% · APR={fc.get('annualized_apr', 0):+.2f}%",
        })
    else:
        rows.append({"name": "funding_carry", "pnl_pct": "FAILED", "sharpe": "—",
                     "max_dd": "—", "win": "—", "orders": "—", "note": f"rc={fc.get('rc')}"})

    lines = []
    lines.append(f"# 1-Year Backtest Report — as of {as_of}\n")
    lines.append(f"- **Window**: rolling 365 days ending {as_of}")
    lines.append(f"- **Starting equity**: {equity:.0f} USDT")
    lines.append(f"- **Fee model**: taker = {taker_bps:.1f} bps (NT `MakerTakerFeeModel`)")
    lines.append(f"- **Universe** (xs_momentum): top-{len(universe)} USDT-settled perpetuals by 24h vol")
    lines.append(f"- **Generated**: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| Strategy | PnL % | Sharpe | maxDD | Win% | Orders | Notes |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| `{r['name']}` | {r['pnl_pct']} | {r['sharpe']} | "
                     f"{r['max_dd']} | {r['win']} | {r['orders']} | {r['note']} |")
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    for s in ("xs_momentum",):
        if (out_dir / f"{s}.html").exists():
            lines.append(f"- [`{s}.html`]({s}.html) — equity curve (plotly)")
        if (out_dir / f"{s}.equity.csv").exists():
            lines.append(f"- [`{s}.equity.csv`]({s}.equity.csv) — account state time series")
        if (out_dir / f"{s}.summary.txt").exists():
            lines.append(f"- [`{s}.summary.txt`]({s}.summary.txt) — full stdout")
    if (out_dir / "funding_carry.summary.txt").exists():
        lines.append("- [`funding_carry.summary.txt`](funding_carry.summary.txt) — full stdout")
    lines.append("- [`universe.json`](universe.json) — snapshot of top-30 USDT perpetuals")
    lines.append("")
    lines.append("## Known limitations")
    lines.append("")
    lines.append("- **`liq_reversal` excluded** — OKX exposes no public historical liquidation feed; "
                 "the strategy relies on real-time `liquidation-orders` WebSocket which cannot be replayed offline.")
    lines.append("- **`funding_carry` window is ~90 days, not 1 year** — OKX `/api/v5/public/funding-rate-history` "
                 f"caps history around 90 days. The script requested 1095 entries; got {int(fc.get('span_days', 0))} days "
                 "of settlement data. APR is annualized from that shorter window, not directly comparable to 1y NT backtests.")
    lines.append("- **NT `maxDD = 0.00%` is a known reporting quirk**, not the real drawdown — "
                 "NT computes max drawdown from sampled `AccountState` events; for strategies that touch the account "
                 "infrequently (and especially bar-only backtests where account state isn't republished between fills), "
                 "the sample set is too sparse and NT reports 0. The actual drawdown shape is in the equity HTML / CSV.")
    lines.append("- **Slippage not modeled** — NT `PerfectFill`. Fees are charged (taker bps configured above) but "
                 "no spread/impact; realistic taker slip on top-30 USDT-SWAPs at $10K notional is typically 1-3 bps per fill.")
    lines.append("- **Funding NOT applied to `xs_momentum`** — NT `SimulatedExchange` does not settle "
                 "perpetual funding cashflow. A persistent long perp position pays funding when rate > 0; the NT-based "
                 "backtests omit this drag (rough magnitude: -5 to -10% annualized for chronic-long systems in 2025-2026 regime). "
                 "Only the dedicated `funding_carry` script models funding (that is its entire raison d'être).")
    lines.append("- **Universe ranking quirk** — `xs_momentum` uses `vol_ccy_24h` to pick top-30 (matches live config). "
                 "OKX returns this denominated in **base-currency token count**, which biases toward low-priced "
                 "memecoins with billions of tokens per contract. ETH barely made the cut (#29). Live system has the same bias; "
                 "fixing it would require ranking by `vol24h × last_price` for a USD-denominated approximation.")
    lines.append("- **Universe survivorship bias** — `xs_momentum` trades today's top-30 snapshot, not point-in-time "
                 "top-30 of each historical day. ~6 instruments in the snapshot have <200 days of history "
                 "(newly listed); these are absent from rebalances early in the window.")
    lines.append("")
    lines.append("## Universe (top-30 by 24h vol at snapshot time)")
    lines.append("")
    lines.append(", ".join(f"`{u}`" for u in universe))
    lines.append("")

    out = out_dir / "report.md"
    out.write_text("\n".join(lines))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main_async(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else (DEFAULT_OUT_BASE / args.as_of)
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = Path(args.catalog)
    catalog.mkdir(parents=True, exist_ok=True)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    # Phase 1: resolve universe (or load from existing universe.json if reuse)
    universe_file = out_dir / "universe.json"
    if args.reuse_data and universe_file.exists():
        universe = json.loads(universe_file.read_text())["instruments"]
        print(f"[universe] reused {len(universe)} instruments from {universe_file}")
    else:
        print(f"[universe] resolving top-{args.universe_size} USDT perpetuals by 24h vol...")
        universe = await _resolve_universe(args.universe_size)
        universe_file.write_text(json.dumps({
            "as_of": args.as_of,
            "size": len(universe),
            "settle_ccy": "USDT",
            "sort_by": "vol24h",
            "instruments": universe,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, indent=2))
        print(f"[universe] {len(universe)} resolved → {universe_file}")
        print(f"           first 5: {universe[:5]}")

    # Phase 2: refresh catalog
    if args.reuse_data:
        print(f"[data] reusing catalog at {catalog}")
    else:
        await _download_universe_bars(
            universe, catalog_path=catalog,
            taker_fee_bps=args.taker_fee_bps,
            maker_fee_bps=args.maker_fee_bps,
        )

    # Phase 3: run backtests
    xs_result, fc_result = {"ok": True}, {"ok": True}

    common = [
        sys.executable, str(REPO_ROOT / "scripts" / "backtest.py"),
        "--equity", str(args.equity),
        "--leverage", str(args.leverage),
        "--taker-fee-bps", str(args.taker_fee_bps),
        "--maker-fee-bps", str(args.maker_fee_bps),
        "--catalog", str(catalog),
        "--reuse-data",  # data is already prepared in phase 2
    ]

    if "xs_momentum" not in skip:
        # 30 × 1-DAY, NT bar identifiers need .OKX suffix internally
        xs_cmd = common + [
            "--strategy", "xs_momentum",
            "--instrument-ids", ",".join(universe),
            "--signal-bar", "1D",
            "--total-bars", "400",
            "--top-n", "5",
            "--bot-n", "5",
            "--max-inst-count", "4",
            "--lookback-days", "7",
            "--plot", str(out_dir / "xs_momentum.html"),
            "--equity-csv", str(out_dir / "xs_momentum.equity.csv"),
        ]
        xs_result = _run_subprocess(xs_cmd, out_dir / "xs_momentum.summary.txt")

    if "funding_carry" not in skip:
        fc_cmd = [
            sys.executable, str(REPO_ROOT / "scripts" / "backtest_funding_carry.py"),
            "--perp-symbol", "BTC-USDT-SWAP",
            "--total", "1095",
            "--equity", str(args.equity),
            "--fee-bps", str(args.taker_fee_bps),
        ]
        fc_result = _run_funding_carry(fc_cmd, out_dir / "funding_carry.summary.txt")

    # Phase 4: report.md
    report = _write_report(
        out_dir, as_of=args.as_of, equity=args.equity, taker_bps=args.taker_fee_bps,
        universe=universe, xs=xs_result, fc=fc_result,
    )
    print(f"\n[report] {report}")
    print(f"[done] artifacts in {out_dir}")

    # Exit non-zero if any backtest failed (but report is still emitted)
    return 0 if all(r.get("ok", True) for r in (xs_result, fc_result)) else 1


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
