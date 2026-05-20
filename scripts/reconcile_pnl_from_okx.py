"""每日 OKX 账户 bills → pnl.sqlite.trades_okx 权威账本（+ 偏差报告）。

为什么：策略侧 ``record_strategy_trade`` 在 ``submit_order`` 之后立刻写一条 trade
（不等 OrderFilled，用 bar.close 估算价），订单失败 / 部分成交 / rate-limit drop
时仍写入；现网观察 OBImbalance 5/18 一天写 485 条但 OKX 实际只 31 笔成交，
PnL 估算 +6,340 而真实账户只动 +3。本脚本拉 OKX bills 作为权威源。

用法：
    .venv/bin/python scripts/reconcile_pnl_from_okx.py                # 最近 24h
    .venv/bin/python scripts/reconcile_pnl_from_okx.py --days 7
    .venv/bin/python scripts/reconcile_pnl_from_okx.py --since 2026-05-17 --until 2026-05-20

输出：
    1. ``trades_okx`` 表：bill_id (PK) + ts_ms + inst_id + type/subType + clOrdId + balChg/pnl/fee
       INSERT OR IGNORE → 重跑幂等
    2. stdout 打印每日偏差摘要：``trades.pnl_usdt (estimate)`` vs ``bills (actual)`` per (strategy, day)
    3. ``var/pnl_reconciliation_<YYYYMMDD>.jsonl``：写到磁盘供 daily_report / alert 消费

Cron（VPS）::

    5 0 * * *  /home/okxtrade/okx-trade/.venv/bin/python \\
               /home/okxtrade/okx-trade/scripts/reconcile_pnl_from_okx.py --days 2 >> /var/log/reconcile.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from okx_trade import OKXRestClient, OKXSettings


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades_okx (
    bill_id    TEXT PRIMARY KEY,
    ts_ms      INTEGER NOT NULL,
    inst_id    TEXT,
    bill_type  TEXT,
    sub_type   TEXT,
    cl_ord_id  TEXT,
    strategy_id TEXT,
    bal_chg    REAL,
    pnl        REAL,
    fee        REAL
)
"""
INDEX = "CREATE INDEX IF NOT EXISTS idx_trades_okx_ts ON trades_okx(ts_ms)"
INDEX_STRAT = "CREATE INDEX IF NOT EXISTS idx_trades_okx_sid ON trades_okx(strategy_id, ts_ms)"


def utc_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def parse_strategy_index(cl_ord_id: str | None) -> int | None:
    """NT clOrdId 在 OKX bills 里是去破折号的紧凑格式
    ``O{YYYYMMDD}{HHMMSS}{trader3}{stratidx3}{seq}``
    （例：``O202605200713000010044`` → strategy_idx=4）。

    NT journal log 里能看到带破折号的版本 ``O-20260520-071300-001-004-4``，
    但 OKX 保存时剥掉了，所以这里按位置截。

    解析失败返回 None。
    """
    if not cl_ord_id or not cl_ord_id.startswith("O") or len(cl_ord_id) < 22:
        return None
    # 跳过 'O' + YYYYMMDD (8) + HHMMSS (6) + trader3 (3) = 18 chars
    # strategy_idx = chars[18:21]
    try:
        return int(cl_ord_id[18:21])
    except (ValueError, IndexError):
        return None


def map_strategy_id(idx: int | None, registered: list[str]) -> str | None:
    """strategy_idx → live.yaml strategies 顺序里的 name。

    NT 注册顺序在 ``runtime.live_node._resolve_strategy_specs`` 里按 yaml 出现
    顺序，所以 idx 与 yaml 列表索引一致。Out-of-range 返回 None（容错）。
    """
    if idx is None or idx < 0 or idx >= len(registered):
        return None
    return registered[idx]


def get_registered_strategies(live_yaml_path: Path) -> list[str]:
    """从 live.yaml 拉 enabled strategy 顺序作为 NT 注册顺序的近似。

    精确做法是 import runtime.live_node._resolve_strategy_specs，但为了脚本可独立
    跑（不依赖 strategy 模块），这里直接读 yaml。
    """
    try:
        import yaml
        with live_yaml_path.open() as f:
            cfg = yaml.safe_load(f)
        strategies = cfg.get("strategies", {}) or {}
        return [name for name, spec in strategies.items() if (spec or {}).get("enabled", False)]
    except Exception:
        return []


async def fetch_bills(begin_ms: int, end_ms: int) -> list[dict]:
    """分页拉 [begin, end] 窗口 bills。OKX 单页 100，限 200 页（20000 笔）。
    单日活跃可达 1k-2k 笔（高频策略），原 30 页 cap 在 3+ 天窗口里会被打爆。"""
    out: list[dict] = []
    after: str | None = None
    settings = OKXSettings()
    async with OKXRestClient(settings) as client:
        for _ in range(200):
            params: dict[str, str] = {
                "begin": str(begin_ms),
                "end":   str(end_ms),
                "limit": "100",
            }
            if after:
                params["after"] = after
            data = await client.transport.request(
                "GET", "/api/v5/account/bills",
                params=params, private=True, group=None,
            )
            if not data:
                break
            out.extend(data)
            if len(data) < 100:
                break
            after = data[-1]["billId"]
    return out


