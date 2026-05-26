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


class TestSubmitIsolatedOrder:
    """Test all 7 branches of submit_isolated_order:
       1. enable=False                  → cross fallback
       2. _iso_service is None          → cross fallback
       3. is_backtest()                 → cross fallback
       4. net_mode + caller pos_side=X  → forced None
       5. long_short_mode + no pos_side → derived from order.side
       6. ensure_leverage fails         → return False, no submit
       7. happy path                    → tag attached + submit + True
    """

    def _make_stub_strategy(
        self,
        *,
        enable_isolated_margin=True,
        iso_service=None,
        order_side="BUY",
    ):
        from okx_trade.strategies._okx_base import OkxStrategyBase

        class _Config:
            pass

        class _MockOrderSide:
            BUY = "BUY"
            SELL = "SELL"

        class _Order:
            def __init__(self):
                self.side = order_side
                self.tags = None

                class _InstId:
                    value = "DOT-USDT-SWAP.OKX"
                self.instrument_id = _InstId()

        class _Log:
            def __init__(self): self.warnings = []
            def warning(self, msg): self.warnings.append(msg)
            def info(self, msg): pass

        config = _Config()
        config.enable_isolated_margin = enable_isolated_margin

        m = type("Stub", (), {})()
        m.config = config
        m._iso_service = iso_service
        m._vol_filter = None
        m.log = _Log()
        m._submitted = []
        m.submit_order = lambda order: m._submitted.append(order)
        m.submit_isolated_order = OkxStrategyBase.submit_isolated_order.__get__(m)
        return m, _Order()

    @pytest.mark.asyncio
    async def test_branch1_disabled_falls_back_to_cross(self) -> None:
        from okx_trade.enums import PosSide
        m, order = self._make_stub_strategy(enable_isolated_margin=False)
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert len(m._submitted) == 1
        assert order.tags is None  # no isolated tag attached

    @pytest.mark.asyncio
    async def test_branch2_no_service_falls_back(self) -> None:
        from okx_trade.enums import PosSide
        m, order = self._make_stub_strategy(iso_service=None)
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert len(m._submitted) == 1
        assert order.tags is None

    @pytest.mark.asyncio
    async def test_branch3_backtest_falls_back(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            def is_backtest(self): return True

        m, order = self._make_stub_strategy(iso_service=_FakeService())
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert len(m._submitted) == 1
        assert order.tags is None

    @pytest.mark.asyncio
    async def test_branch4_net_mode_forces_pos_side_none(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "net_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                self.calls.append((inst_id, lever, pos_side))
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        svc = _FakeService()
        m, order = self._make_stub_strategy(iso_service=svc)
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert svc.calls == [("DOT-USDT-SWAP", 5, None)]  # forced None
        assert order.tags == ["td_mode:isolated"]
        assert len(m._submitted) == 1

    @pytest.mark.asyncio
    async def test_branch5_long_short_mode_derives_from_buy(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                self.calls.append((inst_id, lever, pos_side))
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        svc = _FakeService()
        m, order = self._make_stub_strategy(iso_service=svc, order_side="BUY")
        ok = await m.submit_isolated_order(order, lever=5)  # no pos_side
        assert ok is True
        assert svc.calls[0][2] == PosSide.LONG  # derived from BUY

    @pytest.mark.asyncio
    async def test_branch5b_long_short_mode_derives_from_sell(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                self.calls.append((inst_id, lever, pos_side))
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        svc = _FakeService()
        m, order = self._make_stub_strategy(iso_service=svc, order_side="SELL")
        ok = await m.submit_isolated_order(order, lever=5)
        assert svc.calls[0][2] == PosSide.SHORT  # derived from SELL

    @pytest.mark.asyncio
    async def test_branch6_ensure_leverage_failure_no_submit(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                return False, "51001 broken"
            def make_isolated_tags(self): return ["td_mode:isolated"]

        m, order = self._make_stub_strategy(iso_service=_FakeService())
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is False
        assert len(m._submitted) == 0  # did NOT submit
        assert order.tags is None
        assert any("skip leg" in w for w in m.log.warnings)

    @pytest.mark.asyncio
    async def test_branch7_happy_path_appends_to_existing_tags(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        m, order = self._make_stub_strategy(iso_service=_FakeService())
        order.tags = ["existing:tag"]
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert order.tags == ["existing:tag", "td_mode:isolated"]
