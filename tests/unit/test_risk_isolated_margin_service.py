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


class TestEnsureLeverage:
    @pytest.fixture
    def service_with_mock_rest(self):
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())
        captured = {"calls": []}

        class _MockAccount:
            async def set_leverage(self_inner, *, inst_id, leverage, mgn_mode, pos_side):
                captured["calls"].append({
                    "inst_id": inst_id, "leverage": leverage,
                    "mgn_mode": mgn_mode, "pos_side": pos_side,
                })

        class _MockRest:
            account = _MockAccount()

        svc._rest = _MockRest()
        return svc, captured

    @pytest.mark.asyncio
    async def test_first_call_invokes_rest(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide, TdMode
        svc, captured = service_with_mock_rest
        ok, err = await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        assert ok is True
        assert err is None
        assert len(captured["calls"]) == 1
        c = captured["calls"][0]
        assert c["inst_id"] == "BTC-USDT-SWAP"
        assert c["leverage"] == 5
        assert c["mgn_mode"] == TdMode.ISOLATED
        assert c["pos_side"] == PosSide.LONG

    @pytest.mark.asyncio
    async def test_strips_okx_venue_suffix(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("DOT-USDT-SWAP.OKX", 3.0, PosSide.SHORT)
        assert captured["calls"][0]["inst_id"] == "DOT-USDT-SWAP"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_rest(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        assert len(captured["calls"]) == 1

    @pytest.mark.asyncio
    async def test_different_pos_side_separate_cache(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.SHORT)
        assert len(captured["calls"]) == 2

    @pytest.mark.asyncio
    async def test_changed_lever_re_invokes(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        await svc.ensure_leverage("BTC-USDT-SWAP", 8.0, PosSide.LONG)
        assert len(captured["calls"]) == 2
        assert captured["calls"][0]["leverage"] == 5
        assert captured["calls"][1]["leverage"] == 8

    @pytest.mark.asyncio
    async def test_none_pos_side_uses_net_cache_key(self, service_with_mock_rest) -> None:
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, None)
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, None)
        assert len(captured["calls"]) == 1
        # PosSide passed to account.set_leverage is None (account.py auto-fills NET)
        assert captured["calls"][0]["pos_side"] is None

    @pytest.mark.asyncio
    async def test_rest_failure_returns_false_with_err(self) -> None:
        from okx_trade.config import OKXSettings
        from okx_trade.enums import PosSide
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockAccount:
            async def set_leverage(self_inner, **kw):
                raise RuntimeError("OKX 51001: instId mismatch")

        class _MockRest:
            account = _MockAccount()

        svc._rest = _MockRest()
        ok, err = await svc.ensure_leverage("BAD-USDT", 5.0, PosSide.LONG)
        assert ok is False
        assert err is not None
        assert "51001" in err
        # Failed call should NOT populate cache
        assert ("BAD-USDT", "long") not in svc._lever_cache


def _make_null_log():
    class _Null:
        def info(self, *_a, **_kw): pass
        def warning(self, *_a, **_kw): pass
        def error(self, *_a, **_kw): pass
        def debug(self, *_a, **_kw): pass
    return _Null()
