"""Backtest runner 冲烟测试：合成 bar → catalog → BacktestNode → Summary。

不依赖网络（不调 OKX REST），用合成的 ``Candle`` + ``parse_okx_instrument`` 走通
完整链路。验证：
- ``write_bars_to_catalog`` 能正确落盘
- ``run_backtest`` 能跑完且返回非空 ``BacktestSummary``
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
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
)
from okx_trade.models.common import Instrument
from okx_trade.models.market import Candle


def _make_swap_instrument() -> Any:  # type: ignore[name-defined]
    okx = Instrument.model_validate({
        "instId": "BTC-USDT-SWAP", "instType": "SWAP",
        "baseCcy": "BTC", "quoteCcy": "USDT", "settleCcy": "USDT",
        "ctVal": "0.01", "ctValCcy": "BTC",
        "tickSz": "0.1", "lotSz": "1", "minSz": "1", "state": "live",
    })
    return parse_okx_instrument(okx, ts_init=0)


def _make_synthetic_candles(start_ts_ms: int, count: int, interval_ms: int = 60_000) -> list:
    """构造 ``count`` 根 1 分钟 K 线，价格在 67000 上下波动 ±100。"""
    candles = []
    for i in range(count):
        ts = start_ts_ms + i * interval_ms
        base = 67000 + (i % 20) * 10  # 简单锯齿
        candles.append(Candle.from_array([
            str(ts),
            f"{base}", f"{base + 5}", f"{base - 5}", f"{base + 1}",
            "1.0", "67001.0", "67001.0", "1",
        ]))
    return candles


@pytest.fixture
def tmp_catalog():
    """临时 catalog 目录，每个测试结束后清理。"""
    d = tempfile.mkdtemp(prefix="okx_bt_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestCatalogWrite:
    def test_write_and_load_bars(self, tmp_catalog: str) -> None:
        inst = _make_swap_instrument()
        candles = _make_synthetic_candles(start_ts_ms=1_700_000_000_000, count=50)
        bars = bars_to_nt_bars(candles, inst, "1m")
        assert len(bars) == 50

        write_bars_to_catalog(tmp_catalog, inst, bars)

        # catalog 目录里应该有 instrument + bar 文件
        files = list(Path(tmp_catalog).rglob("*.parquet"))
        assert len(files) >= 1


class TestBacktestSmoke:
    """端到端冲烟：catalog → BacktestNode → Summary，不要求策略产生信号。"""

    def test_runs_without_error(self, tmp_catalog: str) -> None:
        inst = _make_swap_instrument()
        # 写两套 bar：1m signal + 1H range（合成的）
        signal_candles = _make_synthetic_candles(
            start_ts_ms=1_700_000_000_000, count=200, interval_ms=60_000,
        )
        signal_bars = bars_to_nt_bars(signal_candles, inst, "1m")

        # 1H bar：用每 60 根聚合成一根（取首/尾/最高/最低）
        range_candles = []
        for i in range(0, len(signal_candles), 60):
            chunk = signal_candles[i:i + 60]
            if not chunk:
                continue
            range_candles.append(Candle.from_array([
                str(chunk[0].ts),
                str(chunk[0].open),
                str(max(c.high for c in chunk)),
                str(min(c.low for c in chunk)),
                str(chunk[-1].close),
                "60",
                "60", "60", "1",
            ]))
        range_bars = bars_to_nt_bars(range_candles, inst, "1H")

        write_bars_to_catalog(tmp_catalog, inst, signal_bars + range_bars)

        signal_bar_type = make_bar_type("BTC-USDT-SWAP", "1m")
        range_bar_type = make_bar_type("BTC-USDT-SWAP", "1H")
        instrument_id = f"BTC-USDT-SWAP.{OKX_VENUE}"

        data_configs = [
            BacktestDataConfig(
                catalog_path=str(Path(tmp_catalog).resolve()),
                data_cls=Bar.fully_qualified_name(),
                instrument_id=instrument_id,
                bar_types=[str(signal_bar_type)],
            ),
            BacktestDataConfig(
                catalog_path=str(Path(tmp_catalog).resolve()),
                data_cls=Bar.fully_qualified_name(),
                instrument_id=instrument_id,
                bar_types=[str(range_bar_type)],
            ),
        ]

        venue = build_okx_venue_config(starting_balance_usdt=10000, leverage=10)
        strategy_config = ImportableStrategyConfig(
            strategy_path="okx_trade.strategies.range_breakout:RangeBreakoutStrategy",
            config_path="okx_trade.strategies.range_breakout:RangeBreakoutConfig",
            config={
                "instrument_id": instrument_id,
                "range_bar_type": str(range_bar_type),
                "signal_bar_type": str(signal_bar_type),
                "risk_pct": 0.005,
                "account_equity_usdt": 10000.0,
            },
        )

        summary = run_backtest(
            venue=venue,
            data=data_configs,
            strategies=[strategy_config],
        )

        # 至少应该处理了一些事件
        assert summary.iterations > 0
        # 不要求策略一定有交易（合成数据可能不触发信号）
        assert summary.total_orders >= 0
        # Summary 的 __str__ 不抛
        assert isinstance(str(summary), str)


# 标注 Any 用，避免 unused import
from typing import Any  # noqa: E402
