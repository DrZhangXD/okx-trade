"""``OKXInstrumentProvider`` 单测：mock REST client，验证加载和过滤逻辑。"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from nautilus_trader.common.component import LiveClock

from okx_trade.adapter.instrument_provider import OKXInstrumentProvider
from okx_trade.adapter.parsing import to_instrument_id
from okx_trade.enums import InstType
from okx_trade.models.common import Instrument


def _make_okx_swap(inst_id: str, base: str = "BTC", quote: str = "USDT",
                   settle: str = "USDT") -> Instrument:
    return Instrument.model_validate({
        "instId": inst_id, "instType": "SWAP",
        "baseCcy": base, "quoteCcy": quote, "settleCcy": settle,
        "ctVal": "0.01", "ctValCcy": base,
        "tickSz": "0.1", "lotSz": "1", "minSz": "1", "state": "live",
    })


def _make_okx_spot(inst_id: str, base: str = "BTC", quote: str = "USDT") -> Instrument:
    return Instrument.model_validate({
        "instId": inst_id, "instType": "SPOT",
        "baseCcy": base, "quoteCcy": quote,
        "tickSz": "0.01", "lotSz": "0.0001", "minSz": "0.0001", "state": "live",
    })


def _make_mock_client(swaps: list[Instrument], spots: list[Instrument]) -> Any:
    """构造一个 mock OKXRestClient，public.get_instruments 按 instType 返回不同列表。"""
    client = MagicMock()
    client.public = MagicMock()

    async def get_instruments(inst_type: InstType, inst_id: str | None = None,
                              uly: str | None = None) -> list[Instrument]:
        items = swaps if inst_type == InstType.SWAP else spots
        if inst_id:
            return [i for i in items if i.inst_id == inst_id]
        return items

    async def get_instrument(inst_type: InstType, inst_id: str) -> Instrument:
        items = await get_instruments(inst_type, inst_id)
        if not items:
            raise RuntimeError(f"not found: {inst_id}")
        return items[0]

    client.public.get_instruments = AsyncMock(side_effect=get_instruments)
    client.public.get_instrument = AsyncMock(side_effect=get_instrument)
    return client


class TestLoadAllAsync:
    async def test_load_all_default(self) -> None:
        client = _make_mock_client(
            swaps=[_make_okx_swap("BTC-USDT-SWAP"), _make_okx_swap("ETH-USDT-SWAP", "ETH")],
            spots=[_make_okx_spot("BTC-USDT"), _make_okx_spot("ETH-USDT", "ETH")],
        )
        provider = OKXInstrumentProvider(client=client, clock=LiveClock())
        await provider.load_all_async()
        # 4 个全部入库
        assert len(provider.list_all()) == 4
        assert provider.find(to_instrument_id("BTC-USDT-SWAP")) is not None
        assert provider.find(to_instrument_id("ETH-USDT")) is not None

    async def test_load_all_filtered_by_inst_ids(self) -> None:
        client = _make_mock_client(
            swaps=[_make_okx_swap("BTC-USDT-SWAP"), _make_okx_swap("ETH-USDT-SWAP", "ETH")],
            spots=[_make_okx_spot("BTC-USDT")],
        )
        provider = OKXInstrumentProvider(client=client, clock=LiveClock())
        await provider.load_all_async(filters={"inst_ids": {"BTC-USDT-SWAP", "BTC-USDT"}})
        assert len(provider.list_all()) == 2

    async def test_load_all_filtered_by_inst_types(self) -> None:
        client = _make_mock_client(
            swaps=[_make_okx_swap("BTC-USDT-SWAP")],
            spots=[_make_okx_spot("BTC-USDT")],
        )
        provider = OKXInstrumentProvider(client=client, clock=LiveClock())
        await provider.load_all_async(filters={"inst_types": [InstType.SWAP]})
        # 只拉 SWAP，SPOT 不会进 cache
        assert len(provider.list_all()) == 1
        assert provider.find(to_instrument_id("BTC-USDT-SWAP")) is not None
        assert provider.find(to_instrument_id("BTC-USDT")) is None


class TestLoadIds:
    async def test_load_ids_routes_by_suffix(self) -> None:
        # 启发式：以 -SWAP 结尾走 SWAP，否则走 SPOT
        client = _make_mock_client(
            swaps=[_make_okx_swap("BTC-USDT-SWAP")],
            spots=[_make_okx_spot("BTC-USDT")],
        )
        provider = OKXInstrumentProvider(client=client, clock=LiveClock())
        await provider.load_ids_async([
            to_instrument_id("BTC-USDT-SWAP"),
            to_instrument_id("BTC-USDT"),
        ])
        assert provider.find(to_instrument_id("BTC-USDT-SWAP")) is not None
        assert provider.find(to_instrument_id("BTC-USDT")) is not None

    async def test_load_ids_empty_noop(self) -> None:
        client = _make_mock_client(swaps=[], spots=[])
        provider = OKXInstrumentProvider(client=client, clock=LiveClock())
        await provider.load_ids_async([])
        assert provider.list_all() == []

    async def test_load_async_single(self) -> None:
        client = _make_mock_client(
            swaps=[_make_okx_swap("BTC-USDT-SWAP")],
            spots=[],
        )
        provider = OKXInstrumentProvider(client=client, clock=LiveClock())
        await provider.load_async(to_instrument_id("BTC-USDT-SWAP"))
        assert provider.find(to_instrument_id("BTC-USDT-SWAP")) is not None
