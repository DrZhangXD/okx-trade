"""启动前 reconcile:取消 pending 单 + 用 reduce-only 市价单清空所有 open positions。

为什么需要这步
-------------
- okx-trade.service 重启会重置策略内部状态(`_active_direction` / `_target_contracts` /
  `_position_contracts`),但 OKX 端的 stale 仓位 / pending 单不会消失。
- NT 自带的 reconciliation 只同步 cache,不会主动平掉 orphan 仓位、也不会取消遗留
  pending 单。后者会让 NT 启动时把"已 ACCEPTED 未成交"的旧单识别成幻仓,触发
  XSMomentum 等策略在下次 rebalance 时发 reduce-only 单 → OKX 返 sCode=51169。
- 历史教训(2026-05):demo 账户因 stale 仓位锁满 5209 USDT margin → free=0 →
  所有新单 51008 → 系统瘫痪。手动平仓只治标。

行为(顺序很重要)
-----------------
1. ``GET /api/v5/trade/orders-pending``:列出所有还在 live / partially_filled 状态
   的单,逐个 ``POST /api/v5/trade/cancel-order`` 取消。先清单再清仓,否则刚平的仓
   可能被遗留 pending 单立刻反向开回。
2. ``GET /api/v5/account/positions`` + ``POST /api/v5/trade/close-position``:
   对每个 ``is_open=True`` 的 position 走 OKX 内置市价平仓(reduce-only 语义、
   自动选 posSide)。
3. 退出码:

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

已知副作用：HEDGING 模式下的 "Received fill for closed position" 警告
---------------------------------------------------------------------
本脚本通过 ``/api/v5/trade/close-position`` 直接下平仓单，这条路径绕开
NT 自己的订单 / 仓位 cache。restart 后 NT 的 ExecEngine 经过 external
reconciliation 拉取 OKX 真实仓位，但同时也会收到本脚本平仓单产生的
fill 事件 —— 对 NT cache 而言，这些 fill 对应"已经被 reconcile 视为关闭"
的 position，NT 因此在日志里打：

    [WARN] ExecEngine: Received fill for closed position X-USDT-SWAP.OKX-EXTERNAL
    in HEDGING mode; creating new position and ignoring previous state

警告本身无害（NT 会创建新 position 跟踪 fill；reconciliation 完成后状态
对齐）。频次：每次 restart 集中爆几十条；steady-state 为 0。如要彻底
消除，需要把 close-position 改成走 NT 自己的 submit_order(reduce_only=True)
路径——但那就失去本脚本"在 NT 起来之前清干净"的解耦设计。
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
            # === 阶段 1: 取消所有 pending 单 ===
            try:
                pending = await client.trade.get_pending_orders()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"failed to query pending orders: {exc}")
                return EXIT_CONNECT_FAIL

            cancel_failed = 0
            if pending:
                logger.info(f"found {len(pending)} pending order(s) to cancel:")
                for o in pending:
                    logger.info(
                        f"  {o.get('instId')} ordId={o.get('ordId')} "
                        f"side={o.get('side')} sz={o.get('sz')} state={o.get('state')}"
                    )
                if not dry_run:
                    for o in pending:
                        inst_id = o.get("instId", "")
                        ord_id = o.get("ordId", "")
                        if not inst_id or not ord_id:
                            cancel_failed += 1
                            continue
                        try:
                            await client.trade.cancel_order(
                                inst_id=inst_id, ord_id=ord_id,
                            )
                            logger.info(f"canceled {inst_id} ordId={ord_id}")
                        except Exception as exc:  # noqa: BLE001
                            # 已 fill / 已 cancel / 已 expire 的单可能返错,不致命
                            logger.warning(
                                f"cancel failed for {inst_id} ordId={ord_id}: {exc}"
                            )
                            cancel_failed += 1
                else:
                    logger.info("--dry-run: skipping actual cancels")
            else:
                logger.info("no pending orders, skipping cancel step")

            # === 阶段 2: 平掉所有 open positions ===
            try:
                positions = await client.account.get_positions()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"failed to query positions: {exc}")
                return EXIT_CONNECT_FAIL

            open_positions = [p for p in positions if p.is_open]
            if not open_positions:
                if pending and cancel_failed == 0:
                    logger.info(
                        f"all {len(pending)} pending orders canceled; "
                        "no open positions to close"
                    )
                else:
                    logger.info("no open positions on OKX, nothing to close")
                return EXIT_PARTIAL_FAIL if cancel_failed else EXIT_OK

            logger.info(f"found {len(open_positions)} open position(s) to close:")
            for p in open_positions:
                logger.info(
                    f"  {p.inst_id} pos={p.pos} side={p.pos_side} "
                    f"mode={p.mgn_mode} upl={p.upl}"
                )

            if dry_run:
                logger.info("--dry-run: skipping actual close")
                return EXIT_OK

            close_failed = 0
            for p in open_positions:
                try:
                    pos_side = PosSide(p.pos_side)
                    mgn_mode = TdMode(p.mgn_mode)
                except ValueError as exc:
                    logger.error(
                        f"cannot map OKX values for {p.inst_id}: {exc}; skipping"
                    )
                    close_failed += 1
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
                    close_failed += 1

            total_failed = cancel_failed + close_failed
            if total_failed:
                logger.warning(
                    f"{cancel_failed} cancel + {close_failed} close failures"
                )
                return EXIT_PARTIAL_FAIL
            logger.info("all pending orders canceled, all positions reconciled")
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
