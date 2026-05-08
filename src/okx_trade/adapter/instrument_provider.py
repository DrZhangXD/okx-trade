"""``OKXInstrumentProvider``：把 OKX REST instruments 灌进 NT 的 cache。

NT TradingNode 启动时会调用本 provider 的 ``initialize()`` 来填充本地 instrument 表。
策略层后续 ``cache.instrument(InstrumentId)`` 才能拿到 tick_sz / lot_sz / settlement_currency
等下单必备元数据。

设计：
- 继承 ``nautilus_trader.common.providers.InstrumentProvider`` 抽象基类；
- 复用 Phase 1 的 ``OKXRestClient.public.get_instruments()``（无鉴权）；
- 默认加载 SWAP + SPOT 全集；filters 可缩小范围。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId

from ..enums import InstType
from .parsing import from_instrument_id, parse_okx_instrument

if TYPE_CHECKING:
    from nautilus_trader.common.component import LiveClock
    from nautilus_trader.common.config import InstrumentProviderConfig

    from ..rest.client import OKXRestClient


class OKXInstrumentProvider(InstrumentProvider):
    """OKX 交易品种提供方，封装 REST `/public/instruments` 调用。

    Args:
        client: 已构造好的 ``OKXRestClient``（必须已经 ``__aenter__``，否则 REST 调用会失败）。
        clock: NT ``LiveClock``，用来给 instrument 打 ``ts_init`` 戳。
        config: NT ``InstrumentProviderConfig``，用 ``load_all=True`` 控制启动时是否预加载。
    """

    def __init__(
        self,
        client: OKXRestClient,
        clock: LiveClock,
        config: InstrumentProviderConfig | None = None,
    ) -> None:
        super().__init__(config=config)
        self._client = client
        self._clock = clock

    async def load_all_async(self, filters: dict[str, Any] | None = None) -> None:
        """拉 SWAP + SPOT 全集到 cache。

        ``filters`` 支持的键：
        - ``inst_types``: ``list[InstType]``，限制 instType 子集（默认两个都拉）
        - ``inst_ids``: ``set[str]``，仅保留特定 instId（如 ``{"BTC-USDT-SWAP"}``）
        """
        filters = filters or {}
        inst_types: list[InstType] = filters.get("inst_types", [InstType.SWAP, InstType.SPOT])
        inst_ids_keep: set[str] | None = filters.get("inst_ids")

        # 并行拉所有 instType（两个 REST 请求）
        tasks = [self._client.public.get_instruments(t) for t in inst_types]
        results = await asyncio.gather(*tasks)

        ts_init = self._clock.timestamp_ns()
        loaded = 0
        for okx_list in results:
            for okx_inst in okx_list:
                if inst_ids_keep is not None and okx_inst.inst_id not in inst_ids_keep:
                    continue
                try:
                    nt_inst = parse_okx_instrument(okx_inst, ts_init=ts_init)
                except ValueError:
                    # 暂不支持的 instType（FUTURES/OPTION）跳过，不抛异常
                    continue
                self.add(nt_inst)
                loaded += 1
        # NT 基类自带 _log，但本 provider 不强依赖 logger；如有需要在 client 层包一层 structlog。

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict[str, Any] | None = None,
    ) -> None:
        """按 instrument_id 列表精确加载（用于 NT TradingNode 配置了 ``load_ids`` 的场景）。"""
        if not instrument_ids:
            return
        # OKX REST 的 instId 参数只接受单个，因此需要并发多次调用；按 instType 分组优化
        spot_ids: list[str] = []
        swap_ids: list[str] = []
        for nid in instrument_ids:
            okx_id = from_instrument_id(nid)
            # 简单启发：以 ``-SWAP`` 结尾就是永续，否则当 spot
            if okx_id.endswith("-SWAP"):
                swap_ids.append(okx_id)
            else:
                spot_ids.append(okx_id)

        ts_init = self._clock.timestamp_ns()

        async def _load_one(inst_type: InstType, inst_id: str) -> None:
            okx_inst = await self._client.public.get_instrument(inst_type, inst_id)
            nt_inst = parse_okx_instrument(okx_inst, ts_init=ts_init)
            self.add(nt_inst)

        tasks = [_load_one(InstType.SWAP, x) for x in swap_ids]
        tasks += [_load_one(InstType.SPOT, x) for x in spot_ids]
        await asyncio.gather(*tasks)

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict[str, Any] | None = None,
    ) -> None:
        """单个 instrument 加载——委托给 ``load_ids_async``。"""
        await self.load_ids_async([instrument_id], filters=filters)


__all__ = ["OKXInstrumentProvider"]
