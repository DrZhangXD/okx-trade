"""4 策略并行 paper trading 启动脚手架（M5）。

用法::

    # 校验 yaml + universe（不启动 NT engine）
    python scripts/live.py --config configs/live.yaml --check

    # 实际启动 NT TradingNode + monitor + 4 策略
    python scripts/live.py --config configs/live.yaml --run

    # 只跑一次 daily report 生成
    python scripts/live.py --config configs/live.yaml --report-only

支持的 4 个策略：
- range_breakout (M2)
- funding_carry  (M2)
- xs_momentum    (M4)
- liq_reversal   (M4)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 允许从仓库根直接 ``python scripts/live.py``
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from okx_trade import OKXRestClient, OKXSettings
from okx_trade.adapter.constants import OKX_VENUE
from okx_trade.enums import InstType


def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def resolve_strategy_configs(live_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """读取每个 enabled 策略的子 yaml，返回 ``{name: cfg}``。"""
    out: dict[str, dict[str, Any]] = {}
    for name, entry in live_cfg.get("strategies", {}).items():
        if not entry.get("enabled", False):
            continue
        cfg_path = entry["config"]
        out[name] = load_yaml(cfg_path)
    return out


async def resolve_universe(
    rest: OKXRestClient, size: int, settle_ccy: str,
) -> list[str]:
    """按 24h 成交额拉 top N USDT-settled 永续，返回 NT inst id 列表。"""
    instruments = await rest.public.get_instruments(InstType.SWAP)
    perps = [i for i in instruments
             if i.settle_ccy == settle_ccy and i.state == "live"]

    tickers = await rest.market.get_tickers(InstType.SWAP)
    vol_by_inst = {t.inst_id: float(t.vol_ccy_24h or 0) for t in tickers}

    perps.sort(key=lambda i: vol_by_inst.get(i.inst_id, 0.0), reverse=True)
    return [f"{p.inst_id}.{OKX_VENUE}" for p in perps[:size]]


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
async def check_async(args: argparse.Namespace) -> int:
    live_cfg = load_yaml(args.config)
    print(f"[1/4] loaded {args.config}")

    strategies = resolve_strategy_configs(live_cfg)
    enabled = list(strategies.keys())
    print(f"[2/4] enabled strategies: {enabled}")
    for name, cfg in strategies.items():
        print(f"        - {name}: {len(cfg)} fields")

    if "xs_momentum" in strategies:
        u = live_cfg.get("xs_momentum_universe", {})
        size = int(u.get("size", 30))
        settle = u.get("settle_ccy", "USDT")
        try:
            async with OKXRestClient(OKXSettings()) as rest:
                ids = await resolve_universe(rest, size, settle)
            print(f"[3/4] xs_momentum universe ({size}, {settle}): {len(ids)} resolved")
            print(f"        first 5: {ids[:5]}")
        except Exception as exc:
            print(f"[3/4] universe resolution skipped (no network / no creds): {exc}")
    else:
        print("[3/4] xs_momentum disabled, skipping universe resolution")

    rd = live_cfg.get("risk_defaults", {})
    print(f"[4/4] risk defaults: drawdown_daily={rd.get('drawdown_daily_pct')} "
          f"drawdown_weekly={rd.get('drawdown_weekly_pct')} "
          f"enable_kelly={rd.get('enable_kelly')} "
          f"enable_correlation={rd.get('enable_correlation')}")

    po = live_cfg.get("portfolio_optimizer", {})
    print(f"      portfolio: mode={po.get('mode', 'equal')} "
          f"min_history_days={po.get('min_history_days', 30)}")

    print("\nAll strategy configs valid; ready for `--run`.")
    return 0


# ---------------------------------------------------------------------------
# --run
# ---------------------------------------------------------------------------
def run_live(args: argparse.Namespace) -> int:
    """启动 NT TradingNode + LiveMonitor + 4 策略。"""
    from okx_trade.runtime import build_live_context

    live_cfg = load_yaml(args.config)
    ctx = build_live_context(live_cfg, build_node=True)

    if ctx.node is None:
        print("ERROR: NT TradingNode build failed; check logs.")
        return 1

    print(f"[live] {len(ctx.strategies)} strategies registered: "
          f"{list(ctx.strategies.keys())}")
    print(f"[live] allocator={type(ctx.allocator).__name__}")
    print(f"[live] sinks={[type(s).__name__ for s in ctx.sinks]}")

    # spawn monitor + daily-report 后台 task 在 NT loop 启动前 schedule
    monitor_task: asyncio.Task | None = None
    report_task: asyncio.Task | None = None
    loop = asyncio.get_event_loop()
    if ctx.monitor is not None:
        monitor_task = loop.create_task(ctx.monitor.run())
    if ctx.reporter is not None:
        report_task = loop.create_task(
            ctx.reporter.run_loop(
                on_write=lambda p: print(f"[daily-report] written: {p}"),
                on_error=lambda e: print(f"[daily-report] error: {e}"),
            ),
        )

    try:
        ctx.node.run()
    finally:
        if ctx.monitor is not None:
            ctx.monitor.stop()
        if monitor_task is not None and not monitor_task.done():
            monitor_task.cancel()
        if report_task is not None and not report_task.done():
            report_task.cancel()
        ctx.tracker.close()
    return 0


# ---------------------------------------------------------------------------
# --report-only
# ---------------------------------------------------------------------------
def report_only(args: argparse.Namespace) -> int:
    """读 PnL DB，写一份当日 daily report 后退出。"""
    from okx_trade.runtime import build_live_context

    live_cfg = load_yaml(args.config)
    ctx = build_live_context(live_cfg, build_node=False)
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    p = ctx.reporter.write_for_date(today)
    print(f"daily report written: {p}")
    ctx.tracker.close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OKX multi-strategy paper trading")
    p.add_argument("--config", default="configs/live.yaml")
    p.add_argument("--check", action="store_true",
                   help="只校验配置 + universe 解析，不启动 NT engine")
    p.add_argument("--run", action="store_true",
                   help="启动 NT TradingNode + monitor 跑 4 策略并行 paper trading")
    p.add_argument("--report-only", action="store_true",
                   help="读 PnL DB 写一份当日 JSON daily report 后退出")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    flags = sum(int(b) for b in (args.check, args.run, args.report_only))
    if flags == 0:
        # 默认走 check
        args.check = True
    elif flags > 1:
        print("ERROR: --check / --run / --report-only 只能选一个")
        return 2

    if args.check:
        return asyncio.run(check_async(args))
    if args.report_only:
        return report_only(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
