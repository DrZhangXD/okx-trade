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


class TestMakeIsolatedTags:
    def test_returns_td_mode_isolated(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())
        assert svc.make_isolated_tags() == ["td_mode:isolated"]


class TestIsBacktest:
    def test_returns_true_when_api_key_empty(self) -> None:
        from okx_trade.config import OKXSettings
        # Default OKXSettings reads from env; force empty api_key
        settings = OKXSettings(api_key="", secret_key="", passphrase="")
        svc = IsolatedMarginService(settings, log=_make_null_log())
        assert svc.is_backtest() is True

    def test_returns_false_when_api_key_set(self) -> None:
        from okx_trade.config import OKXSettings
        settings = OKXSettings(
            api_key="real-key", secret_key="real-secret", passphrase="real-pass",
        )
        svc = IsolatedMarginService(settings, log=_make_null_log())
        assert svc.is_backtest() is False


class TestGetPosMode:
    @pytest.mark.asyncio
    async def test_first_call_fetches_and_caches(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockTransport:
            calls = 0
            async def request(self, method, path, *, params=None, private=None, group=None):
                _MockTransport.calls += 1
                return [{"posMode": "long_short_mode"}]

        class _MockRest:
            transport = _MockTransport()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "long_short_mode"
        assert svc._pos_mode == "long_short_mode"
        # Second call hits cache, transport.request not called again
        result2 = await svc.get_pos_mode()
        assert result2 == "long_short_mode"
        assert _MockTransport.calls == 1

    @pytest.mark.asyncio
    async def test_net_mode_returned(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockTransport:
            async def request(self, *a, **kw):
                return [{"posMode": "net_mode"}]

        class _MockRest:
            transport = _MockTransport()

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "net_mode"

    @pytest.mark.asyncio
    async def test_unknown_pos_mode_warns_and_falls_back_to_net(self) -> None:
        from okx_trade.config import OKXSettings
        class _CapturingLog:
            def __init__(self): self.warnings = []
            def info(self, *_a, **_kw): pass
            def warning(self, msg, **_kw): self.warnings.append(msg)
            def error(self, *_a, **_kw): pass
            def debug(self, *_a, **_kw): pass

        log = _CapturingLog()
        svc = IsolatedMarginService(OKXSettings(), log=log)

        class _MockTransport:
            async def request(self, *a, **kw):
                return [{"posMode": "future_unexpected_mode"}]

        class _MockRest:
            transport = _MockTransport()

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "net_mode"  # safe fallback
        assert any("UNEXPECTED posMode" in w for w in log.warnings)

    @pytest.mark.asyncio
    async def test_rest_failure_falls_back_to_net(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockTransport:
            async def request(self, *a, **kw):
                raise RuntimeError("network down")

        class _MockRest:
            transport = _MockTransport()

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "net_mode"


def _make_null_log():
    class _Null:
        def info(self, *_a, **_kw): pass
        def warning(self, *_a, **_kw): pass
        def error(self, *_a, **_kw): pass
        def debug(self, *_a, **_kw): pass
    return _Null()
