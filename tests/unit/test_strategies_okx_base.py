"""Unit tests for OkxStrategyBase (2026-05-26 Phase 1)."""
from __future__ import annotations

import pytest


class TestVolFilterAllow:
    def test_returns_no_filter_when_filter_is_none(self) -> None:
        from okx_trade.strategies._okx_base import OkxStrategyBase

        class _Stub:
            _vol_filter = None

        result = OkxStrategyBase.vol_filter_allow(_Stub(), "BTC-USDT-SWAP")  # type: ignore[arg-type]
        assert result == (True, "no_filter")

    def test_delegates_to_filter_when_present(self) -> None:
        from okx_trade.strategies._okx_base import OkxStrategyBase

        class _FakeFilter:
            def allow(self, inst_id):
                return (False, "vol_ratio=4.5>3.0")

        class _Stub:
            _vol_filter = _FakeFilter()

        result = OkxStrategyBase.vol_filter_allow(_Stub(), "BTC-USDT-SWAP")  # type: ignore[arg-type]
        assert result == (False, "vol_ratio=4.5>3.0")
