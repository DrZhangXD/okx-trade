"""验证 -3099 USDT 是 MTM 浮亏：
1. 拉当前 open positions + 总 upl 看现状
2. 拉 BTC/ETH/SOL/AVAX/TON/LTC/ADA/BNB/DOGE/DOT/LINK/XRP 1h candles
   覆盖 2026-05-19 22:00 → 2026-05-20 03:00 UTC，看价格变化"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from okx_trade import OKXRestClient, OKXSettings

COINS = ["BTC", "ETH", "SOL", "AVAX", "TON", "LTC", "ADA", "BNB", "DOGE", "DOT", "LINK", "XRP"]


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


async def main() -> None:
    settings = OKXSettings()
    async with OKXRestClient(settings) as client:
        # --- 1) current balance + positions ---
        bal = await client.account.get_balance()
        usdt = bal.get("USDT")
        print("=== current account balance ===")
        print(f"  total_eq={bal.total_eq}  adj_eq={bal.adj_eq}")
        if usdt:
            print(f"  USDT eq={usdt.eq}  cash={usdt.cash_bal}  avail={usdt.avail_bal}  upl={usdt.upl}")

        positions = await client.account.get_positions()
        opens = [p for p in positions if p.is_open]
        print(f"\n=== open positions: {len(opens)} ===")
        total_upl = Decimal(0)
        total_abs_pos_value = Decimal(0)
        print(f"{'instId':<22} {'side':<6} {'pos(张)':>10} {'avg_px':>12} {'upl':>10} {'upl%':>8} {'margin':>10}")
        for p in sorted(opens, key=lambda x: abs(Decimal(str(x.upl))), reverse=True):
            inst = p.inst_id
            side = p.pos_side
            pos_qty = p.pos
            avg = p.avg_px
            upl = p.upl
            uplr = p.upl_ratio
            margin = p.margin
            total_upl += upl
            total_abs_pos_value += abs(pos_qty * avg)
            print(f"{inst:<22} {side:<6} {float(pos_qty):>10.4f} {float(avg):>12.4f}"
                  f" {float(upl):>+10.2f} {float(uplr)*100:>+7.2f}% {float(margin):>10.2f}")
        print(f"\nTOTAL: |pos×avg_px|={float(total_abs_pos_value):.2f}  net_upl={float(total_upl):+.2f}")

        # --- 2) 1h candles for top coins covering 22:00 UTC 5/19 → 03:00 UTC 5/20 ---
        print("\n=== 1H candles per coin (close & %change vs prev close) ===")
        print(f"{'coin':<6} {'window'} {'open':>10} {'high':>10} {'low':>10} {'close':>10}  {'%vs 22:00':>10}")
        for coin in COINS:
            inst = f"{coin}-USDT-SWAP"
            try:
                # 拉 8 根 1h candle 包住窗口
                candles = await client.market.get_candles_extended(inst, "1H", total=8)
            except Exception as exc:  # noqa: BLE001
                print(f"{coin}: error {exc}")
                continue
            # candles 按 ts 倒序（newest first）
            in_window = [c for c in candles if 1779231600000 <= int(c.ts) <= 1779246000000]
            in_window.sort(key=lambda c: int(c.ts))
            if not in_window:
                continue
            base_close = float(in_window[0].close)
            for c in in_window:
                pct = (float(c.close) - base_close) / base_close * 100
                print(f"{coin:<6} {fmt_ts(int(c.ts)):<12}"
                      f" {float(c.open):>10.4f} {float(c.high):>10.4f}"
                      f" {float(c.low):>10.4f} {float(c.close):>10.4f}"
                      f" {pct:>+9.2f}%")
            print()


if __name__ == "__main__":
    asyncio.run(main())
