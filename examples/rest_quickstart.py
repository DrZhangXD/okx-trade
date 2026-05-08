"""REST 快速上手：拉 ticker、查余额。

使用前在项目根目录创建 ``.env``（参考 ``.env.example``），至少要配:
- OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE （demo 环境凭证）
- OKX_HTTP_PROXY / OKX_HTTPS_PROXY （国内访问必备）
- OKX_IS_DEMO=true

跑法::

    python examples/rest_quickstart.py
"""
from __future__ import annotations

import asyncio

from okx_trade import OKXRestClient, OKXSettings


async def main() -> None:
    settings = OKXSettings()  # 自动从 .env 加载
    async with OKXRestClient(settings) as client:
        # 1. 公共行情：BTC-USDT-SWAP 最新 ticker（无需鉴权）
        ticker = await client.market.get_ticker("BTC-USDT-SWAP")
        print(f"[ticker] BTC-USDT-SWAP last={ticker.last} ts={ticker.ts}")

        # 2. 公共行情：最近 5 根 1m K 线
        candles = await client.market.get_candles("BTC-USDT-SWAP", bar="1m", limit=5)
        for c in candles:
            print(f"[candle] {c.ts} O={c.open} H={c.high} L={c.low} C={c.close} V={c.volume}")

        # 3. 私有：账户余额
        if not settings.has_credentials():
            print("(未配置凭证，跳过私有调用)")
            return
        balance = await client.account.get_balance()
        for ccy in ("USDT", "BTC"):
            d = balance.get(ccy)
            if d:
                print(f"[balance] {ccy} eq={d.eq} avail={d.avail_eq}")


if __name__ == "__main__":
    asyncio.run(main())
