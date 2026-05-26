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
from okx_trade.enums import PosSide, TdMode

INST_ID = "DOT-USDT-SWAP"
TEST_LEVER = 3


async def main() -> int:
    settings = OKXSettings()
    print(f"is_demo={settings.is_demo}")
    async with OKXRestClient(settings) as client:
        # Step 0: check account config to confirm posMode
        try:
            cfg = await client.transport.request(
                "GET", "/api/v5/account/config",
                private=True, group=None,
            )
            pos_mode = cfg[0].get("posMode") if cfg else "unknown"
            print(f"account posMode={pos_mode!r}")
        except Exception as exc:
            print(f"WARN: could not fetch account config: {exc}")
            pos_mode = "unknown"

        # Step 1: determine correct posSide for set-leverage
        # OKX set-leverage isolated rules:
        #   posMode=net_mode       → posSide=net (or omit — behaviour varies)
        #   posMode=long_short_mode → posSide=long AND posSide=short (two calls)
        if pos_mode and "long_short" in pos_mode:
            sides_to_try: list[PosSide | None] = [PosSide.LONG, PosSide.SHORT]
        else:
            # net mode: account.py auto-defaults to posSide=net
            sides_to_try = [None]  # None → auto-net via account.py

        for side in sides_to_try:
            side_label = side.value if side else "net(auto)"
            try:
                await client.account.set_leverage(
                    inst_id=INST_ID,
                    leverage=TEST_LEVER,
                    mgn_mode=TdMode.ISOLATED,
                    pos_side=side,  # None → auto posSide=net for net-mode accounts
                )
                print(f"  set_leverage OK for posSide={side_label}")
            except Exception as exc:
                print(f"FAIL set_leverage posSide={side_label}: {exc}")
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
            print(f"  leverage-info row: {row}")
            if row.get("mgnMode") != "isolated":
                print(f"FAIL: row mgnMode={row.get('mgnMode')} != isolated")
                return 1
            if str(row.get("lever")) != str(TEST_LEVER):
                print(f"FAIL: row lever={row.get('lever')} != {TEST_LEVER}")
                return 1
        print(f"PASS: {INST_ID} set to isolated lever={TEST_LEVER} (posMode={pos_mode})")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
