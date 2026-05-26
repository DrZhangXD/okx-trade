"""Unit tests for IsolatedMarginService (2026-05-26 Phase 1)."""
from __future__ import annotations

import pytest

from okx_trade.risk.isolated_margin_service import (
    BatchEnsureResult,
    IsolatedMarginService,
)


class TestBatchEnsureResult:
    def test_all_ok_true_when_no_failures(self) -> None:
        r = BatchEnsureResult(all_ok=True, failed=[])
        assert r.all_ok is True
        assert r.failed == []

    def test_all_ok_false_when_failures_present(self) -> None:
        r = BatchEnsureResult(all_ok=False, failed=[("BTC-USDT-SWAP", "err")])
        assert r.all_ok is False
        assert len(r.failed) == 1
        assert r.failed[0] == ("BTC-USDT-SWAP", "err")

    def test_is_frozen(self) -> None:
        r = BatchEnsureResult(all_ok=True, failed=[])
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            r.all_ok = False  # type: ignore[misc]


class TestIsolatedMarginServiceInit:
    def test_constructs_with_empty_caches(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())
        assert svc._lever_cache == {}
        assert svc._pos_mode is None
        assert svc._rest is None


def _make_null_log():
    class _Null:
        def info(self, *_a, **_kw): pass
        def warning(self, *_a, **_kw): pass
        def error(self, *_a, **_kw): pass
        def debug(self, *_a, **_kw): pass
    return _Null()
