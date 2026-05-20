"""一次性诊断脚本：拉 OKX account bills 窗口流水，定位 2026-05-19 23:40~00:40 UTC 期间
账户从 58874 → 55775 USDT 暴跌 3099 的来源。

用法：
    .venv/bin/python scripts/diag_account_bills.py

环境：复用 .env 里的 OKX_* 凭证（paper 模式 IS_DEMO=true）。

输出：
    1. 按 type + subType 汇总的流水（按总 balChg 降序）
    2. 单笔 |balChg| 最大的 10 条明细
    3. 按 instId 汇总该窗口的净流水
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from okx_trade import OKXRestClient, OKXSettings

# 调查窗口：23:30 UTC 2026-05-19 → 01:30 UTC 2026-05-20（向前后各扩 1h）
WINDOW_BEGIN_MS = 1779231000000  # 2026-05-19 22:50:00 UTC（稍前一点抓上下文）
WINDOW_END_MS   = 1779240600000  # 2026-05-20 01:30:00 UTC

# OKX bills type 字典
BILL_TYPES = {
    "1": "Transfer", "2": "Trade", "3": "Delivery", "4": "Auto conv",
    "5": "Liquidation", "6": "Margin transfer", "7": "Interest",
    "8": "Funding fee", "9": "ADL", "10": "Clawback",
    "11": "Sys token conv", "12": "Strategy transfer", "13": "ddh",
    "14": "Settlement", "15": "Block trade", "18": "Profit sharing exp",
}


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def fetch_all_bills(client: OKXRestClient) -> list[dict]:
    """分页拉 [begin, end] 窗口所有 bills。OKX 单页最多 100 条，向后翻 (after=旧 billId)。"""
    out: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict[str, str] = {
            "begin": str(WINDOW_BEGIN_MS),
            "end":   str(WINDOW_END_MS),
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
        pages += 1
        if len(data) < 100:
            break
        after = data[-1]["billId"]
        if pages > 30:
            print(f"WARN: stopped after {pages} pages (safety cap)")
            break
    print(f"fetched {len(out)} bills across {pages} page(s)")
    return out


async def main() -> None:
    print(f"window: {fmt_ts(WINDOW_BEGIN_MS)} → {fmt_ts(WINDOW_END_MS)} UTC\n")
    settings = OKXSettings()
    async with OKXRestClient(settings) as client:
        bills = await fetch_all_bills(client)

    if not bills:
        print("no bills in window")
        return

    # ===== aggregate by (type, subType) =====
    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "balChg": Decimal(0), "pnl": Decimal(0), "fee": Decimal(0)},
    )
    for b in bills:
        key = (b.get("type", ""), b.get("subType", ""))
        a = agg[key]
        a["count"] += 1
        a["balChg"] += Decimal(b.get("balChg") or "0")
        a["pnl"]    += Decimal(b.get("pnl")    or "0")
        a["fee"]    += Decimal(b.get("fee")    or "0")

    print("\n=== by (type / subType) — sorted by |balChg| desc ===")
    print(f"{'type':<22} {'sub':<5} {'n':>5} {'balChg':>14} {'pnl':>14} {'fee':>10}")
    for (t, st), a in sorted(agg.items(), key=lambda kv: abs(kv[1]["balChg"]), reverse=True):
        tname = BILL_TYPES.get(t, f"type{t}")
        print(f"{tname:<22} {st:<5} {a['count']:>5}"
              f" {float(a['balChg']):>14.4f} {float(a['pnl']):>14.4f}"
              f" {float(a['fee']):>10.4f}")

    total_balchg = sum((Decimal(b.get("balChg") or "0") for b in bills), Decimal(0))
    total_pnl    = sum((Decimal(b.get("pnl")    or "0") for b in bills), Decimal(0))
    total_fee    = sum((Decimal(b.get("fee")    or "0") for b in bills), Decimal(0))
    print(f"\nTOTAL balChg = {float(total_balchg):+.4f}  pnl = {float(total_pnl):+.4f}  fee = {float(total_fee):+.4f}")

    # ===== top 10 single biggest |balChg| =====
    print("\n=== top 10 single bills by |balChg| ===")
    top = sorted(bills, key=lambda b: abs(Decimal(b.get("balChg") or "0")), reverse=True)[:10]
    print(f"{'ts (UTC)':<20} {'type':<18} {'sub':<5} {'instId':<22} {'balChg':>12} {'pnl':>12} {'fee':>10}")
    for b in top:
        ts = fmt_ts(int(b.get("ts", "0")))
        tname = BILL_TYPES.get(b.get("type", ""), f"t{b.get('type','')}")
        st = b.get("subType", "")
        inst = b.get("instId", "") or "-"
        bc = float(Decimal(b.get("balChg") or "0"))
        pn = float(Decimal(b.get("pnl") or "0"))
        fe = float(Decimal(b.get("fee") or "0"))
        print(f"{ts:<20} {tname:<18} {st:<5} {inst:<22} {bc:>+12.4f} {pn:>+12.4f} {fe:>+10.4f}")

    # ===== by instId =====
    per_inst: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for b in bills:
        per_inst[b.get("instId") or "-"] += Decimal(b.get("balChg") or "0")
    print("\n=== net balChg by instId — sorted by |net| desc ===")
    print(f"{'instId':<22} {'net balChg':>14}")
    for inst, net in sorted(per_inst.items(), key=lambda kv: abs(kv[1]), reverse=True):
        if abs(net) < Decimal("0.01"):
            continue
        print(f"{inst:<22} {float(net):>+14.4f}")


if __name__ == "__main__":
    asyncio.run(main())
