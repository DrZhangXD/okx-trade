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
