"""Unit tests for VolatilityFilter (2026-05-26 Phase 1)."""
from __future__ import annotations

import pytest

from okx_trade.risk.volatility_filter import (
    VolatilityFilter,
    VolatilityFilterConfig,
)


def _make_null_log():
    class _Null:
        def info(self, *_a, **_kw): pass
        def warning(self, *_a, **_kw): pass
        def error(self, *_a, **_kw): pass
        def debug(self, *_a, **_kw): pass
    return _Null()


class TestVolatilityFilterConfigDefaults:
    def test_defaults(self) -> None:
        c = VolatilityFilterConfig()
        assert c.enable is False
        assert c.window_min == 60
        assert c.baseline_min == 1440
        assert c.warmup_min == 1440
        assert c.ratio_threshold == 3.0
        assert c.buffer_max == 2000

    def test_is_frozen(self) -> None:
        c = VolatilityFilterConfig()
        with pytest.raises(Exception):
            c.enable = True  # type: ignore[misc]


class TestFeedBar:
    def test_lazy_creates_deque(self) -> None:
        f = VolatilityFilter(VolatilityFilterConfig(), log=_make_null_log())
        assert f.buffer_size("BTC-USDT-SWAP") == 0
        f.feed_bar("BTC-USDT-SWAP", 100.0)
        assert f.buffer_size("BTC-USDT-SWAP") == 1

    def test_appends_to_existing(self) -> None:
        f = VolatilityFilter(VolatilityFilterConfig(), log=_make_null_log())
        f.feed_bar("BTC-USDT-SWAP", 100.0)
        f.feed_bar("BTC-USDT-SWAP", 100.1)
        f.feed_bar("BTC-USDT-SWAP", 100.2)
        assert f.buffer_size("BTC-USDT-SWAP") == 3

    def test_per_inst_isolation(self) -> None:
        f = VolatilityFilter(VolatilityFilterConfig(), log=_make_null_log())
        f.feed_bar("BTC-USDT-SWAP", 100.0)
        f.feed_bar("ETH-USDT-SWAP", 2000.0)
        assert f.buffer_size("BTC-USDT-SWAP") == 1
        assert f.buffer_size("ETH-USDT-SWAP") == 1

    def test_buffer_max_truncates(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(buffer_max=5), log=_make_null_log(),
        )
        for px in [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]:
            f.feed_bar("BTC-USDT-SWAP", px)
        assert f.buffer_size("BTC-USDT-SWAP") == 5
