"""历史 K 线 → NT Bar → ParquetDataCatalog。

工作流：
1. ``download_historical_bars(rest_client, inst_id, bar, total)``
   分页拉 OKX REST history-candles，返回 ``list[Candle]``（按时间正序）。
2. ``bars_to_nt_bars(candles, instrument, bar_period)`` 翻译为 NT ``Bar``。
3. ``write_bars_to_catalog(path, instrument, bars)`` 持久化到 Parquet（NT
   BacktestDataConfig 直接读这个目录）。

为什么落 Parquet？NT 回测引擎要求数据从 catalog 加载（支持 chunk + 跨进程共享）。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..adapter.parsing import make_bar_type, parse_okx_candle_to_bar, parse_okx_instrument
from ..enums import BarSize

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.instruments import Instrument as NTInstrument

    from ..models.market import Candle
    from ..rest.client import OKXRestClient


async def download_historical_bars(
    client: OKXRestClient,
    inst_id: str,
    bar: BarSize | str,
    *,
    total: int = 1500,
) -> list[Candle]:
    """通过 OKX REST 拉 ``total`` 根历史 K 线（最新到最早，返回时已正序）。

    限频：每页最多 100 根（history-candles 端点限制），分页串行。
    Phase 1 ``market.get_candles_extended`` 已实现分页逻辑，这里直接复用。
    """
    return await client.market.get_candles_extended(inst_id, bar=bar, total=total)


_PERIOD_MS: dict[str, int] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1H": 3_600_000, "2H": 7_200_000, "4H": 14_400_000, "6H": 21_600_000, "12H": 43_200_000,
    "1D": 86_400_000, "1W": 604_800_000,
}


def _period_to_ms(bar_period: str) -> int:
    """``"1H"`` → 3_600_000；用于把 bar 开盘时间换算成收盘时间。"""
    if bar_period in _PERIOD_MS:
        return _PERIOD_MS[bar_period]
    p = bar_period.upper().replace("MO", "M")  # 不支持 month；保护一下
    if p in _PERIOD_MS:
        return _PERIOD_MS[p]
    raise ValueError(f"unsupported bar_period for backtest: {bar_period}")


def bars_to_nt_bars(
    candles: list[Candle],
    instrument: NTInstrument,
    bar_period: str,
) -> list[Bar]:
    """把 ``Candle`` 列表翻译成 NT ``Bar``。

    Args:
        candles: 已正序排列的 ``Candle``。
        instrument: 已构造好的 NT instrument（提供 price/size precision）。
        bar_period: OKX 频率字符串，``"1m"`` / ``"1H"`` 等。

    Note:
        OKX candle ``ts`` 是 bar 开盘时间。NT 回测引擎按 ``ts_event`` 顺序 dispatch，
        若用开盘时间会让策略在 bar 开盘瞬间就拿到整根 bar 的 OHLC（lookahead）。
        因此这里把 ``ts_event = candle.ts + bar_period``，即收盘时间。
    """
    bar_type = make_bar_type(instrument.id.symbol.value, bar_period)
    period_ns = _period_to_ms(bar_period) * 1_000_000
    out: list[Bar] = []
    for c in candles:
        ts_close_ns = int(c.ts) * 1_000_000 + period_ns
        bar = parse_okx_candle_to_bar(
            c, bar_type=bar_type,
            price_precision=instrument.price_precision,
            size_precision=instrument.size_precision,
            ts_init=ts_close_ns,
        )
        out.append(bar)
    return out


def write_instrument_to_catalog(catalog_path: str | Path, instrument: NTInstrument) -> None:
    """把 instrument 写入 catalog（NT 加载 BacktestData 时需要先有 instrument）。"""
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    catalog = ParquetDataCatalog(path=str(catalog_path))
    catalog.write_data([instrument])


def write_bars_to_catalog(
    catalog_path: str | Path,
    instrument: NTInstrument,
    bars: list[Bar],
) -> None:
    """把 NT Bar 列表写入 ParquetDataCatalog。

    若 instrument 还没在 catalog 中，会先写一份；同 instrument_id 重复写不会重复 append。
    """
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    catalog = ParquetDataCatalog(path=str(catalog_path))
    catalog.write_data([instrument])
    catalog.write_data(bars)


async def prepare_backtest_catalog(
    client: OKXRestClient,
    inst_id: str,
    bar_period: str,
    *,
    total: int = 1500,
    catalog_path: str | Path = "./data",
) -> tuple[NTInstrument, list[Bar]]:
    """一站式：拉数据 → 解析 instrument → 翻译 bars → 写 catalog。

    Returns:
        ``(instrument, bars)``。已写入 catalog；调用方接着构造 ``BacktestDataConfig``。
    """
    from ..enums import InstType

    # 1) 拉 instrument 规格
    inst_type = InstType.SWAP if inst_id.endswith("-SWAP") else InstType.SPOT
    okx_inst = await client.public.get_instrument(inst_type, inst_id)
    nt_inst = parse_okx_instrument(okx_inst, ts_init=0)  # ts 在回测中无关

    # 2) 拉 K 线
    candles = await download_historical_bars(client, inst_id, bar_period, total=total)

    # 3) 翻译 + 写盘
    bars = bars_to_nt_bars(candles, nt_inst, bar_period)
    write_bars_to_catalog(catalog_path, nt_inst, bars)

    return nt_inst, bars


__all__ = [
    "bars_to_nt_bars",
    "download_historical_bars",
    "prepare_backtest_catalog",
    "write_bars_to_catalog",
    "write_instrument_to_catalog",
]
