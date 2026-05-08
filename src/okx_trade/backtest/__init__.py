"""回测套件：拉历史数据 + 跑 NT BacktestNode + 输出指标。

典型流程::

    from okx_trade.backtest import (
        download_historical_bars, write_bars_to_catalog,
        build_okx_venue_config, run_backtest,
    )

    # 1) 拉历史数据 → catalog
    bars = await download_historical_bars(rest_client, "BTC-USDT-SWAP", "1m", total=10000)
    write_bars_to_catalog("./data", instrument, bars)

    # 2) 配置回测
    venue = build_okx_venue_config(starting_balance_usdt=10000)
    result = run_backtest(venue, data_configs=[...], engine_config=..., strategies=[...])
    print(result.summary())
"""
from __future__ import annotations

from .data_loader import (
    bars_to_nt_bars,
    download_historical_bars,
    write_bars_to_catalog,
    write_instrument_to_catalog,
)
from .runner import (
    BacktestSummary,
    build_okx_venue_config,
    run_backtest,
)

__all__ = [
    # data
    "bars_to_nt_bars",
    "download_historical_bars",
    "write_bars_to_catalog",
    "write_instrument_to_catalog",
    # runner
    "BacktestSummary",
    "build_okx_venue_config",
    "run_backtest",
]
