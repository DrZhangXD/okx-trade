"""命令行回测工具。

用法::

    # 拉 BTC-USDT-SWAP 1h K 线 + 1d 区间，跑 Range Breakout 策略
    python scripts/backtest.py \\
        --strategy range_breakout \\
        --symbol BTC-USDT-SWAP \\
        --signal-bar 1H --range-bar 1D \\
        --total-bars 5000 \\
        --equity 10000 --risk-pct 0.005

会自动：
1. 调 OKX REST 拉历史 K 线（demo 端点不需要凭证）；
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
from okx_trade.adapter.parsing import make_bar_type  # noqa: E402
from okx_trade.backtest import (  # noqa: E402
    bars_to_nt_bars,
    download_historical_bars,
    build_okx_venue_config,
    run_backtest,
    write_bars_to_catalog,
)


SUPPORTED_STRATEGIES = {
    "range_breakout": (
        "okx_trade.strategies.range_breakout:RangeBreakoutStrategy",
        "okx_trade.strategies.range_breakout:RangeBreakoutConfig",
    ),
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OKX backtest runner (NautilusTrader)")
    p.add_argument("--strategy", required=True, choices=list(SUPPORTED_STRATEGIES.keys()))
    p.add_argument("--symbol", required=True, help="OKX instId, e.g. BTC-USDT-SWAP")
    p.add_argument("--signal-bar", default="1H", help="信号 K 线周期，默认 1H")
    p.add_argument("--range-bar", default="1D", help="区间 K 线周期，默认 1D")
    p.add_argument("--total-bars", type=int, default=2000, help="信号 K 线拉取根数")
    p.add_argument("--equity", type=float, default=10000.0, help="起始资金 USDT")
    p.add_argument("--risk-pct", type=float, default=0.005, help="单笔风险占净值")
    p.add_argument("--leverage", type=int, default=10, help="杠杆")
    p.add_argument("--catalog", default="./data", help="ParquetDataCatalog 路径")
    p.add_argument("--reuse-data", action="store_true",
                   help="复用 catalog 已有数据，跳过下载")
    return p.parse_args()


async def _main_async(args: argparse.Namespace) -> None:
    # 1) 拉数据 + 写 catalog
    if not args.reuse_data:
        print(f"[1/3] downloading {args.total_bars} bars × ({args.signal_bar}, {args.range_bar})...")
        async with OKXRestClient(OKXSettings()) as client:
            from okx_trade.backtest.data_loader import prepare_backtest_catalog
            inst, signal_bars = await prepare_backtest_catalog(
                client, args.symbol, args.signal_bar,
                total=args.total_bars, catalog_path=args.catalog,
            )
            # 区间 K 线根数：用更稀疏的上限（因为 1D bar 比 1H 少 24 倍）
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
            from okx_trade.enums import InstType
            inst_type = InstType.SWAP if args.symbol.endswith("-SWAP") else InstType.SPOT
            okx_inst = await client.public.get_instrument(inst_type, args.symbol)
            from okx_trade.adapter.parsing import parse_okx_instrument
            inst = parse_okx_instrument(okx_inst, ts_init=0)

    # 2) 构造 NT 回测配置
    print("[2/3] building backtest config...")
    from nautilus_trader.backtest.config import BacktestDataConfig
    from nautilus_trader.config import ImportableStrategyConfig
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    catalog = ParquetDataCatalog(path=args.catalog)
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

    strategy_path, config_path = SUPPORTED_STRATEGIES[args.strategy]
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

    # 3) 跑回测
    print("[3/3] running backtest...")
    summary = run_backtest(
        venue=venue,
        data=data_configs,
        strategies=[strategy_config],
    )

    print("\n=== RESULT ===")
    print(summary)


def main() -> None:
    args = _parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
