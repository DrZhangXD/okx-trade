"""WS 集成冒烟测试：连真 OKX demo WS 端点。

跑法::

    pytest tests/integration -m integration -v -k ws
"""
from __future__ import annotations

import asyncio

import pytest

from okx_trade import OKXSettings, OKXWSClient

pytestmark = pytest.mark.integration


@pytest.fixture
def settings() -> OKXSettings:
    return OKXSettings()  # public 频道无需凭证


async def test_subscribe_books5(settings: OKXSettings) -> None:
    """订阅 BTC-USDT-SWAP books5，10s 内应至少收到 1 条推送。"""
    async with OKXWSClient(settings) as ws:
        async with ws.subscribe("books5", instId="BTC-USDT-SWAP") as stream:
            event = await asyncio.wait_for(anext(stream), timeout=10.0)
            assert event["arg"]["channel"] == "books5"
            assert "data" in event


async def test_subscribe_candle(settings: OKXSettings) -> None:
    """K 线频道走 business endpoint，验证路由正确。"""
    async with OKXWSClient(settings) as ws:
        async with ws.subscribe("candle1m", instId="BTC-USDT-SWAP") as stream:
            event = await asyncio.wait_for(anext(stream), timeout=15.0)
            assert event["arg"]["channel"] == "candle1m"
