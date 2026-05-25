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

from ..adapter.parsing import (
    bar_spec_period_ms,
    make_bar_type,
    parse_okx_candle_to_bar,
    parse_okx_instrument,
)
from ..enums import BarSize, InstType


def _infer_inst_type(inst_id: str) -> InstType:
    """Infer OKX inst_type from inst_id string.

    - ``BTC-USDT-SWAP`` → SWAP
    - ``BTC-USDT-260626`` (YYMMDD suffix) → FUTURES
    - ``BTC-USDT`` → SPOT
    """
    if inst_id.endswith("-SWAP"):
        return InstType.SWAP
    tail = inst_id.rsplit("-", 1)[-1]
    if len(tail) == 6 and tail.isdigit():
        return InstType.FUTURES
    return InstType.SPOT

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


def bars_to_nt_bars(
    candles: list[Candle],
    instrument: NTInstrument,
    bar_period: str,
) -> list[Bar]:
    """把 ``Candle`` 列表翻译成 NT ``Bar``。

    Args:
        candles: 已正序排列的 ``Candle``。
        instrument: 已构造好的 NT instrument（提供 price/size precision）。
        bar_period: OKX 频率字符串，``"1m"`` / ``"1H"`` / ``"3D"`` 等。

    Note:
        ``parse_okx_candle_to_bar`` 内部把 ``ts_event`` 设为收盘时间（OKX candle.ts
        是开盘时间）。回测里 ``ts_init`` 也对齐到收盘时间，让 NT 按事件顺序回放
        而不是 ingestion 顺序。``bar_period`` 的合法集合由 NT ``BarAggregation``
        枚举决定（见 ``parsing.bar_spec_period_ms``），覆盖 ``BarSize`` 中暴露的
        全部周期，包括 ``2D`` / ``3D``。
    """
    bar_type = make_bar_type(instrument.id.symbol.value, bar_period)
    period_ns = bar_spec_period_ms(bar_type.spec) * 1_000_000
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
    taker_fee_bps: float | None = None,
    maker_fee_bps: float | None = None,
) -> tuple[NTInstrument, list[Bar]]:
    """一站式：拉数据 → 解析 instrument → 翻译 bars → 写 catalog。

    Args:
        taker_fee_bps / maker_fee_bps: 可选，把手续费率（bps）烘到 instrument 上，供
            回测的 ``MakerTakerFeeModel`` 读取。None = 沿用 NT 默认（零手续费）。

    Returns:
        ``(instrument, bars)``。已写入 catalog；调用方接着构造 ``BacktestDataConfig``。
    """
    from ..enums import InstType

    # 1) 拉 instrument 规格
    inst_type = _infer_inst_type(inst_id)
    okx_inst = await client.public.get_instrument(inst_type, inst_id)
    nt_inst = parse_okx_instrument(
        okx_inst, ts_init=0,
        taker_fee_bps=taker_fee_bps, maker_fee_bps=maker_fee_bps,
    )

    # 2) 拉 K 线
    candles = await download_historical_bars(client, inst_id, bar_period, total=total)

    # 3) 翻译 + 写盘
    bars = bars_to_nt_bars(candles, nt_inst, bar_period)
    write_bars_to_catalog(catalog_path, nt_inst, bars)

    return nt_inst, bars


from .funding_data import (
    FundingPanel,
    download_historical_funding_rates,
    read_funding_parquet,
    write_funding_parquet,
)


async def prepare_funding_panel(
    client: "OKXRestClient",
    inst_id: str,
    *,
    total: int = 1095,
    catalog_path: Path,
    reuse_cache: bool = True,
) -> FundingPanel:
    """One-stop: read parquet cache, else download via REST and write cache.

    Mirrors ``prepare_backtest_catalog`` ergonomics. Always returns a sorted panel.
    """
    if reuse_cache:
        try:
            return read_funding_parquet(inst_id, catalog_path=catalog_path)
        except FileNotFoundError:
            pass
    panel = await download_historical_funding_rates(client, inst_id, total=total)
    write_funding_parquet(panel, catalog_path=catalog_path)
    return panel


__all__ = [
    "bars_to_nt_bars",
    "download_historical_bars",
    "prepare_backtest_catalog",
    "prepare_funding_panel",
    "write_bars_to_catalog",
    "write_instrument_to_catalog",
]
