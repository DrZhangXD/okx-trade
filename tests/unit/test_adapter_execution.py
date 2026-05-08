"""``OKXLiveExecutionClient`` 关键纯方法单测。

NT 集成的 _submit_order / _cancel_order 走 NT live runtime 不便单测；本文件只覆盖
模块级纯函数（特别是 tdMode 自动路由 + 账户快照翻译）。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from okx_trade.adapter.execution import _build_account_balances, resolve_td_mode
from okx_trade.enums import TdMode
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
    ])
    def test_routes_per_instrument(
        self, inst_id: str, default_mode: TdMode, expected: TdMode,
    ) -> None:
        assert resolve_td_mode(inst_id, default_mode) == expected


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
