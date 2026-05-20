"""``OKXLiveExecutionClient`` 关键纯方法单测。

NT 集成的 _submit_order / _cancel_order 走 NT live runtime 不便单测；本文件只覆盖
模块级纯函数（特别是 tdMode 自动路由 + 账户快照翻译）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from okx_trade.adapter.execution import (
    _build_account_balances,
    parse_td_mode_tag,
    resolve_pos_side,
    resolve_td_mode,
)
from okx_trade.enums import PosSide, Side, TdMode
from okx_trade.models.account import Balance, BalanceDetail


class TestResolveTdMode:
    @pytest.mark.parametrize("inst_id,default_mode,expected", [
        # 永续合约 → 用全局默认
        ("BTC-USDT-SWAP", TdMode.CROSS, TdMode.CROSS),
        ("ETH-USDT-SWAP", TdMode.ISOLATED, TdMode.ISOLATED),
        ("SOL-USDC-SWAP", TdMode.CROSS, TdMode.CROSS),
        # 现货 → 强制 CASH（无视 default）
        ("BTC-USDT", TdMode.CROSS, TdMode.CASH),
        ("ETH-USDT", TdMode.ISOLATED, TdMode.CASH),
        ("SOL-USDC", TdMode.CROSS, TdMode.CASH),
        # M6+.X：FUTURES (3 段，末段全 6 位数字 YYMMDD) → 用默认（cross）
        # 这是 2026-05-19 basis_arb 入场被拒 51000 的根因 fix
        ("BTC-USDT-260626", TdMode.CROSS, TdMode.CROSS),
        ("BTC-USDT-260925", TdMode.ISOLATED, TdMode.ISOLATED),
        ("ETH-USDT-261225", TdMode.CROSS, TdMode.CROSS),
        # M6+.X：OPTION (5 段，末段 C/P) → 用默认
        ("BTC-USD-260626-100000-C", TdMode.CROSS, TdMode.CROSS),
        ("BTC-USD-260925-80000-P", TdMode.ISOLATED, TdMode.ISOLATED),
    ])
    def test_routes_per_instrument(
        self, inst_id: str, default_mode: TdMode, expected: TdMode,
    ) -> None:
        assert resolve_td_mode(inst_id, default_mode) == expected

    def test_futures_open_short_yields_posside_short(self) -> None:
        """Regression：FUTURES SELL 开空，td_mode 必须是 cross/isolated 才能拿
        到 posSide=SHORT；否则 td_mode=cash → posSide=None → OKX 51000。"""
        td_mode = resolve_td_mode("BTC-USDT-260626", TdMode.CROSS)
        assert td_mode == TdMode.CROSS
        ps = resolve_pos_side(
            Side.SELL, reduce_only=False,
            pos_side_mode="long_short", td_mode=td_mode,
        )
        assert ps == PosSide.SHORT


class TestParseTdModeTag:
    """Per-strategy td_mode override via NT order tags (2026-05-20)."""

    def test_returns_isolated_for_isolated_tag(self) -> None:
        assert parse_td_mode_tag(["td_mode:isolated"]) == TdMode.ISOLATED

    def test_returns_cross_for_cross_tag(self) -> None:
        assert parse_td_mode_tag(["td_mode:cross"]) == TdMode.CROSS

    def test_returns_cash_for_cash_tag(self) -> None:
        assert parse_td_mode_tag(["td_mode:cash"]) == TdMode.CASH

    def test_none_for_empty_or_missing(self) -> None:
        assert parse_td_mode_tag(None) is None
        assert parse_td_mode_tag([]) is None
        assert parse_td_mode_tag(()) is None

    def test_none_for_unrelated_tags(self) -> None:
        assert parse_td_mode_tag(["risk_pct:0.5", "exec:taker"]) is None

    def test_none_for_invalid_value(self) -> None:
        assert parse_td_mode_tag(["td_mode:bogus"]) is None
        assert parse_td_mode_tag(["td_mode:"]) is None

    def test_tuple_works_same_as_list(self) -> None:
        """NT Order.tags can be tuple or list."""
        assert parse_td_mode_tag(("td_mode:isolated",)) == TdMode.ISOLATED

    def test_finds_among_multiple_tags(self) -> None:
        tags = ["strategy:basis_arb", "td_mode:isolated", "ts:1234"]
        assert parse_td_mode_tag(tags) == TdMode.ISOLATED


class TestResolveTdModeOverride:
    """resolve_td_mode honors tag_override on derivative instruments only."""

    def test_swap_uses_override_when_set(self) -> None:
        result = resolve_td_mode(
            "BTC-USDT-SWAP", TdMode.CROSS, tag_override=TdMode.ISOLATED,
        )
        assert result == TdMode.ISOLATED

    def test_futures_uses_override(self) -> None:
        result = resolve_td_mode(
            "BTC-USDT-260626", TdMode.CROSS, tag_override=TdMode.ISOLATED,
        )
        assert result == TdMode.ISOLATED

    def test_option_uses_override(self) -> None:
        result = resolve_td_mode(
            "BTC-USD-260626-100000-C", TdMode.CROSS, tag_override=TdMode.ISOLATED,
        )
        assert result == TdMode.ISOLATED

    def test_spot_ignores_override(self) -> None:
        """SPOT must always be CASH per OKX; tag override is ignored without
        raising so the caller (strategy) can apply tag uniformly to all legs
        without branching on inst type."""
        result = resolve_td_mode(
            "BTC-USDT", TdMode.CROSS, tag_override=TdMode.ISOLATED,
        )
        assert result == TdMode.CASH

    def test_no_override_falls_back_to_default(self) -> None:
        """tag_override=None ⇒ behavior identical to original two-arg form."""
        assert resolve_td_mode("BTC-USDT-SWAP", TdMode.CROSS, tag_override=None) == TdMode.CROSS
        assert resolve_td_mode("BTC-USDT-SWAP", TdMode.CROSS) == TdMode.CROSS


class TestResolvePosSide:
    """``posSide`` 由 (side, reduce_only, pos_side_mode, td_mode) 唯一决定。

    Hedged 模式下，reduce-only 单的 ``posSide`` 等于被减仓位的方向（与 side 反向）。
    早期实现忽略这一区分，hedged + reduce_only=True + 紧张保证金会触发 sCode=51008。
    """

    # net 模式 / 现货：永远不传 posSide
    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize("reduce_only", [True, False])
    def test_net_mode_returns_none(self, side: Side, reduce_only: bool) -> None:
        assert resolve_pos_side(
            side, reduce_only=reduce_only,
            pos_side_mode="net", td_mode=TdMode.CROSS,
        ) is None

    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    @pytest.mark.parametrize("reduce_only", [True, False])
    @pytest.mark.parametrize("pos_side_mode", ["net", "long_short"])
    def test_cash_td_mode_returns_none(
        self, side: Side, reduce_only: bool, pos_side_mode: str,
    ) -> None:
        assert resolve_pos_side(
            side, reduce_only=reduce_only,
            pos_side_mode=pos_side_mode, td_mode=TdMode.CASH,
        ) is None

    # hedged 开仓：posSide 与 side 同向
    def test_hedged_open_long(self) -> None:
        assert resolve_pos_side(
            Side.BUY, reduce_only=False,
            pos_side_mode="long_short", td_mode=TdMode.CROSS,
        ) == PosSide.LONG

    def test_hedged_open_short(self) -> None:
        assert resolve_pos_side(
            Side.SELL, reduce_only=False,
            pos_side_mode="long_short", td_mode=TdMode.CROSS,
        ) == PosSide.SHORT

    # hedged 平仓：posSide 等于被减方向（与 side 反向）—— 修复 51008 cascade 的关键
    def test_hedged_close_short_with_buy(self) -> None:
        # BUY + reduce_only → 平 SHORT 仓 → posSide=SHORT（不是 LONG）
        assert resolve_pos_side(
            Side.BUY, reduce_only=True,
            pos_side_mode="long_short", td_mode=TdMode.CROSS,
        ) == PosSide.SHORT

    def test_hedged_close_long_with_sell(self) -> None:
        # SELL + reduce_only → 平 LONG 仓 → posSide=LONG（不是 SHORT）
        assert resolve_pos_side(
            Side.SELL, reduce_only=True,
            pos_side_mode="long_short", td_mode=TdMode.CROSS,
        ) == PosSide.LONG

    # isolated margin 与 cross 行为一致
    @pytest.mark.parametrize("td_mode", [TdMode.CROSS, TdMode.ISOLATED])
    def test_hedged_close_isolated_or_cross(self, td_mode: TdMode) -> None:
        assert resolve_pos_side(
            Side.BUY, reduce_only=True,
            pos_side_mode="long_short", td_mode=td_mode,
        ) == PosSide.SHORT


class TestBuildAccountBalances:
    """``_build_account_balances`` 把 OKX ``Balance`` → NT ``AccountBalance`` 列表。

    覆盖：典型快照、零余额过滤、availBal>eq 时 locked 不能为负、未识别 ccy 跳过。
    """

    def test_typical_snapshot(self) -> None:
        bal = Balance(
            totalEq=Decimal("10000"),
            details=[
                BalanceDetail(
                    ccy="USDT",
                    eq=Decimal("10000"),
                    cashBal=Decimal("10000"),
                    availBal=Decimal("8000"),
                    availEq=Decimal("8000"),
                ),
            ],
        )
        out = _build_account_balances(bal)
        assert len(out) == 1
        ab = out[0]
        assert ab.currency.code == "USDT"
        assert Decimal(str(ab.total.as_decimal())) == Decimal("10000")
        assert Decimal(str(ab.free.as_decimal())) == Decimal("8000")
        assert Decimal(str(ab.locked.as_decimal())) == Decimal("2000")

    def test_skips_zero_balance_rows(self) -> None:
        bal = Balance(
            details=[
                BalanceDetail(ccy="USDT", eq=Decimal("100"), availBal=Decimal("100")),
                BalanceDetail(ccy="BTC", eq=Decimal("0"), availBal=Decimal("0")),
                BalanceDetail(ccy="ETH", eq=Decimal("0"), cashBal=Decimal("0"), availBal=Decimal("0")),
            ],
        )
        out = _build_account_balances(bal)
        assert [ab.currency.code for ab in out] == ["USDT"]

    def test_avail_exceeds_eq_clamps_locked_nonneg(self) -> None:
        # OKX 偶尔会有 availBal > eq（保证金调整 / UPL 浮盈写入延迟），NT 要求
        # locked >= 0，所以这里把 total 抬到 free，locked = 0。
        bal = Balance(
            details=[BalanceDetail(ccy="USDT", eq=Decimal("100"), availBal=Decimal("150"))],
        )
        out = _build_account_balances(bal)
        assert len(out) == 1
        ab = out[0]
        assert Decimal(str(ab.total.as_decimal())) == Decimal("150")
        assert Decimal(str(ab.locked.as_decimal())) == Decimal("0")
        assert Decimal(str(ab.free.as_decimal())) == Decimal("150")

    def test_negative_eq_clamped_to_zero(self) -> None:
        # 浮亏导致 eq < 0：total 夹回 0，行直接被 free=0 一并丢弃。
        bal = Balance(
            details=[BalanceDetail(ccy="USDT", eq=Decimal("-5"), availBal=Decimal("0"))],
        )
        assert _build_account_balances(bal) == []

    def test_unknown_currency_does_not_crash(self) -> None:
        # OKX 偶尔会推一些临时上线币种；Currency.from_str(strict=False) 通常能吃下去，
        # 但不可能 100%——这里只验证不崩溃。
        bal = Balance(
            details=[
                BalanceDetail(ccy="USDT", eq=Decimal("100"), availBal=Decimal("100")),
                BalanceDetail(ccy="WEIRDNEWCCY", eq=Decimal("1"), availBal=Decimal("1")),
            ],
        )
        out = _build_account_balances(bal)
        # 至少 USDT 一行要出现
        assert any(ab.currency.code == "USDT" for ab in out)
