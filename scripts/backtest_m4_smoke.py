"""M4 策略 NT engine 端到端冲烟（独立进程）。

为什么不放到 pytest 里？
-----------------------
NT ``BacktestEngine`` 在同一 Python 进程内只能跑 1 次（Cython 全局状态）。
单测套件已经被 ``test_backtest_runner.py`` 占用，再加 M4 的 backtest 测试会
导致整个 pytest session 崩溃。所以 M4 NT 集成冲烟拆成独立脚本，每次 fork 一个
新进程跑 1 个策略。

用法::

    python scripts/backtest_m4_smoke.py xs_momentum
    python scripts/backtest_m4_smoke.py liq_reversal

输出：summary 字符串 + 退出码 0（成功）/ 1（失败）。

CI 中可这样跑（每个策略一个 job）::

    python scripts/backtest_m4_smoke.py xs_momentum && \\
    python scripts/backtest_m4_smoke.py liq_reversal
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
import tempfile
from pathlib import Path

# 允许从仓库根直接 ``python scripts/backtest_m4_smoke.py``
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nautilus_trader.backtest.config import BacktestDataConfig
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.model.data import Bar

from okx_trade.adapter.constants import OKX_VENUE
from okx_trade.adapter.parsing import make_bar_type, parse_okx_instrument
from okx_trade.backtest import (
    bars_to_nt_bars,
    build_okx_venue_config,
    run_backtest,
    write_bars_to_catalog,
    write_instrument_to_catalog,
)
from okx_trade.models.common import Instrument
from okx_trade.models.market import Candle


def _make_perp(symbol: str, tick_sz: str = "0.01", lot_sz: str = "1") -> object:
    okx = Instrument.model_validate({
        "instId": symbol, "instType": "SWAP",
        "baseCcy": symbol.split("-")[0], "quoteCcy": "USDT", "settleCcy": "USDT",
        "ctVal": "0.01", "ctValCcy": symbol.split("-")[0],
        "tickSz": tick_sz, "lotSz": lot_sz, "minSz": lot_sz, "state": "live",
    })
    return parse_okx_instrument(okx, ts_init=0)


def _d1_candles(start_ts_ms: int, count: int, base: float, amp: float,
                period: float, phase: float) -> list[Candle]:
    interval_ms = 86_400_000
    out: list[Candle] = []
    for i in range(count):
        ts = start_ts_ms + i * interval_ms
        t = (i + phase) / period
        price = base + amp * math.sin(2 * math.pi * t)
        out.append(Candle.from_array([
            str(ts), f"{price}", f"{price * 1.005}", f"{price * 0.995}", f"{price}",
            "1.0", f"{price}", f"{price}", "1",
        ]))
    return out


def _1m_candles(start_ts_ms: int, count: int, base: float = 30000.0) -> list[Candle]:
    out: list[Candle] = []
    for i in range(count):
        ts = start_ts_ms + i * 60_000
        cl = base + (i % 20) * 1.0
        out.append(Candle.from_array([
            str(ts), f"{cl}", f"{cl + 1}", f"{cl - 1}", f"{cl}",
            "1.0", f"{cl}", f"{cl}", "1",
        ]))
    return out


def smoke_xs_momentum() -> None:
    catalog = tempfile.mkdtemp(prefix="okx_m4_xs_")
    try:
        symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "AVAX-USDT-SWAP"]
        params = [
            ("BTC-USDT-SWAP", 100.0, 10.0, 30.0, 0.0),
            ("ETH-USDT-SWAP", 50.0, 4.0, 30.0, 7.5),
            ("SOL-USDT-SWAP", 30.0, 3.0, 30.0, 15.0),
            ("AVAX-USDT-SWAP", 20.0, 2.0, 30.0, 22.5),
        ]
        start_ts = 1_700_000_000_000

        for sym in symbols:
            inst = _make_perp(sym)
            write_instrument_to_catalog(catalog, inst)
            base, amp, per, phase = next(
                (b, a, p, ph) for s, b, a, p, ph in params if s == sym
            )
            candles = _d1_candles(start_ts, 60, base, amp, per, phase)
            bars = bars_to_nt_bars(candles, inst, "1D")
            write_bars_to_catalog(catalog, inst, bars)

        data_configs = [
            BacktestDataConfig(
                catalog_path=str(Path(catalog).resolve()),
                data_cls=Bar.fully_qualified_name(),
                instrument_id=f"{s}.{OKX_VENUE}",
                bar_types=[str(make_bar_type(s, "1D"))],
            )
            for s in symbols
        ]
        venue = build_okx_venue_config(starting_balance_usdt=100_000, leverage=10)
        strategy_config = ImportableStrategyConfig(
            strategy_path="okx_trade.strategies.xs_momentum:XSMomentumStrategy",
            config_path="okx_trade.strategies.xs_momentum:XSMomentumConfig",
            config={
                "instrument_ids": tuple(f"{s}.{OKX_VENUE}" for s in symbols),
                "bar_type_template": "{inst}-1-DAY-LAST-EXTERNAL",
                "lookback_days": 7, "top_n": 1, "bot_n": 1,
                "vol_window": 10, "buffer_size": 30,
                "risk_pct": 0.01, "account_equity_usdt": 100_000.0,
            },
        )
        summary = run_backtest(venue=venue, data=data_configs,
                                strategies=[strategy_config])
        print("=== XS Momentum smoke summary ===")
        print(summary)
        if summary.iterations <= 0:
            raise RuntimeError("backtest produced 0 iterations")
    finally:
        shutil.rmtree(catalog, ignore_errors=True)


def smoke_liq_reversal() -> None:
    catalog = tempfile.mkdtemp(prefix="okx_m4_liq_")
    try:
        inst = _make_perp("BTC-USDT-SWAP")
        write_instrument_to_catalog(catalog, inst)
        candles = _1m_candles(1_700_000_000_000, count=200)
        bars = bars_to_nt_bars(candles, inst, "1m")
        write_bars_to_catalog(catalog, inst, bars)

        bar_type = str(make_bar_type("BTC-USDT-SWAP", "1m"))
        instrument_id = f"BTC-USDT-SWAP.{OKX_VENUE}"
        data_configs = [BacktestDataConfig(
            catalog_path=str(Path(catalog).resolve()),
            data_cls=Bar.fully_qualified_name(),
            instrument_id=instrument_id,
            bar_types=[bar_type],
        )]
        venue = build_okx_venue_config(starting_balance_usdt=10_000, leverage=10)
        strategy_config = ImportableStrategyConfig(
            strategy_path="okx_trade.strategies.liq_reversal:LiqReversalStrategy",
            config_path="okx_trade.strategies.liq_reversal:LiqReversalConfig",
            config={
                "instrument_id": instrument_id, "bar_type": bar_type,
                "liq_window_sec": 60, "liq_history_sec": 3600,
                "zscore_threshold": 3.0, "cooldown_sec": 60,
                "entry_stop_distance_pct": 0.005, "entry_tp_rr_ratio": 2.0,
                "risk_pct": 0.005, "account_equity_usdt": 10_000.0,
                "buffer_size": 1000, "subscribe_liquidations": False,
            },
        )
        summary = run_backtest(venue=venue, data=data_configs,
                                strategies=[strategy_config])
        print("=== Liq Reversal smoke summary ===")
        print(summary)
        if summary.iterations <= 0:
            raise RuntimeError("backtest produced 0 iterations")
    finally:
        shutil.rmtree(catalog, ignore_errors=True)


SMOKES = {
    "xs_momentum": smoke_xs_momentum,
    "liq_reversal": smoke_liq_reversal,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 NT backtest smoke")
    parser.add_argument("strategy", choices=list(SMOKES.keys()))
    args = parser.parse_args()
    try:
        SMOKES[args.strategy]()
    except Exception as exc:
        print(f"smoke FAILED: {exc}", file=sys.stderr)
        return 1
    print("smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
