"""Tests for OKX open-interest endpoints + models."""
from __future__ import annotations

from decimal import Decimal

import pytest

from okx_trade.models.market import OpenInterest, OpenInterestPoint


def test_open_interest_parses_okx_response() -> None:
    raw = {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "oi": "12345.6",        # 张数（contracts）
        "oiCcy": "123.456",     # base ccy 名义
        "oiUsd": "8765432.10",  # USD 名义
        "ts": "1716120000000",
    }
    obj = OpenInterest.model_validate(raw)
    assert obj.inst_id == "BTC-USDT-SWAP"
    assert obj.oi == Decimal("12345.6")
    assert obj.oi_ccy == Decimal("123.456")
    assert obj.oi_usd == Decimal("8765432.10")
    assert obj.ts == 1716120000000


def test_open_interest_point_parses_history_row() -> None:
    # rubik/stat/contracts/open-interest-volume returns arrays [ts, oi_ccy, vol_ccy]
    row = ["1716120000000", "123456.78", "9876543.21"]
    obj = OpenInterestPoint.from_array(row)
    assert obj.ts == 1716120000000
    assert obj.oi_ccy == Decimal("123456.78")
    assert obj.vol_ccy == Decimal("9876543.21")


class _FakeTransport:
    """Pure-Python transport stub: record call, return canned response."""
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method, path, *, params=None, group=None, **_):
        self.calls.append((method, path, dict(params or {})))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_get_open_interest_calls_correct_endpoint() -> None:
    fake = _FakeTransport(responses=[[{
        "instId": "ETH-USDT-SWAP", "instType": "SWAP",
        "oi": "100", "oiCcy": "10", "oiUsd": "30000", "ts": "1716120000000",
    }]])
    from okx_trade.rest.public import PublicEndpoints
    pub = PublicEndpoints(fake)  # type: ignore[arg-type]
    res = await pub.get_open_interest("ETH-USDT-SWAP")
    assert res.inst_id == "ETH-USDT-SWAP"
    assert res.oi_usd == Decimal("30000")
    method, path, params = fake.calls[0]
    assert path == "/api/v5/public/open-interest"
    assert params == {"instType": "SWAP", "instId": "ETH-USDT-SWAP"}


@pytest.mark.asyncio
async def test_get_open_interest_history_parses_array_rows() -> None:
    fake = _FakeTransport(responses=[[
        ["1716120000000", "123.4", "9876.5"],
        ["1716123600000", "125.0", "10000.0"],
    ]])
    from okx_trade.rest.public import PublicEndpoints
    pub = PublicEndpoints(fake)  # type: ignore[arg-type]
    rows = await pub.get_open_interest_history("BTC-USDT", period="1H")
    assert len(rows) == 2
    assert rows[0].ts == 1716120000000
    assert rows[1].oi_ccy == Decimal("125.0")
    method, path, params = fake.calls[0]
    assert path == "/api/v5/rubik/stat/contracts/open-interest-volume"
    assert params["ccy"] == "BTC"
    assert params["period"] == "1H"


@pytest.mark.asyncio
async def test_get_open_interest_history_extended_pages_until_total() -> None:
    page1 = [["1700000000000", "1", "1"], ["1700003600000", "2", "2"]]
    page2 = [["1699996400000", "3", "3"]]
    page3: list = []  # empty → loop exits
    fake = _FakeTransport(responses=[page1, page2, page3])
    from okx_trade.rest.public import PublicEndpoints
    pub = PublicEndpoints(fake)  # type: ignore[arg-type]
    rows = await pub.get_open_interest_history_extended("BTC-USDT", period="1H", total=3)
    assert len(rows) == 3
    assert rows[0].ts < rows[1].ts < rows[2].ts  # ascending
    # Verify cursor (min ts of page1) was propagated to page2 as `end` param
    _, _, params2 = fake.calls[1]
    assert params2["end"] == "1700000000000"


@pytest.mark.asyncio
async def test_get_open_interest_history_extended_breaks_on_empty_page() -> None:
    """If a page returns no rows, the loop terminates early even if total not reached."""
    page1 = [["1700000000000", "1", "1"]]
    page2: list = []  # empty → break
    fake = _FakeTransport(responses=[page1, page2])
    from okx_trade.rest.public import PublicEndpoints
    pub = PublicEndpoints(fake)  # type: ignore[arg-type]
    rows = await pub.get_open_interest_history_extended("BTC-USDT", period="1H", total=999)
    assert len(rows) == 1
    assert len(fake.calls) == 2  # tried 2 pages then broke
