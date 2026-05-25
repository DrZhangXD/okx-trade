"""Tests for option underlying filter on DataClientConfig + provider plumbing."""
from __future__ import annotations

import pytest


def test_data_client_config_accepts_option_ulys():
    from okx_trade.adapter.config import OKXDataClientConfig
    cfg = OKXDataClientConfig(
        api_key="x", api_secret="y", passphrase="z", is_demo=True,
        option_ulys=["BTC-USD"],
    )
    assert cfg.option_ulys == ["BTC-USD"]


def test_data_client_config_option_ulys_defaults_none():
    from okx_trade.adapter.config import OKXDataClientConfig
    cfg = OKXDataClientConfig(api_key="x", api_secret="y", passphrase="z", is_demo=True)
    assert cfg.option_ulys is None
