"""启动前 reconcile:用 reduce-only 市价单清空 OKX 所有 open positions。

为什么需要这步
-------------
- okx-trade.service 重启会重置策略内部状态(`_active_direction` / `_target_contracts` /
  `_position_contracts`),但 OKX 端的 stale 仓位不会消失。
- NT 自带的 reconciliation 只同步 cache,不会主动平掉 orphan 仓位。
- 历史教训(2026-05):demo 账户因 stale 仓位锁满 5209 USDT margin
  → free=0 → 所有新单 51008 → 整个系统瘫痪。手动平仓只治标。

行为
----
1. 读 ``OKXSettings``(env / .env)。
2. ``GET /api/v5/account/positions`` 列出所有持仓。
3. 对每个 ``is_open=True`` 的 position,调 ``POST /api/v5/trade/close-position``
   走 OKX 内置的市价平仓 API(reduce-only 语义、自动选 posSide)。
4. 退出码:

   - ``0`` —— 全部成功(包括 no-op 没仓可平);
   - ``1`` —— 部分失败(systemd 用 ``ExecStartPre=-`` 不阻塞 ExecStart);
   - ``2`` —— 连不上 OKX(网络 / 凭证错误)。

安全开关
--------
- ``--require-demo``(默认 ON):仅在 ``OKX_IS_DEMO=true`` 时执行,实盘环境
  会拒绝运行避免误平用户持仓。要在实盘跑须 ``--no-require-demo`` 显式确认。
- ``--dry-run``:列出会平掉的 position,但不实际下单。

用法
----
::

    # 本地预演
    python scripts/reconcile_okx_positions.py --dry-run

    # 生产 systemd 调:
    ExecStartPre=-/path/to/.venv/bin/python scripts/reconcile_okx_positions.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from okx_trade import OKXRestClient, OKXSettings, PosSide, TdMode

logger = logging.getLogger("reconcile")


EXIT_OK = 0
EXIT_PARTIAL_FAIL = 1
EXIT_CONNECT_FAIL = 2
EXIT_REFUSED_LIVE = 3


async def reconcile(*, dry_run: bool, require_demo: bool) -> int:
    settings = OKXSettings()
    if require_demo and not settings.is_demo:
        logger.error(
            "OKX_IS_DEMO != true; refusing to run on live account. "
            "Re-run with --no-require-demo if intentional."
        )
        return EXIT_REFUSED_LIVE

    logger.info(
        f"reconcile starting: is_demo={settings.is_demo} dry_run={dry_run}"
    )

    try:
        async with OKXRestClient(settings) as client:
            try:
                positions = await client.account.get_positions()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"failed to query positions: {exc}")
                return EXIT_CONNECT_FAIL

            open_positions = [p for p in positions if p.is_open]
            if not open_positions:
                logger.info("no open positions on OKX, nothing to reconcile")
                return EXIT_OK

            logger.info(f"found {len(open_positions)} open position(s) to close:")
            for p in open_positions:
                logger.info(
                    f"  {p.inst_id} pos={p.pos} side={p.pos_side} "
                    f"mode={p.mgn_mode} upl={p.upl}"
                )

            if dry_run:
                logger.info("--dry-run: skipping actual close")
                return EXIT_OK

            failed = 0
            for p in open_positions:
                try:
                    pos_side = PosSide(p.pos_side)
                    mgn_mode = TdMode(p.mgn_mode)
                except ValueError as exc:
                    logger.error(
                        f"cannot map OKX values for {p.inst_id}: {exc}; skipping"
                    )
                    failed += 1
                    continue
                try:
                    result = await client.trade.close_position(
                        p.inst_id, pos_side=pos_side, mgn_mode=mgn_mode,
                    )
                    logger.info(
                        f"closed {p.inst_id} {p.pos_side}: "
                        f"clOrdId={result.get('clOrdId', '?')}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"failed to close {p.inst_id} {p.pos_side}: {exc}")
                    failed += 1

            if failed:
                logger.warning(
                    f"{failed}/{len(open_positions)} close orders failed"
                )
                return EXIT_PARTIAL_FAIL
            logger.info("all positions reconciled")
            return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        logger.error(f"unexpected error: {exc}")
        return EXIT_CONNECT_FAIL


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Close all open OKX positions before service start (reduce-only)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list positions but don't close",
    )
    parser.add_argument(
        "--require-demo", dest="require_demo", action="store_true", default=True,
        help="refuse to run unless OKX_IS_DEMO=true (default ON for safety)",
    )
    parser.add_argument(
        "--no-require-demo", dest="require_demo", action="store_false",
        help="allow running on live account (DANGEROUS)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s reconcile: %(message)s",
    )
    return asyncio.run(reconcile(
        dry_run=args.dry_run, require_demo=args.require_demo,
    ))


if __name__ == "__main__":
    sys.exit(main())
