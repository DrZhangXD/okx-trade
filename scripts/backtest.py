"""命令行回测工具。

支持的策略：

- ``range_breakout``：单标的 1H 信号 + 1D 区间。
  ::

      python scripts/backtest.py \\
          --strategy range_breakout \\
          --symbol BTC-USDT-SWAP \\
          --signal-bar 1H --range-bar 1D \\
          --total-bars 8760

- ``xs_momentum``：多标的 1D 横截面动量。
  ::

      python scripts/backtest.py \\
          --strategy xs_momentum \\
          --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \\
          --signal-bar 1D --total-bars 365

会自动：
1. 调 OKX REST 拉历史 K 线（公共端点不需要凭证）；
2. 写到本地 ParquetDataCatalog（``./data`` 目录，可复用）；
3. 跑 NT BacktestNode；
4. 打印 PnL / Sharpe / maxDD / 交易次数 摘要。

需要 ``pip install -e ".[strategy,dev]"``。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许从仓库根直接 ``python scripts/backtest.py`` 跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from okx_trade import OKXRestClient, OKXSettings  # noqa: E402
from okx_trade.adapter.constants import OKX_VENUE  # noqa: E402
from okx_trade.adapter.parsing import make_bar_type, parse_okx_instrument  # noqa: E402
from okx_trade.backtest import (  # noqa: E402
    bars_to_nt_bars,
    build_okx_venue_config,
    download_historical_bars,
    extract_equity_curve,
    plot_equity_curve_html,
    run_backtest,
    run_backtest_with_node,
    write_bars_to_catalog,
)
from okx_trade.backtest.data_loader import prepare_backtest_catalog  # noqa: E402
from okx_trade.enums import InstType  # noqa: E402


SUPPORTED_STRATEGIES = {
    "range_breakout": (
        "okx_trade.strategies.range_breakout:RangeBreakoutStrategy",
        "okx_trade.strategies.range_breakout:RangeBreakoutConfig",
    ),
    "xs_momentum": (
        "okx_trade.strategies.xs_momentum:XSMomentumStrategy",
        "okx_trade.strategies.xs_momentum:XSMomentumConfig",
    ),
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OKX backtest runner (NautilusTrader)")
    p.add_argument("--strategy", required=True, choices=list(SUPPORTED_STRATEGIES.keys()))
    p.add_argument("--symbol", help="OKX instId（单标的策略用，e.g. BTC-USDT-SWAP）")
    p.add_argument("--instrument-ids",
                   help="多标的策略用，逗号分隔（e.g. BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP）")
    p.add_argument("--signal-bar", default="1H", help="信号 K 线周期，默认 1H")
    p.add_argument("--range-bar", default="1D", help="区间 K 线周期（仅 range_breakout 用），默认 1D")
    p.add_argument("--total-bars", type=int, default=2000, help="信号 K 线拉取根数")
    p.add_argument("--equity", type=float, default=10000.0, help="起始资金 USDT")
    p.add_argument("--risk-pct", type=float, default=0.005, help="单笔风险占净值")
    p.add_argument("--leverage", type=int, default=10, help="杠杆")
    p.add_argument("--catalog", default="./data", help="ParquetDataCatalog 路径")
    p.add_argument("--reuse-data", action="store_true",
                   help="复用 catalog 已有数据，跳过下载")
    p.add_argument("--plot", default=None,
                   help="保存净值曲线 HTML 到指定路径（plotly 交互图，缺省不绘图）")
    p.add_argument("--equity-csv", default=None,
                   help="同时导出 equity DataFrame 为 CSV（调试用，可选）")
    return p.parse_args()


async def _resolve_instrument(client: OKXRestClient, inst_id: str):
    """拉 instrument 规格并解析为 NT instrument（不写盘）。"""
    inst_type = InstType.SWAP if inst_id.endswith("-SWAP") else InstType.SPOT
    okx_inst = await client.public.get_instrument(inst_type, inst_id)
    return parse_okx_instrument(okx_inst, ts_init=0)


def _run_and_maybe_plot(
    args: argparse.Namespace,
    *,
    venue,
    data,
    strategies,
    plot_title: str,
):
    """跑回测；若 ``--plot`` / ``--equity-csv`` 任一启用，抓 node 抽 equity，输出对应文件。"""
    if args.plot or args.equity_csv:
        summary, node = run_backtest_with_node(
            venue=venue, data=data, strategies=strategies,
        )
        equity = extract_equity_curve(node, OKX_VENUE)
        if equity.empty:
            print("[plot] 警告：账户事件为空，跳过绘图/导出 CSV")
        else:
            if args.equity_csv:
                out_csv = Path(args.equity_csv).resolve()
                out_csv.parent.mkdir(parents=True, exist_ok=True)
                equity.to_csv(out_csv)
                print(f"[plot] equity CSV → {out_csv}")
            if args.plot:
                subtitle = (
                    f"PnL {summary.pnl_pct:+.2%} | "
                    f"Sharpe {summary.sharpe_ratio:.2f} | "
                    f"maxDD {summary.max_drawdown_pct:.2%} | "
                    f"win {summary.win_rate:.1%} | "
                    f"orders {summary.total_orders}"
                )
                out_html = plot_equity_curve_html(
                    equity,
                    output_path=args.plot,
                    title=plot_title,
                    starting_balance=args.equity,
                    subtitle=subtitle,
                )
                print(f"[plot] equity HTML → {out_html}")
        return summary
    return run_backtest(venue=venue, data=data, strategies=strategies)


# ---------------------------------------------------------------------------
# range_breakout：单标的 1H 信号 + 1D 区间
# ---------------------------------------------------------------------------


async def _run_range_breakout(args: argparse.Namespace) -> None:
    if not args.symbol:
        raise SystemExit("range_breakout 需要 --symbol")

    if not args.reuse_data:
        print(f"[1/3] downloading {args.total_bars} bars × ({args.signal_bar}, {args.range_bar})...")
        async with OKXRestClient(OKXSettings()) as client:
            inst, signal_bars = await prepare_backtest_catalog(
                client, args.symbol, args.signal_bar,
                total=args.total_bars, catalog_path=args.catalog,
            )
            range_total = max(50, args.total_bars // 24)
            range_candles = await download_historical_bars(
                client, args.symbol, args.range_bar, total=range_total,
            )
            range_bars = bars_to_nt_bars(range_candles, inst, args.range_bar)
            write_bars_to_catalog(args.catalog, inst, range_bars)
            print(f"        signal_bars={len(signal_bars)} range_bars={len(range_bars)}")
    else:
        print("[1/3] reusing catalog (--reuse-data)")
        async with OKXRestClient(OKXSettings()) as client:
            inst = await _resolve_instrument(client, args.symbol)

    print("[2/3] building backtest config...")
    from nautilus_trader.backtest.config import BacktestDataConfig
    from nautilus_trader.config import ImportableStrategyConfig
    from nautilus_trader.model.data import Bar

    instrument_id = f"{args.symbol}.{OKX_VENUE}"
    signal_bar_type = make_bar_type(args.symbol, args.signal_bar)
    range_bar_type = make_bar_type(args.symbol, args.range_bar)

    data_configs = [
        BacktestDataConfig(
            catalog_path=str(Path(args.catalog).resolve()),
            data_cls=Bar.fully_qualified_name(),
            instrument_id=instrument_id,
            bar_types=[str(signal_bar_type)],
        ),
        BacktestDataConfig(
            catalog_path=str(Path(args.catalog).resolve()),
            data_cls=Bar.fully_qualified_name(),
            instrument_id=instrument_id,
            bar_types=[str(range_bar_type)],
        ),
    ]

    venue = build_okx_venue_config(
        starting_balance_usdt=args.equity,
        leverage=args.leverage,
    )

    strategy_path, config_path = SUPPORTED_STRATEGIES["range_breakout"]
    strategy_config = ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=config_path,
        config={
            "instrument_id": instrument_id,
            "range_bar_type": str(range_bar_type),
            "signal_bar_type": str(signal_bar_type),
            "risk_pct": args.risk_pct,
            "account_equity_usdt": args.equity,
        },
    )

    print("[3/3] running backtest...")
    summary = _run_and_maybe_plot(
        args,
        venue=venue,
        data=data_configs,
        strategies=[strategy_config],
        plot_title=f"range_breakout — {args.symbol}",
    )
    print("\n=== RESULT ===")
    print(summary)


# ---------------------------------------------------------------------------
# xs_momentum：多标的 1D 横截面动量
# ---------------------------------------------------------------------------


async def _run_xs_momentum(args: argparse.Namespace) -> None:
    if not args.instrument_ids:
        raise SystemExit("xs_momentum 需要 --instrument-ids（逗号分隔）")
    inst_id_list = [s.strip() for s in args.instrument_ids.split(",") if s.strip()]
    if len(inst_id_list) < 2:
        raise SystemExit(f"xs_momentum 至少需要 2 个标的，得到 {len(inst_id_list)}")

    if not args.reuse_data:
        print(f"[1/3] downloading {args.total_bars} × {args.signal_bar} bars for "
              f"{len(inst_id_list)} instruments...")
        async with OKXRestClient(OKXSettings()) as client:
            for inst_id in inst_id_list:
                _, bars = await prepare_backtest_catalog(
                    client, inst_id, args.signal_bar,
                    total=args.total_bars, catalog_path=args.catalog,
                )
                print(f"        {inst_id}: {len(bars)} bars")
    else:
        print("[1/3] reusing catalog (--reuse-data)")

    print("[2/3] building backtest config...")
    from nautilus_trader.backtest.config import BacktestDataConfig
    from nautilus_trader.config import ImportableStrategyConfig
    from nautilus_trader.model.data import Bar

    catalog_path_str = str(Path(args.catalog).resolve())

    nt_instrument_ids = [f"{s}.{OKX_VENUE}" for s in inst_id_list]
    bar_types = [make_bar_type(s, args.signal_bar) for s in inst_id_list]

    data_configs = [
        BacktestDataConfig(
            catalog_path=catalog_path_str,
            data_cls=Bar.fully_qualified_name(),
            instrument_id=instrument_id,
            bar_types=[str(bar_type)],
        )
        for instrument_id, bar_type in zip(nt_instrument_ids, bar_types)
    ]

    venue = build_okx_venue_config(
        starting_balance_usdt=args.equity,
        leverage=args.leverage,
    )

    # bar_type_template："{inst}-1-DAY-LAST-EXTERNAL"，每个 inst 自己 replace
    sample_bar_str = str(bar_types[0])
    # sample 形如 BTC-USDT-SWAP.OKX-1-DAY-LAST-EXTERNAL；把前面 instrument_id 部分换成占位符
    template_suffix = sample_bar_str.split(".OKX-", 1)[1]  # "1-DAY-LAST-EXTERNAL"
    bar_type_template = "{inst}-" + template_suffix

    # 至少 4 个标的才能 top_n=2 + bot_n=2；少于 4 个把 top_n/bot_n 调到 1
    top_n = bot_n = 2 if len(inst_id_list) >= 4 else 1

    strategy_path, config_path = SUPPORTED_STRATEGIES["xs_momentum"]
    strategy_config = ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=config_path,
        config={
            "instrument_ids": tuple(nt_instrument_ids),
            "bar_type_template": bar_type_template,
            "top_n": top_n,
            "bot_n": bot_n,
            "risk_pct": args.risk_pct,
            "account_equity_usdt": args.equity,
        },
    )

    print(f"        universe={len(nt_instrument_ids)} top_n={top_n} bot_n={bot_n}")
    print("[3/3] running backtest...")
    summary = _run_and_maybe_plot(
        args,
        venue=venue,
        data=data_configs,
        strategies=[strategy_config],
        plot_title=f"xs_momentum — {','.join(inst_id_list)}",
    )
    print("\n=== RESULT ===")
    print(summary)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_RUNNERS = {
    "range_breakout": _run_range_breakout,
    "xs_momentum": _run_xs_momentum,
}


async def _main_async(args: argparse.Namespace) -> None:
    runner = _RUNNERS[args.strategy]
    await runner(args)


def main() -> None:
    args = _parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
