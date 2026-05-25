"""Tests for the inst_id -> InstType inference in data_loader."""
from __future__ import annotations

import pytest

from okx_trade.backtest.data_loader import _infer_inst_type
from okx_trade.enums import InstType


@pytest.mark.parametrize(
    "inst_id, expected",
    [
        ("BTC-USDT-SWAP", InstType.SWAP),
        ("ETH-USDT-SWAP", InstType.SWAP),
        ("BTC-USDT", InstType.SPOT),
        ("ETH-USDC", InstType.SPOT),
        ("BTC-USDT-260626", InstType.FUTURES),
        ("BTC-USDT-251225", InstType.FUTURES),
        ("ETH-USDT-260925", InstType.FUTURES),
    ],
)
def test_infer_inst_type_canonical_examples(inst_id, expected):
    assert _infer_inst_type(inst_id) == expected


def test_infer_inst_type_handles_non_digit_third_segment():
    # Non-6-digit suffix is not a dated future
    assert _infer_inst_type("BTC-USDT-NOTADATE") == InstType.SPOT
    assert _infer_inst_type("FOO") == InstType.SPOT
