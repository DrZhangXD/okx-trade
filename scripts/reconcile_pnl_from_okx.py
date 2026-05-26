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
    """从 live.yaml 拉 strategy 顺序作为 NT 注册顺序的近似。

    返回 **yaml 出现顺序的全部 strategy（含 disabled）**，不仅 enabled，因为
    historical OKX bills 的 clOrdId stratidx 是基于当时 yaml 顺序，若按"当前
    enabled 列表"算会因为 disable/enable 移位导致历史归因错乱。

    例：basis_arb 5/20 disable 后，若只取 enabled，索引 3 从 basis_arb 移到
    ob_imbalance，5/19 的 basis_arb fills 会被错误归到 ob_imbalance。
    """
    try:
        import yaml
        with live_yaml_path.open() as f:
            cfg = yaml.safe_load(f)
        strategies = cfg.get("strategies", {}) or {}
        return list(strategies.keys())  # yaml 出现顺序，含 disabled
    except Exception:
        return []


async def _fetch_bills_window(client, begin_ms: int, end_ms: int) -> list[dict]:
    """分页拉单窗口 [begin, end] 的 bills。最多 200 页 × 100 = 20000 笔。

    OKX bills 是新→旧顺序返回；``after=oldest_billId_of_page`` 翻下一页（更旧）。
    单窗口超过 20k 时只能拿到最新的 20k，最旧的会被丢——所以高频日必须靠
    上层 ``fetch_bills`` 按日切窗口绕开。
    """
    out: list[dict] = []
    after: str | None = None
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


async def fetch_bills(begin_ms: int, end_ms: int, chunk_hours: int = 24) -> list[dict]:
    """切 ``chunk_hours`` 小时窗口逐段拉，绕过单窗口 20k 上限。

    为什么：2026-05-25 单日产生 ~20k bills（stat_arb_pairs 一家就 19,624 fill），
    7 天窗口一次拉只能拿到最新 20k，早段被截掉，对账时表现为净差几万 USDT
    与 totalEq 对不上。按日切后每日独立分页，重复 bill 落表靠
    ``INSERT OR IGNORE`` 幂等。

    Cap-hit 检测：单 chunk 满 20k 时打 WARN，提示需更细 chunk_hours。
    """
    chunk_ms = chunk_hours * 3_600_000
    settings = OKXSettings()
    out: list[dict] = []
    async with OKXRestClient(settings) as client:
        cur = begin_ms
        while cur < end_ms:
            nxt = min(cur + chunk_ms, end_ms)
            chunk = await _fetch_bills_window(client, cur, nxt)
            cur_str = datetime.fromtimestamp(cur / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            nxt_str = datetime.fromtimestamp(nxt / 1000, tz=timezone.utc).strftime("%H:%M")
            print(f"  chunk [{cur_str} → {nxt_str} UTC]: {len(chunk)} bills"
                  + ("  *** HIT 20k CAP — bills may have been dropped, lower --chunk-hours ***"
                     if len(chunk) >= 20000 else ""))
            out.extend(chunk)
            cur = nxt
    return out


def upsert_bills(
    db_path: Path, bills: list[dict], registered: list[str],
) -> tuple[int, int, int]:
    """Insert OR IGNORE bills into trades_okx; then UPDATE all rows' strategy_id
    using the current registered mapping (idempotent re-mapping).

    Returns (inserted_new, rows_remapped, total_bills_seen).

    Why the UPDATE pass: clOrdId stratidx is stable per bill (encoded at order
    creation time per the then-current yaml), but historical inserts might have
    been done with a stale registered list (e.g. before basis_arb was disabled).
    Re-running this script with up-to-date registered list will correct them.
    """
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

        # Re-map strategy_id for ALL rows using current registered list.
        # This corrects any prior runs where the list was different (e.g.
        # before a disable / re-order).
        remapped = 0
        cur = conn.execute("SELECT bill_id, cl_ord_id FROM trades_okx WHERE cl_ord_id IS NOT NULL")
        rows = cur.fetchall()
        for bid, coid in rows:
            idx = parse_strategy_index(coid)
            sid = map_strategy_id(idx, registered)
            r = conn.execute(
                "UPDATE trades_okx SET strategy_id = ? WHERE bill_id = ? AND "
                "(strategy_id IS NULL OR strategy_id != ?)",
                (sid, bid, sid),
            )
            if r.rowcount > 0:
                remapped += 1
        conn.commit()
    finally:
        conn.close()
    return inserted, remapped, len(bills)


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
    ap.add_argument("--chunk-hours", type=int, default=24,
                    help="按多少小时切窗口分段拉 bills（默认 24h/天）；高频日（>20k/天）"
                         "降到 12 或 6 避免单 chunk hit 20k cap")
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

    print(f"fetching bills in {args.chunk_hours}h chunks...")
    bills = asyncio.run(fetch_bills(begin_ms, end_ms, chunk_hours=args.chunk_hours))
    print(f"fetched {len(bills)} bills from OKX (deduped on bill_id by INSERT OR IGNORE)")

    inserted, remapped, total = upsert_bills(args.db, bills, registered)
    print(f"inserted {inserted}/{total} new rows; re-mapped strategy_id for {remapped} pre-existing rows")

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
