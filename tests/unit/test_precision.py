"""精度对齐工具测试。重点覆盖 float 输入下的精度坑。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from okx_trade.utils.precision import (
    floor_to_lot,
    fmt_price,
    fmt_size,
    round_to_tick,
)


class TestRoundToTick:
    def test_basic(self) -> None:
        assert round_to_tick("12345.678", "0.1") == Decimal("12345.7")

    def test_round_half_up(self) -> None:
        # 0.05 在 0.1 的 tick 下应进位到 0.1
        assert round_to_tick("100.05", "0.1") == Decimal("100.1")

    def test_large_tick(self) -> None:
        # tick=0.5 时，12345.64 应对齐到 12345.5（最近的 0.5 倍数）
        assert round_to_tick("12345.64", "0.5") == Decimal("12345.5")
        assert round_to_tick("12345.76", "0.5") == Decimal("12346.0")

    def test_integer_tick(self) -> None:
        assert round_to_tick("12345.6", "1") == Decimal("12346")

    def test_float_input_no_precision_loss(self) -> None:
        # float 0.1 经典精度坑：scalp 旧实现可能出问题
        assert round_to_tick(0.1 + 0.2, "0.1") == Decimal("0.3")

    def test_zero_tick_returns_input(self) -> None:
        assert round_to_tick("123.456", "0") == Decimal("123.456")


class TestFloorToLot:
    def test_exact(self) -> None:
        assert floor_to_lot("3.7", "0.1") == Decimal("3.7")

    def test_floor_down(self) -> None:
        # 3.78 在 lot=0.1 下应向下到 3.7（不能超过 3.78）
        assert floor_to_lot("3.78", "0.1") == Decimal("3.7")

    def test_integer_lot(self) -> None:
        assert floor_to_lot("3.78", "1") == Decimal("3")

    def test_already_aligned(self) -> None:
        assert floor_to_lot("100", "1") == Decimal("100")

    def test_zero_lot_returns_input(self) -> None:
        assert floor_to_lot("100.5", "0") == Decimal("100.5")


class TestFmtSize:
    def test_decimal_lot(self) -> None:
        assert fmt_size("3.7", "0.1") == "3.7"

    def test_integer_lot(self) -> None:
        assert fmt_size("100", "1") == "100"
        # 整数 lot 不能带小数点
        assert "." not in fmt_size("100.0", "1")

    def test_small_lot(self) -> None:
        assert fmt_size("0.001", "0.001") == "0.001"

    def test_padding(self) -> None:
        # lot=0.01 暗示 2 位小数；3 应输出 "3.00"
        assert fmt_size("3", "0.01") == "3.00"


class TestFmtPrice:
    def test_decimal_tick(self) -> None:
        assert fmt_price("12345.7", "0.1") == "12345.7"

    def test_integer_tick(self) -> None:
        assert fmt_price("12345", "1") == "12345"

    @pytest.mark.parametrize(
        "value,tick,expected",
        [
            ("100.5", "0.5", "100.5"),
            ("0.0001234", "0.00000001", "0.00012340"),
        ],
    )
    def test_param(self, value: str, tick: str, expected: str) -> None:
        assert fmt_price(value, tick) == expected
