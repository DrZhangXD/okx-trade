"""NT TradingNode 注册用的工厂。

NT 启动时按 ``LiveDataEngineConfig.client_factories`` 字典查工厂，每个工厂的 ``create()``
被调用一次，返回 client 实例。

用法（在 NT 配置里）::

    from okx_trade.adapter.factories import (
        OKXLiveDataClientFactory, OKXLiveExecClientFactory,
    )
    from okx_trade.adapter.config import OKXDataClientConfig, OKXExecClientConfig

    config_node = TradingNodeConfig(
        data_clients={"OKX": OKXDataClientConfig(api_key=..., ...)},
        exec_clients={"OKX": OKXExecClientConfig(api_key=..., ...)},
    )
    node = TradingNode(config=config_node)
    node.add_data_client_factory("OKX", OKXLiveDataClientFactory)
    node.add_exec_client_factory("OKX", OKXLiveExecClientFactory)
    node.build()
    node.run()
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory
from pydantic import SecretStr

from ..config import OKXSettings
from ..enums import TdMode
from .config import OKXDataClientConfig, OKXExecClientConfig
from .constants import OKX_CLIENT_ID, OKX_VENUE
from .data import OKXLiveDataClient
from .execution import OKXLiveExecutionClient
from .instrument_provider import OKXInstrumentProvider

if TYPE_CHECKING:
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus


def _build_settings_from_data_config(config: OKXDataClientConfig) -> OKXSettings:
    """合并 config 字段到 ``OKXSettings``：config 显式值优先，未填的走 ``.env``。"""
    overrides: dict = {}
    if config.api_key:
        overrides["api_key"] = SecretStr(config.api_key)
    if config.api_secret:
        overrides["secret_key"] = SecretStr(config.api_secret)
    if config.passphrase:
        overrides["passphrase"] = SecretStr(config.passphrase)
    if config.http_proxy:
        overrides["http_proxy"] = config.http_proxy
        overrides["https_proxy"] = config.http_proxy
    overrides["is_demo"] = config.is_demo
    return OKXSettings(**overrides)


def _build_settings_from_exec_config(config: OKXExecClientConfig) -> OKXSettings:
    overrides: dict = {}
    if config.api_key:
        overrides["api_key"] = SecretStr(config.api_key)
    if config.api_secret:
        overrides["secret_key"] = SecretStr(config.api_secret)
    if config.passphrase:
        overrides["passphrase"] = SecretStr(config.passphrase)
    if config.http_proxy:
        overrides["http_proxy"] = config.http_proxy
        overrides["https_proxy"] = config.http_proxy
    overrides["is_demo"] = config.is_demo
    return OKXSettings(**overrides)


class OKXLiveDataClientFactory(LiveDataClientFactory):
    """构造 ``OKXLiveDataClient`` 实例供 NT TradingNode 启动调用。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: OKXDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OKXLiveDataClient:
        # 从 config 合成 OKXSettings
        from ..rest.client import OKXRestClient
        settings = _build_settings_from_data_config(config)

        # InstrumentProvider 需要 REST client；但 NT 在 _connect 时才能起 client。
        # 解法：构造 provider 时传一个 lazy REST client（init 时不连接），_connect() 真正起。
        # 简化版：构造 REST client 但不 enter context；让 data client 在 _connect 时 enter。
        rest = OKXRestClient(settings)
        provider = OKXInstrumentProvider(client=rest, clock=clock)

        client = OKXLiveDataClient(
            loop=loop,
            client_id=OKX_CLIENT_ID,
            venue=OKX_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            settings=settings,
            config=config,
        )
        # data client _connect 会再 enter rest，这里只是把 provider 关联好；data client
        # 会用自己的 self._rest 而不是 provider 的 client。重新绑定一下：
        client._rest = rest  # type: ignore[attr-defined]
        return client


class OKXLiveExecClientFactory(LiveExecClientFactory):
    """构造 ``OKXLiveExecutionClient`` 实例。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: OKXExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> OKXLiveExecutionClient:
        from ..rest.client import OKXRestClient
        settings = _build_settings_from_exec_config(config)
        rest = OKXRestClient(settings)
        provider = OKXInstrumentProvider(client=rest, clock=clock)
        td_mode = TdMode(config.td_mode)
        client = OKXLiveExecutionClient(
            loop=loop,
            client_id=OKX_CLIENT_ID,
            venue=OKX_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            settings=settings,
            td_mode=td_mode,
            pos_side_mode=config.pos_side_mode,
            config=config,
        )
        client._rest = rest  # type: ignore[attr-defined]
        return client


__all__ = [
    "OKXLiveDataClientFactory",
    "OKXLiveExecClientFactory",
]
