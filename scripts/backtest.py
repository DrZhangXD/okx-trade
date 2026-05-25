"""命令行回测工具。

支持的策略：

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
    build_okx_venue_config,
    extract_equity_curve,
    plot_equity_curve_html,
    run_backtest,
    run_backtest_with_node,
)
from okx_trade.backtest.data_loader import prepare_backtest_catalog  # noqa: E402
from okx_trade.enums import InstType  # noqa: E402


SUPPORTED_STRATEGIES = {
    "xs_momentum": (
        "okx_trade.strategies.xs_momentum:XSMomentumStrategy",
        "okx_trade.strategies.xs_momentum:XSMomentumConfig",
    ),
    "funding_carry": (
        "okx_trade.strategies.funding_carry:FundingCarryStrategy",
        "okx_trade.strategies.funding_carry:FundingCarryConfig",
    ),
    "funding_cross_section": (
        "okx_trade.strategies.funding_cross_section:FundingXSStrategy",
        "okx_trade.strategies.funding_cross_section:FundingXSConfig",
    ),
    "funding_skew_momentum": (
        "okx_trade.strategies.funding_skew_momentum:FundingSkewStrategy",
        "okx_trade.strategies.funding_skew_momentum:FundingSkewConfig",
    ),
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OKX backtest runner (NautilusTrader)")
    p.add_argument("--strategy", required=True, choices=list(SUPPORTED_STRATEGIES.keys()))
    p.add_argument("--symbol", help="OKX instId（单标的策略用，e.g. BTC-USDT-SWAP）")
    p.add_argument("--instrument-ids",
                   help="多标的策略用，逗号分隔（e.g. BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP）")
    p.add_argument("--signal-bar", default="1H", help="信号 K 线周期，默认 1H")
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
    # xs_momentum 高级配置（不传则用 configs/strategies/xs_momentum.yaml 默认值）
    p.add_argument("--top-n", type=int, default=5,
                   help="xs_momentum 多腿数量（默认与实盘一致 = 5）")
    p.add_argument("--bot-n", type=int, default=5,
                   help="xs_momentum 空腿数量（默认与实盘一致 = 5）")
    p.add_argument("--max-inst-count", type=int, default=4,
                   help="xs_momentum 单次 rebalance 最大 inst 数（默认实盘 throttle = 4）")
    p.add_argument("--lookback-days", type=int, default=7,
                   help="xs_momentum 动量窗口天数（默认 7）")
    p.add_argument("--target-vol-annualized", type=float, default=0.15,
                   help="xs_momentum 目标年化波动率（默认 0.15）")
    # funding_carry / funding_cross_section / funding_skew / basis_arb shared flags
    p.add_argument("--spot-instrument-id", default=None,
                   help="现货 inst (funding_carry / basis_arb)")
    p.add_argument("--perp-instrument-id", default=None,
                   help="永续 inst (funding_carry / *_funding)")
    p.add_argument("--funding-total", type=int, default=1095,
                   help="funding rate 历史样本数（默认 1095 ≈ 1 年 × 3 次/天）")
    # funding_carry-specific
    p.add_argument("--entry-apr-threshold", type=float, default=0.08,
                   help="funding_carry 开仓 APR 阈值（默认 8%% APR）")
    p.add_argument("--exit-apr-threshold", type=float, default=0.02,
                   help="funding_carry 平仓 APR 阈值（默认 2%% APR）")
    p.add_argument("--max-position-pct", type=float, default=0.30,
                   help="funding_carry 单次开仓占净值比例（默认 30%%）")
    # 手续费模型（默认零费用，与历史行为兼容）
    p.add_argument("--taker-fee-bps", type=float, default=0.0,
                   help="taker 手续费率 (bps)。>0 时开启 NT MakerTakerFeeModel；"
                        "OKX 实盘 taker = 5 bps")
    p.add_argument("--maker-fee-bps", type=float, default=2.0,
                   help="maker 手续费率 (bps)，仅在 --taker-fee-bps > 0 时生效")
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
# xs_momentum：多标的 1D 横截面动量
# ---------------------------------------------------------------------------


async def _run_xs_momentum(args: argparse.Namespace) -> None:
    if not args.instrument_ids:
        raise SystemExit("xs_momentum 需要 --instrument-ids（逗号分隔）")
    inst_id_list = [s.strip() for s in args.instrument_ids.split(",") if s.strip()]
    if len(inst_id_list) < 2:
        raise SystemExit(f"xs_momentum 至少需要 2 个标的，得到 {len(inst_id_list)}")

    fee_kwargs = {
        "taker_fee_bps": args.taker_fee_bps,
        "maker_fee_bps": args.maker_fee_bps,
    } if args.taker_fee_bps > 0 else {}

    if not args.reuse_data:
        print(f"[1/3] downloading {args.total_bars} × {args.signal_bar} bars for "
              f"{len(inst_id_list)} instruments...")
        async with OKXRestClient(OKXSettings()) as client:
            for inst_id in inst_id_list:
                _, bars = await prepare_backtest_catalog(
                    client, inst_id, args.signal_bar,
                    total=args.total_bars, catalog_path=args.catalog,
                    **fee_kwargs,
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
        enable_fees=args.taker_fee_bps > 0,
    )

    # bar_type_template："{inst}-1-DAY-LAST-EXTERNAL"，每个 inst 自己 replace
    sample_bar_str = str(bar_types[0])
    # sample 形如 BTC-USDT-SWAP.OKX-1-DAY-LAST-EXTERNAL；把前面 instrument_id 部分换成占位符
    template_suffix = sample_bar_str.split(".OKX-", 1)[1]  # "1-DAY-LAST-EXTERNAL"
    bar_type_template = "{inst}-" + template_suffix

    # CLI flag 与 universe 大小做下限保护：top_n + bot_n 不能超过 universe
    top_n = min(args.top_n, max(1, len(inst_id_list) // 2))
    bot_n = min(args.bot_n, max(1, len(inst_id_list) // 2))

    strategy_path, config_path = SUPPORTED_STRATEGIES["xs_momentum"]
    strategy_config = ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=config_path,
        config={
            "instrument_ids": tuple(nt_instrument_ids),
            "bar_type_template": bar_type_template,
            "top_n": top_n,
            "bot_n": bot_n,
            "max_inst_count": args.max_inst_count,
            "lookback_days": args.lookback_days,
            "target_vol_annualized": args.target_vol_annualized,
            "risk_pct": args.risk_pct,
            "account_equity_usdt": args.equity,
        },
    )

    print(f"        universe={len(nt_instrument_ids)} top_n={top_n} bot_n={bot_n} "
          f"max_inst={args.max_inst_count} lookback={args.lookback_days}")
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
# funding_carry：spot + perp delta-neutral funding harvest
# ---------------------------------------------------------------------------


async def _run_funding_carry(args: argparse.Namespace) -> None:
    if not args.spot_instrument_id or not args.perp_instrument_id:
        raise SystemExit("funding_carry 需要 --spot-instrument-id 和 --perp-instrument-id")

    catalog_path = Path(args.catalog).resolve()
    catalog_path.mkdir(parents=True, exist_ok=True)

    if not args.reuse_data:
        print(f"[1/4] downloading bars + instrument specs for "
              f"{args.spot_instrument_id} / {args.perp_instrument_id}...")
        async with OKXRestClient(OKXSettings()) as client:
            spot_inst, spot_bars = await prepare_backtest_catalog(
                client, args.spot_instrument_id, args.signal_bar,
                total=args.total_bars, catalog_path=str(catalog_path),
            )
            # download perp instrument spec and write to catalog so the strategy
            # can resolve it from cache; we don't need perp bar data for this strategy
            from okx_trade.adapter.parsing import parse_okx_instrument
            from okx_trade.backtest.data_loader import write_instrument_to_catalog
            perp_inst_type = (
                InstType.SWAP if args.perp_instrument_id.endswith("-SWAP") else InstType.SPOT
            )
            okx_perp = await client.public.get_instrument(perp_inst_type, args.perp_instrument_id)
            perp_inst = parse_okx_instrument(okx_perp, ts_init=0)
            write_instrument_to_catalog(catalog_path, perp_inst)
            print(f"        spot={len(spot_bars)} bars, perp instrument registered in catalog")

            # Download funding rate history for the perp (writes to catalog/funding/<inst_id>/<YYYYMM>.parquet)
            # The strategy will auto-load via on_start's read_funding_parquet() call.
            from okx_trade.backtest.data_loader import prepare_funding_panel
            funding_panel = await prepare_funding_panel(
                client, args.perp_instrument_id,
                total=args.funding_total,
                catalog_path=catalog_path,
                reuse_cache=args.reuse_data,
            )
            print(f"        funding panel: {len(funding_panel.ts_ms)} samples")
    else:
        print("[1/4] reusing catalog (--reuse-data)")

    print("[2/4] building backtest config...")
    from nautilus_trader.backtest.config import BacktestDataConfig
    from nautilus_trader.config import ImportableStrategyConfig
    from nautilus_trader.model.data import Bar

    spot_bar_type = make_bar_type(args.spot_instrument_id, args.signal_bar)
    spot_nt_id = f"{args.spot_instrument_id}.{OKX_VENUE}"
    perp_nt_id = f"{args.perp_instrument_id}.{OKX_VENUE}"

    data_configs = [
        BacktestDataConfig(
            catalog_path=str(catalog_path),
            data_cls=Bar.fully_qualified_name(),
            instrument_id=spot_nt_id,
            bar_types=[str(spot_bar_type)],
        ),
    ]

    fee_kwargs = {
        "taker_fee_bps": args.taker_fee_bps,
        "maker_fee_bps": args.maker_fee_bps,
    } if args.taker_fee_bps > 0 else {}

    venue = build_okx_venue_config(
        starting_balance_usdt=args.equity,
        leverage=args.leverage,
        enable_fees=args.taker_fee_bps > 0,
        **fee_kwargs,
    )

    strategy_path, config_path = SUPPORTED_STRATEGIES["funding_carry"]
    strategy_config = ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=config_path,
        config={
            "spot_instrument_id": spot_nt_id,
            "perp_instrument_id": perp_nt_id,
            "spot_bar_type": str(spot_bar_type),
            "entry_apr_threshold": args.entry_apr_threshold,
            "exit_apr_threshold": args.exit_apr_threshold,
            "max_position_pct": args.max_position_pct,
            "account_equity_usdt": args.equity,
            "funding_panel_parquet_path": str(catalog_path),
        },
    )

    print(f"        spot_instrument={spot_nt_id} perp_instrument={perp_nt_id}")
    print(f"        entry_apr={args.entry_apr_threshold:.1%} "
          f"exit_apr={args.exit_apr_threshold:.1%} "
          f"max_pos={args.max_position_pct:.0%}")
    print("[3/4] running backtest...")
    summary = _run_and_maybe_plot(
        args,
        venue=venue,
        data=data_configs,
        strategies=[strategy_config],
        plot_title=f"funding_carry — {args.spot_instrument_id} / {args.perp_instrument_id}",
    )
    print("\n=== RESULT ===")
    print(summary)


# ---------------------------------------------------------------------------
# funding_cross_section：多标的 funding 横截面套利
# ---------------------------------------------------------------------------


async def _run_funding_cross_section(args: argparse.Namespace) -> None:
    if not args.instrument_ids:
        raise SystemExit("funding_cross_section 需要 --instrument-ids (逗号分隔)")
    inst_id_list = [s.strip() for s in args.instrument_ids.split(",") if s.strip()]
    if len(inst_id_list) < 2:
        raise SystemExit(f"funding_cross_section 至少需要 2 个标的，得到 {len(inst_id_list)}")

    catalog_path = Path(args.catalog).resolve()
    catalog_path.mkdir(parents=True, exist_ok=True)

    from okx_trade.backtest.data_loader import prepare_funding_panel

    if not args.reuse_data:
        print(f"[1/4] downloading bars + funding panels for {len(inst_id_list)} insts...")
    else:
        print("[1/4] reusing catalog (--reuse-data)")

    async with OKXRestClient(OKXSettings()) as client:
        for inst_id in inst_id_list:
            if not args.reuse_data:
                _, bars = await prepare_backtest_catalog(
                    client, inst_id, args.signal_bar,
                    total=args.total_bars, catalog_path=str(catalog_path),
                )
                print(f"        {inst_id}: {len(bars)} bars")
            # Always ensure funding panel is cached (function handles cache-or-download)
            panel = await prepare_funding_panel(
                client, inst_id, total=args.funding_total,
                catalog_path=catalog_path, reuse_cache=args.reuse_data,
            )
            if not args.reuse_data:
                print(f"        {inst_id}: funding={len(panel.ts_ms)} samples")

    print("[2/4] building backtest config...")
    from nautilus_trader.backtest.config import BacktestDataConfig
    from nautilus_trader.config import ImportableStrategyConfig
    from nautilus_trader.model.data import Bar

    nt_instrument_ids = [f"{s}.{OKX_VENUE}" for s in inst_id_list]
    bar_types = [make_bar_type(s, args.signal_bar) for s in inst_id_list]

    data_configs = [
        BacktestDataConfig(
            catalog_path=str(catalog_path),
            data_cls=Bar.fully_qualified_name(),
            instrument_id=instrument_id,
            bar_types=[str(bar_type)],
        )
        for instrument_id, bar_type in zip(nt_instrument_ids, bar_types)
    ]

    # Build bar_type_template using the same trick as xs_momentum
    sample_bar_str = str(bar_types[0])
    template_suffix = sample_bar_str.split(".OKX-", 1)[1]
    bar_type_template = "{inst}-" + template_suffix

    venue = build_okx_venue_config(
        starting_balance_usdt=args.equity,
        leverage=args.leverage,
        enable_fees=args.taker_fee_bps > 0,
        **({"taker_fee_bps": args.taker_fee_bps, "maker_fee_bps": args.maker_fee_bps}
           if args.taker_fee_bps > 0 else {}),
    )

    strategy_path, config_path = SUPPORTED_STRATEGIES["funding_cross_section"]
    strategy_config = ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=config_path,
        config={
            "instrument_ids": tuple(nt_instrument_ids),
            "beta_bar_type_template": bar_type_template,
            "account_equity_usdt": args.equity,
            "funding_panel_parquet_path": str(catalog_path),
        },
    )

    print(f"        funding_panel_parquet_path={catalog_path}")
    print("[3/4] running backtest...")
    summary = _run_and_maybe_plot(
        args, venue=venue, data=data_configs, strategies=[strategy_config],
        plot_title=f"funding_cross_section — {','.join(inst_id_list)}",
    )
    print("\n=== RESULT ===")
    print(summary)


# ---------------------------------------------------------------------------
# funding_skew_momentum：单标的 funding rate 极端拥挤反向交易
# ---------------------------------------------------------------------------


async def _run_funding_skew_momentum(args: argparse.Namespace) -> None:
    inst_id = args.symbol or (
        args.instrument_ids.split(",")[0].strip() if args.instrument_ids else None
    )
    if not inst_id:
        raise SystemExit("funding_skew_momentum 需要 --symbol 或 --instrument-ids (取第一个)")

    catalog_path = Path(args.catalog).resolve()
    catalog_path.mkdir(parents=True, exist_ok=True)

    from okx_trade.backtest.data_loader import prepare_funding_panel

    if not args.reuse_data:
        print(f"[1/4] downloading bars + funding panel for {inst_id}...")
    else:
        print("[1/4] reusing catalog (--reuse-data)")

    async with OKXRestClient(OKXSettings()) as client:
        if not args.reuse_data:
            _, bars = await prepare_backtest_catalog(
                client, inst_id, args.signal_bar,
                total=args.total_bars, catalog_path=str(catalog_path),
            )
            print(f"        {inst_id}: {len(bars)} bars")
        panel = await prepare_funding_panel(
            client, inst_id, total=args.funding_total,
            catalog_path=catalog_path, reuse_cache=args.reuse_data,
        )
        if not args.reuse_data:
            print(f"        funding panel: {len(panel.ts_ms)} samples")

    print("[2/4] building backtest config...")
    from nautilus_trader.backtest.config import BacktestDataConfig
    from nautilus_trader.config import ImportableStrategyConfig
    from nautilus_trader.model.data import Bar

    nt_inst_id = f"{inst_id}.{OKX_VENUE}"
    bar_type = make_bar_type(inst_id, args.signal_bar)

    data_configs = [BacktestDataConfig(
        catalog_path=str(catalog_path),
        data_cls=Bar.fully_qualified_name(),
        instrument_id=nt_inst_id,
        bar_types=[str(bar_type)],
    )]

    venue = build_okx_venue_config(
        starting_balance_usdt=args.equity,
        leverage=args.leverage,
        enable_fees=args.taker_fee_bps > 0,
        **({"taker_fee_bps": args.taker_fee_bps, "maker_fee_bps": args.maker_fee_bps}
           if args.taker_fee_bps > 0 else {}),
    )

    strategy_path, config_path = SUPPORTED_STRATEGIES["funding_skew_momentum"]
    strategy_config = ImportableStrategyConfig(
        strategy_path=strategy_path,
        config_path=config_path,
        config={
            "instrument_id": nt_inst_id,
            "bar_type": str(bar_type),
            "account_equity_usdt": args.equity,
            "funding_panel_parquet_path": str(catalog_path),
        },
    )

    print("[3/4] running backtest...")
    summary = _run_and_maybe_plot(
        args, venue=venue, data=data_configs, strategies=[strategy_config],
        plot_title=f"funding_skew_momentum — {inst_id}",
    )
    print("\n=== RESULT ===")
    print(summary)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_RUNNERS = {
    "xs_momentum": _run_xs_momentum,
    "funding_carry": _run_funding_carry,
    "funding_cross_section": _run_funding_cross_section,
    "funding_skew_momentum": _run_funding_skew_momentum,
}


async def _main_async(args: argparse.Namespace) -> None:
    runner = _RUNNERS[args.strategy]
    await runner(args)


def main() -> None:
    args = _parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
