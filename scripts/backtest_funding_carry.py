"""funding_carry 策略 1 年收益估算（独立于 NT 回测引擎）。

为什么不直接用 ``scripts/backtest.py`` ？

NT 默认 SimulatedExchange 不模拟永续合约的 funding cashflow——funding 收入是
OKX 永续机制独有，每 8h 按持仓 × rate 结算到账户余额。两腿 delta-neutral 的话价格
PnL 互相抵消，最终账户余额几乎不变；策略真实 alpha 体现不出来。

本脚本绕开 NT，按策略决策直接累计 funding cashflow：
1. 拉 1 年 BTC-USDT-SWAP funding-rate-history（每 8h 一条）；
2. 模拟 ``funding_carry_decision``（entry_apr_threshold / exit_apr_threshold）的状态机；
3. 持仓期间按 ``max_position_pct × equity`` notional 累计 funding 收入；
4. 不含交易成本、不含资金费时刻外的 spot/perp 价格 PnL（delta-neutral 假设近似为 0）。

输出 PnL / APR 估算，给用户一个 concrete 的 1 年数字。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.strategies.funding_carry import (  # noqa: E402
    CarryAction,
    FundingCarryParams,
    funding_carry_decision,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="funding_carry 1-year cashflow estimator")
    p.add_argument("--perp-symbol", default="BTC-USDT-SWAP", help="永续合约 instId")
    p.add_argument("--total", type=int, default=1095,
                   help="拉取 funding rate 条数（默认 1095 = 1 年 × 3 次/天）")
    p.add_argument("--equity", type=float, default=10000.0)
    p.add_argument("--max-position-pct", type=float, default=0.30)
    p.add_argument("--entry-apr-threshold", type=float, default=0.08)
    p.add_argument("--exit-apr-threshold", type=float, default=0.02)
    p.add_argument("--fee-bps", type=float, default=0.0,
                   help="taker 手续费率 (bps)，每次进/出仓 4 legs（spot in/out + perp in/out）")
    return p.parse_args()


async def _main(args: argparse.Namespace) -> None:
    print(f"[1/3] downloading {args.total} funding-rate entries for {args.perp_symbol}...")
    async with OKXRestClient(OKXSettings()) as client:
        rates = await client.public.get_funding_rate_history_extended(
            args.perp_symbol, total=args.total,
        )
    if not rates:
        print("ERROR: no funding rates returned"); return

    print(f"        got {len(rates)} entries: "
          f"{rates[0].funding_time} ... {rates[-1].funding_time}")

    # 转 ms 时间戳，最早 -> 最晚
    span_ms = rates[-1].funding_time - rates[0].funding_time
    span_days = span_ms / 86_400_000

    print(f"[2/3] simulating decision state machine "
          f"(entry={args.entry_apr_threshold:.0%} APR, exit={args.exit_apr_threshold:.0%} APR)...")
    params = FundingCarryParams(
        entry_apr_threshold=args.entry_apr_threshold,
        exit_apr_threshold=args.exit_apr_threshold,
        max_position_pct=args.max_position_pct,
    )

    has_position = False
    total_funding_pnl = 0.0
    notional = args.equity * args.max_position_pct  # USDT
    n_enters = n_exits = 0
    n_settlements_in_pos = 0
    n_settlements_total = 0
    cumulative_pos_pnl: list[float] = []
    pos_pnl_running = 0.0

    for r in rates:
        rate_8h = float(r.funding_rate)
        n_settlements_total += 1
        # carry：现货多 + 永续空。funding rate > 0 时多头给空头付钱 → carry 赚
        if has_position:
            n_settlements_in_pos += 1
            pnl_this_period = notional * rate_8h
            total_funding_pnl += pnl_this_period
            pos_pnl_running += pnl_this_period
            cumulative_pos_pnl.append(pos_pnl_running)

        action = funding_carry_decision(rate_8h, has_position, params)
        if action == CarryAction.ENTER:
            has_position = True
            n_enters += 1
        elif action == CarryAction.EXIT:
            has_position = False
            n_exits += 1

    print(f"        settlements_total={n_settlements_total} "
          f"in_position={n_settlements_in_pos} "
          f"enters={n_enters} exits={n_exits}")

    # 手续费扣减：每次完整 round-trip = 4 legs (spot 进/出 + perp 进/出)
    n_round_trips = min(n_enters, n_exits)
    fee_cost = 0.0
    if args.fee_bps > 0:
        # 每条 leg 都按 notional × fee_bps/1e4；4 legs / round-trip
        fee_cost = n_round_trips * 4 * notional * args.fee_bps / 10000.0
        # 若还在持仓中（最后一笔 enter 未配对 exit），也算开仓 2 legs
        if n_enters > n_exits:
            fee_cost += 2 * notional * args.fee_bps / 10000.0
    net_funding_pnl = total_funding_pnl - fee_cost

    # 年化（按实际跨度日历日 → 年）
    apr = (net_funding_pnl / args.equity) / span_days * 365 if span_days > 0 else 0.0
    gross_apr = (total_funding_pnl / args.equity) / span_days * 365 if span_days > 0 else 0.0
    in_pos_pct = n_settlements_in_pos / n_settlements_total if n_settlements_total else 0

    print("\n=== RESULT ===")
    print(f"span_days       = {span_days:.1f}")
    print(f"notional        = {notional:.0f} USDT  ({args.max_position_pct:.0%} of {args.equity:.0f})")
    print(f"gross_funding   = {total_funding_pnl:+.2f} USDT  "
          f"({total_funding_pnl/args.equity:+.2%} of equity)")
    if args.fee_bps > 0:
        print(f"fee_cost        = {fee_cost:.2f} USDT  "
              f"({n_round_trips} round-trips × 4 legs × {args.fee_bps:.1f} bps)")
        print(f"net_funding_pnl = {net_funding_pnl:+.2f} USDT  "
              f"({net_funding_pnl/args.equity:+.2%} of equity)")
        print(f"gross_apr       = {gross_apr:+.2%}")
        print(f"net_apr         = {apr:+.2%}  (over {span_days:.0f} days)")
    else:
        print(f"annualized_apr  = {apr:+.2%}  (over {span_days:.0f} days)")
    print(f"in_position     = {in_pos_pct:.1%} of settlement points")
    print(f"trades          = {n_enters} enter + {n_exits} exit = {n_enters + n_exits} legs (each = 2 NT orders)")
    print()
    if args.fee_bps > 0:
        print(f"注：已扣 taker {args.fee_bps:.1f} bps × 4 legs/round-trip。"
              f"未含滑点；假设两腿 delta-neutral，价格 PnL 残差近似 0。")
    else:
        print("注：未含交易成本（taker 0.05% × 4 legs/round-trip）+ 滑点。"
              "假设两腿 delta-neutral，价格 PnL 互相抵消（实际有 0.1-0.3% 残差）。")


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
