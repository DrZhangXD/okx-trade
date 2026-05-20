"""Strategy Truth Dashboard - per-strategy real PnL from OKX bills.

读 ``trades_okx``（由 ``scripts/reconcile_pnl_from_okx.py`` 填充）输出每个策略
真实 OKX 账户层面的 PnL / fills / fee / win_rate / drawdown markdown 报告。

用法::

    .venv/bin/python scripts/strategy_truth_dashboard.py                # 默认全期
    .venv/bin/python scripts/strategy_truth_dashboard.py --since 2026-05-08
    .venv/bin/python scripts/strategy_truth_dashboard.py --days 7
    .venv/bin/python scripts/strategy_truth_dashboard.py --output -    # stdout
    .venv/bin/python scripts/strategy_truth_dashboard.py --json        # json 给 monitor

输出文件：``var/audits/strategy_truth_<YYYYMMDD>.md``

每日 cron 跑可作为真实损益 source of truth，对照 ``trades`` 表估算偏差用。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_strategy_stats(
    db_path: Path, since_ms: int | None = None, until_ms: int | None = None,
) -> dict:
    """For each strategy_id in trades_okx, aggregate fills + balChg + realized + fee.

    Returns:
        {
            "by_strategy": {sid: {fills, balchg, realized, fee, days_active,
                                   daily_balchg: [(day, bal)], avg_per_fill}},
            "totals": {fills, balchg, realized, fee, n_strategies, missing_strat},
            "by_day_total": [(day, balchg, fills)],
            "window": (since_day, until_day, n_days),
        }
    """
    conn = sqlite3.connect(str(db_path))
    where_parts = []
    params: list = []
    if since_ms is not None:
        where_parts.append("ts_ms >= ?")
        params.append(int(since_ms))
    if until_ms is not None:
        where_parts.append("ts_ms <= ?")
        params.append(int(until_ms))
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # by strategy
    cur = conn.execute(
        f"SELECT COALESCE(strategy_id, '<unmapped>') AS sid, "
        f"  COUNT(*) AS fills, "
        f"  ROUND(SUM(bal_chg), 4) AS balchg, "
        f"  ROUND(SUM(pnl), 4) AS realized, "
        f"  ROUND(SUM(fee), 4) AS fee "
        f"FROM trades_okx {where} GROUP BY sid",
        params,
    )
    by_strategy_raw = {r[0]: {"fills": r[1], "balchg": r[2] or 0,
                              "realized": r[3] or 0, "fee": r[4] or 0}
                       for r in cur.fetchall()}

    # daily per-strategy timeline
    cur = conn.execute(
        f"SELECT COALESCE(strategy_id, '<unmapped>') AS sid, "
        f"  date(ts_ms/1000, 'unixepoch') AS day, "
        f"  ROUND(SUM(bal_chg), 4) AS balchg, COUNT(*) AS fills "
        f"FROM trades_okx {where} GROUP BY sid, day ORDER BY sid, day",
        params,
    )
    daily_by_sid: dict[str, list[tuple[str, float, int]]] = {}
    for sid, day, balchg, fills in cur.fetchall():
        daily_by_sid.setdefault(sid, []).append((day, balchg or 0, fills))

    # daily account totals
    cur = conn.execute(
        f"SELECT date(ts_ms/1000, 'unixepoch') AS day, "
        f"  ROUND(SUM(bal_chg), 4), COUNT(*) "
        f"FROM trades_okx {where} GROUP BY day ORDER BY day",
        params,
    )
    by_day_total = [(r[0], r[1] or 0, r[2]) for r in cur.fetchall()]

    conn.close()

    # Compose. 关键修正 (2026-05-20):
    # "true_pnl" 必须用 realized + fee。balChg sum 是错的——spot 买入会减
    # USDT 但 BTC 资产还在账户（被 totalEq collateral 算进去），那不是损失。
    # spot_holding_value = balChg - true_pnl ≈ 还在仓里没卖的 BTC 价值。
    by_strategy: dict[str, dict] = {}
    for sid, base in by_strategy_raw.items():
        days = daily_by_sid.get(sid, [])
        bals = [d[1] for d in days]
        true_pnl = base["realized"] + base["fee"]
        spot_holding_value = base["balchg"] - true_pnl
        # Drawdown from cumulative daily TRUE PnL (not balChg)
        # NOTE: we use balChg-based daily for now since per-day realized
        # decomp would require a separate aggregation pass; max_dd here
        # over-estimates for spot-heavy strategies.
        cumsum = 0.0
        peak = 0.0
        max_dd = 0.0
        for b in bals:
            cumsum += b
            peak = max(peak, cumsum)
            max_dd = min(max_dd, cumsum - peak)
        winning_days = sum(1 for b in bals if b > 0)
        avg_per_fill = true_pnl / base["fills"] if base["fills"] > 0 else 0
        by_strategy[sid] = {
            **base,
            "true_pnl": round(true_pnl, 2),
            "spot_holding_value": round(spot_holding_value, 2),
            "days_active": len(days),
            "winning_days": winning_days,
            "max_dd": round(max_dd, 4),
            "avg_per_fill_usdt": round(avg_per_fill, 4),
            "daily_balchg": days,
        }

    return {
        "by_strategy": by_strategy,
        "by_day_total": by_day_total,
        "totals": {
            "fills": sum(r["fills"] for r in by_strategy.values()),
            "balchg": round(sum(r["balchg"] for r in by_strategy.values()), 2),
            "realized": round(sum(r["realized"] for r in by_strategy.values()), 2),
            "fee": round(sum(r["fee"] for r in by_strategy.values()), 2),
            "n_strategies": len([s for s in by_strategy if s != "<unmapped>"]),
        },
    }


def render_markdown(data: dict, *, window_label: str) -> str:
    by_strategy = data["by_strategy"]
    totals = data["totals"]
    by_day = data["by_day_total"]

    lines = []
    lines.append(f"# Strategy Truth Dashboard — {window_label}")
    lines.append(f"_Generated {datetime.now(tz=timezone.utc).isoformat()}_")
    lines.append("")
    lines.append("数据源: ``trades_okx`` (OKX account/bills 权威账本)。"
                 "对比 ``trades`` (策略估算)：见 ``pnl_reconciliation_*.jsonl``。")
    lines.append("")

    # 总真实 PnL = sum of realized + fee
    total_true_pnl = totals["realized"] + totals["fee"]
    total_spot_holding = totals["balchg"] - total_true_pnl

    # ---- TL;DR ----
    lines.append("## TL;DR")
    lines.append(f"- 总 fills: **{totals['fills']:,}**")
    lines.append(f"- **真实账户损益（realized + fee）: ${total_true_pnl:+,.2f}** ← 你要看的就是这个")
    lines.append(f"- 还在仓里的 spot 资产价值（balChg - true_pnl）: ${total_spot_holding:+,.2f}")
    lines.append(f"  - 这部分在 totalEq 的非 USDT collateral 里，不是损失")
    lines.append(f"- 总 USDT 现金流 balChg: ${totals['balchg']:+,.2f}（含 5/15 paper 注资 +$75k）")
    lines.append(f"- 总 realized PnL: ${totals['realized']:+,.2f}")
    lines.append(f"- 总 fee: ${totals['fee']:+,.2f}")
    lines.append("")

    # ---- Strategy rank ----
    lines.append("## Per-strategy rank (按 真实损益 true_pnl 升序，最亏在前)")
    lines.append("")
    lines.append(f"| 策略 | **真实损益** | spot 持仓 | fills | avg/fill | realized | fee | balChg (≠损益) | days |")
    lines.append(f"|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    ranked = sorted(by_strategy.items(), key=lambda kv: kv[1]["true_pnl"])
    for sid, r in ranked:
        lines.append(
            f"| **{sid}** | "
            f"**${r['true_pnl']:+,.2f}** | "
            f"${r['spot_holding_value']:+,.2f} | "
            f"{r['fills']:,} | "
            f"${r['avg_per_fill_usdt']:+.4f} | "
            f"${r['realized']:+,.2f} | "
            f"${r['fee']:+,.2f} | "
            f"${r['balchg']:+,.2f} | "
            f"{r['days_active']} |"
        )
    lines.append("")
    lines.append("**关键解读**：")
    lines.append("- `真实损益 = realized + fee`：OKX 已实现盈亏 + 手续费，这才是账户层面真的赚/亏")
    lines.append("- `spot 持仓 = balChg - true_pnl`：strategy 还持有的 BTC/ETH spot 现货价值，**不是损失**")
    lines.append("- `balChg` 单纯 sum 会把【买入 spot 还在仓】误算成【亏损】——这是 2026-05-20 早先的错误")
    lines.append("")

    # ---- Daily account total timeline ----
    lines.append("## Daily account total (cumulative)")
    lines.append("")
    lines.append("| day | fills | day balChg | cum balChg |")
    lines.append("|---|---:|---:|---:|")
    cum = 0.0
    for day, bal, fills in by_day:
        cum += bal
        marker = "  ❌" if bal < -1000 else "  ⚠️" if bal < -100 else ""
        lines.append(f"| {day} | {fills:,} | ${bal:+,.2f}{marker} | ${cum:+,.2f} |")
    lines.append("")

    # ---- Per-strategy daily detail (only flag biggest losers by TRUE pnl) ----
    losers = [sid for sid, r in by_strategy.items()
              if r["true_pnl"] < -100 and sid != "<unmapped>"]
    if losers:
        lines.append("## 重点关注 (真实损益 > -$100)")
        for sid in losers:
            r = by_strategy[sid]
            lines.append(f"\n### `{sid}`  (net **${r['balchg']:+,.2f}** USDT)")
            lines.append("")
            lines.append("| day | balChg | fills |")
            lines.append("|---|---:|---:|")
            for day, bal, fills in r["daily_balchg"]:
                m = "  ❌" if bal < -1000 else "  ⚠️" if bal < -100 else ""
                lines.append(f"| {day} | ${bal:+,.2f}{m} | {fills:,} |")
        lines.append("")

    # ---- Decision verdict ----
    lines.append("## Strategy verdict")
    lines.append("")
    lines.append("基于真实损益（realized + fee）的状态判定（不构成永久结论）：")
    lines.append("")
    lines.append("| 策略 | 状态 | 建议 |")
    lines.append("|---|---|---|")
    for sid, r in ranked:
        if sid == "<unmapped>":
            continue
        # 改用 true_pnl 判定
        if r["true_pnl"] < -1000:
            status = "🔴 大额亏损"
            advice = "立即评估是否 disable / 改阈值"
        elif r["true_pnl"] < -100:
            status = "🟡 持续亏损"
            advice = "查参数 / 检查 fee 占 alpha 比例"
        elif r["true_pnl"] < 0:
            status = "🟠 小额亏损"
            advice = "继续观察"
        else:
            status = "🟢 持平/盈利"
            advice = "保持"
        lines.append(f"| `{sid}` | {status} | {advice} |")
    lines.append("")
    lines.append("---")
    lines.append("生成命令: `python scripts/strategy_truth_dashboard.py`")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("var/pnl.sqlite"))
    ap.add_argument("--since", type=str, help="起始日期 YYYY-MM-DD UTC")
    ap.add_argument("--until", type=str, help="结束日期 YYYY-MM-DD UTC")
    ap.add_argument("--days", type=int, help="最近 N 天（覆盖 --since）")
    ap.add_argument("--output", type=str, default="auto",
                    help="输出路径；'-' = stdout；'auto' = var/audits/")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 markdown")
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    since_ms = None
    until_ms = None
    if args.days:
        since_ms = now_ms - args.days * 86_400_000
        until_ms = now_ms
    elif args.since:
        since_ms = int(datetime.strptime(args.since, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
    if args.until:
        until_ms = int(datetime.strptime(args.until, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)

    data = fetch_strategy_stats(args.db, since_ms, until_ms)

    if args.json:
        out = json.dumps(data, indent=2, default=str)
    else:
        window = (f"{args.since or 'all'} → {args.until or utc_day(now_ms)} UTC")
        out = render_markdown(data, window_label=window)

    if args.output == "-":
        print(out)
    else:
        if args.output == "auto":
            target_dir = Path("var/audits")
            target_dir.mkdir(parents=True, exist_ok=True)
            suffix = "json" if args.json else "md"
            path = target_dir / f"strategy_truth_{utc_day(now_ms).replace('-', '')}.{suffix}"
        else:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out)
        print(f"written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
