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

    # Compose
    by_strategy: dict[str, dict] = {}
    for sid, base in by_strategy_raw.items():
        days = daily_by_sid.get(sid, [])
        bals = [d[1] for d in days]
        # Compute drawdown from cumulative daily balChg
        cumsum = 0.0
        peak = 0.0
        max_dd = 0.0
        for b in bals:
            cumsum += b
            peak = max(peak, cumsum)
            max_dd = min(max_dd, cumsum - peak)
        winning_days = sum(1 for b in bals if b > 0)
        avg_per_fill = base["balchg"] / base["fills"] if base["fills"] > 0 else 0
        by_strategy[sid] = {
            **base,
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

    # ---- TL;DR ----
    lines.append("## TL;DR")
    lines.append(f"- 总 fills: **{totals['fills']:,}**")
    lines.append(f"- 总账户 balChg: **${totals['balchg']:+,.2f}** (USDT cash)")
    lines.append(f"- 总实现 PnL (OKX 'pnl' 字段): ${totals['realized']:+,.2f}")
    lines.append(f"- 总 fee: ${totals['fee']:+,.2f}")
    lines.append("")

    # ---- Strategy rank ----
    lines.append("## Per-strategy rank (按 net balChg 升序，最亏的在前)")
    lines.append("")
    lines.append(f"| 策略 | 真实 net balChg | fills | avg/fill | realized | fee | days active | winning days | max DD |")
    lines.append(f"|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    ranked = sorted(by_strategy.items(), key=lambda kv: kv[1]["balchg"])
    for sid, r in ranked:
        lines.append(
            f"| **{sid}** | "
            f"${r['balchg']:+,.2f} | "
            f"{r['fills']:,} | "
            f"${r['avg_per_fill_usdt']:+.3f} | "
            f"${r['realized']:+,.2f} | "
            f"${r['fee']:+,.2f} | "
            f"{r['days_active']} | "
            f"{r['winning_days']} | "
            f"${r['max_dd']:+,.2f} |"
        )
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

    # ---- Per-strategy daily detail (only flag biggest losers) ----
    losers = [sid for sid, r in by_strategy.items()
              if r["balchg"] < -100 and sid != "<unmapped>"]
    if losers:
        lines.append("## 重点关注 (losing > $100)")
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
    lines.append("基于真实账户数据的状态判定（仅基于此窗口，不构成永久结论）：")
    lines.append("")
    lines.append("| 策略 | 状态 | 建议 |")
    lines.append("|---|---|---|")
    for sid, r in ranked:
        if sid == "<unmapped>":
            continue
        if r["balchg"] < -1000:
            status = "🔴 大额亏损"
            advice = "立即评估是否 disable / 改阈值"
        elif r["balchg"] < -100:
            status = "🟡 持续亏损"
            advice = "查参数 / 检查 fee 占 alpha 比例"
        elif r["balchg"] < 0:
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
