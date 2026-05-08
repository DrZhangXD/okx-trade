"""WS 订阅示例：订阅 BTC-USDT-SWAP books5，打印前 10 条 update。

跑法::

    python examples/ws_subscribe.py
"""
from __future__ import annotations

import asyncio

from okx_trade import OKXSettings, OKXWSClient


async def main() -> None:
    settings = OKXSettings()
    async with OKXWSClient(settings) as ws:
        async with ws.subscribe("books5", instId="BTC-USDT-SWAP") as stream:
            count = 0
            async for event in stream:
                data = event.get("data") or []
                if not data:
                    continue
                top = data[0]
                bid = top.get("bids", [["?"]])[0][0]
                ask = top.get("asks", [["?"]])[0][0]
                print(f"[books5] bid={bid} ask={ask}")
                count += 1
                if count >= 10:
                    break


if __name__ == "__main__":
    asyncio.run(main())
