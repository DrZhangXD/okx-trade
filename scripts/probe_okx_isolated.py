"""One-shot smoke test: set leverage to isolated for DOT-USDT-SWAP at lever=3,
verify OKX accepts it under the current pos_side_mode=net configuration.

Usage:
    .venv/bin/python scripts/probe_okx_isolated.py

Exits non-zero if set-leverage fails or returned mgnMode != isolated.
"""
from __future__ import annotations

import asyncio
import sys

from okx_trade import OKXRestClient, OKXSettings
from okx_trade.enums import TdMode

INST_ID = "DOT-USDT-SWAP"
TEST_LEVER = 3


async def main() -> int:
    settings = OKXSettings()
    async with OKXRestClient(settings) as client:
        try:
            await client.account.set_leverage(
                inst_id=INST_ID,
                leverage=TEST_LEVER,
                mgn_mode=TdMode.ISOLATED,
                pos_side=None,  # net mode → account.py auto-sets posSide=net
            )
        except Exception as exc:
            print(f"FAIL set_leverage: {exc}")
            return 1

        # Verify via GET /api/v5/account/leverage-info
        data = await client.transport.request(
            "GET", "/api/v5/account/leverage-info",
            params={"instId": INST_ID, "mgnMode": "isolated"},
            private=True, group=None,
        )
        if not data:
            print("FAIL: leverage-info returned empty")
            return 1
        for row in data:
            print(f"  {row}")
            if row.get("mgnMode") != "isolated":
                print(f"FAIL: row mgnMode={row.get('mgnMode')} != isolated")
                return 1
            if str(row.get("lever")) != str(TEST_LEVER):
                print(f"FAIL: row lever={row.get('lever')} != {TEST_LEVER}")
                return 1
        print(f"PASS: {INST_ID} set to isolated lever={TEST_LEVER} (net mode)")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
