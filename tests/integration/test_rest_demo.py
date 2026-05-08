"""REST 集成冒烟测试：跑真 OKX demo 端点。

跑法::

    # .env 配 OKX_API_KEY/SECRET/PASSPHRASE + OKX_IS_DEMO=true + OKX_HTTP_PROXY
    pytest tests/integration -m integration -v

默认 ``not integration`` 跳过，CI 不会触发。
"""
from __future__ import annotations

import pytest

from okx_trade import OKXRestClient, OKXSettings

pytestmark = pytest.mark.integration


@pytest.fixture
def settings() -> OKXSettings:
    s = OKXSettings()
    if not s.has_credentials():
        pytest.skip("missing OKX credentials in .env")
    return s


async def test_get_ticker(settings: OKXSettings) -> None:
    async with OKXRestClient(settings) as client:
        ticker = await client.market.get_ticker("BTC-USDT-SWAP")
        assert ticker.inst_id == "BTC-USDT-SWAP"
        assert ticker.last > 0


async def test_get_candles(settings: OKXSettings) -> None:
    async with OKXRestClient(settings) as client:
        candles = await client.market.get_candles("BTC-USDT-SWAP", bar="1m", limit=5)
        assert 1 <= len(candles) <= 5
        assert all(c.high >= c.low for c in candles)


async def test_get_balance(settings: OKXSettings) -> None:
    async with OKXRestClient(settings) as client:
        balance = await client.account.get_balance()
        # demo 账户至少有 USDT 余额
        usdt = balance.get("USDT")
        assert usdt is not None


async def test_get_funding_rate(settings: OKXSettings) -> None:
    """Phase 2 新增：拉永续 funding rate（funding_carry 策略依赖）。"""
    async with OKXRestClient(settings) as client:
        fr = await client.public.get_funding_rate("BTC-USDT-SWAP")
        assert fr.inst_id == "BTC-USDT-SWAP"
        assert fr.funding_time > 0
        # funding_rate 一般在 ±0.5%/8h 内
        assert abs(float(fr.funding_rate)) < 0.005