def upsert_bills(
    db_path: Path, bills: list[dict], registered: list[str],
) -> tuple[int, int]:
    """Insert OR IGNORE bills into trades_okx. Returns (inserted, total)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(SCHEMA)
        conn.execute(INDEX)
        conn.execute(INDEX_STRAT)
        conn.commit()

        inserted = 0
        for b in bills:
            cl_ord_id = b.get("clOrdId") or None
            idx = parse_strategy_index(cl_ord_id)
            sid = map_strategy_id(idx, registered)
            cur = conn.execute(
                "INSERT OR IGNORE INTO trades_okx "
                "(bill_id, ts_ms, inst_id, bill_type, sub_type, cl_ord_id, "
                " strategy_id, bal_chg, pnl, fee) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    b.get("billId"),
                    int(b.get("ts", "0") or 0),
                    b.get("instId") or None,
                    b.get("type", ""),
                    b.get("subType", ""),
                    cl_ord_id,
                    sid,
                    float(Decimal(b.get("balChg") or "0")),
                    float(Decimal(b.get("pnl") or "0")),
                    float(Decimal(b.get("fee") or "0")),
                ),
            )
            inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return inserted, len(bills)


def discrepancy_report(
    db_path: Path, since_ms: int, until_ms: int,
) -> list[dict]:
    """对比 trades（策略估算） vs trades_okx（OKX 权威）按 (day, strategy) 汇总。

    Returns:
        list of {day, strategy_id, est_pnl, real_balchg, real_pnl, real_fee,
                 est_n, real_n, divergence_pct}
    """
    conn = sqlite3.connect(str(db_path))
    try:
        # legacy trades table may not exist (e.g. test db); treat as empty
        has_trades = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if has_trades:
            cur = conn.execute(
                "SELECT strategy_id, "
                "  date(closed_ts_ms/1000, 'unixepoch') AS day, "
                "  COUNT(*) AS n, SUM(pnl_usdt) AS pnl "
                "FROM trades WHERE closed_ts_ms BETWEEN ? AND ? "
                "GROUP BY strategy_id, day",
                (since_ms, until_ms),
            )
            est = {(r[1], r[0]): (int(r[2]), float(r[3] or 0)) for r in cur.fetchall()}
        else:
            est = {}
        cur = conn.execute(
            "SELECT strategy_id, "
            "  date(ts_ms/1000, 'unixepoch') AS day, "
            "  COUNT(*) AS n, SUM(bal_chg) AS bal, SUM(pnl) AS pnl, SUM(fee) AS fee "
            "FROM trades_okx WHERE ts_ms BETWEEN ? AND ? "
            "GROUP BY strategy_id, day",
            (since_ms, until_ms),
        )
        real = {(r[1], r[0] or "<unmapped>"): (int(r[2]), float(r[3] or 0),
                                                float(r[4] or 0), float(r[5] or 0))
                for r in cur.fetchall()}
    finally:
        conn.close()

    keys = sorted(set(est) | set(real))
    rows: list[dict] = []
    for day, sid in keys:
        en, ep = est.get((day, sid), (0, 0.0))
        rn, rb, rp, rf = real.get((day, sid), (0, 0.0, 0.0, 0.0))
        # divergence: |est - real_balchg| / max(|real|, 1)
        diff = ep - rb
        ratio = diff / rb * 100 if abs(rb) > 0.01 else (float("inf") if abs(diff) > 0.01 else 0.0)
        rows.append({
            "day": day,
            "strategy_id": sid,
            "est_n": en,
            "est_pnl": round(ep, 4),
            "real_n_fills": rn,
            "real_balchg": round(rb, 4),
            "real_pnl": round(rp, 4),
            "real_fee": round(rf, 4),
            "divergence_usdt": round(diff, 4),
            "divergence_pct": round(ratio, 1) if isinstance(ratio, float) and ratio != float("inf") else "inf",
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1, help="窗口天数（默认最近 1 天）")
    ap.add_argument("--since", type=str, help="起始日期 YYYY-MM-DD UTC（覆盖 --days）")
    ap.add_argument("--until", type=str, help="结束日期 YYYY-MM-DD UTC（默认现在）")
    ap.add_argument("--db", type=Path, default=Path("var/pnl.sqlite"))
    ap.add_argument("--live-yaml", type=Path, default=Path("configs/live.yaml"))
    ap.add_argument("--report-dir", type=Path, default=Path("var"))
    ap.add_argument("--no-report", action="store_true", help="只写 db，不打印/写报告")
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    if args.since:
        begin_ms = int(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        begin_ms = now_ms - args.days * 86_400_000
    if args.until:
        end_ms = int(datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        end_ms = now_ms

    print(f"reconcile window: {utc_day(begin_ms)} → {utc_day(end_ms)} UTC")
    print(f"db: {args.db}")

    registered = get_registered_strategies(args.live_yaml)
    print(f"registered strategies (yaml order, for clOrdId → sid mapping): {registered}")

    bills = asyncio.run(fetch_bills(begin_ms, end_ms))
    print(f"fetched {len(bills)} bills from OKX")

    inserted, total = upsert_bills(args.db, bills, registered)
    print(f"inserted {inserted}/{total} new rows into trades_okx")

    if args.no_report:
        return 0

    report = discrepancy_report(args.db, begin_ms, end_ms)
    print(f"\n=== discrepancy report ({len(report)} (day, strategy) cells) ===")
    print(f"{'day':<12} {'strategy':<28} {'est_n':>6} {'est_pnl':>10} {'real_n':>7} "
          f"{'real_bal':>10} {'real_pnl':>10} {'div_usdt':>10} {'div_pct':>8}")
    for r in report:
        print(f"{r['day']:<12} {r['strategy_id'] or '<unmapped>':<28} "
              f"{r['est_n']:>6} {r['est_pnl']:>+10.2f} {r['real_n_fills']:>7} "
              f"{r['real_balchg']:>+10.2f} {r['real_pnl']:>+10.2f} "
              f"{r['divergence_usdt']:>+10.2f} {str(r['divergence_pct']):>8}")

    report_path = args.report_dir / f"pnl_reconciliation_{utc_day(end_ms).replace('-', '')}.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w") as f:
        for r in report:
            f.write(json.dumps(r) + "\n")
    print(f"\nreport written: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
