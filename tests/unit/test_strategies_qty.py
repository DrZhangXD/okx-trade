"""``safe_make_qty`` 单测：策略层下单前量化 + 守门，避免 NT ``make_qty``
在 round-to-zero 时抛 ``ValueError`` 把整个 TradingNode 拖崩。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

import pytest

from okx_trade.strategies.qty import safe_make_qty


class _FakeInstrument:
    """最小化 NT Instrument stand-in：只暴露 ``size_increment`` 与 ``make_qty``。

    ``make_qty`` 模拟 NT 真实行为：把传入值按 ``size_increment`` 的整数倍 ROUND_HALF_EVEN
    量化，若结果 == 0，抛 ``ValueError``（与 NT 1.226 实际行为一致，见
    ``nautilus_trader/model/instruments/base.pyx`` ``Instrument.make_qty``）。
    """

    def __init__(self, size_increment: float, *, id: str = "FAKE-USDT-SWAP.OKX") -> None:
        self.size_increment = size_increment
        self.id = id

    def make_qty(self, value):  # noqa: ANN001 - mimic NT signature
        d = Decimal(value) if not isinstance(value, Decimal) else value
        quant = Decimal(str(self.size_increment))
        # 用 size_increment 倍数为单位 ROUND_HALF_EVEN，再乘回去
        rounded = (d / quant).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN) * quant
        if rounded == 0:
            raise ValueError(
                f"Invalid `value` for quantity: {d} was rounded to zero "
                f"due to size increment {self.size_increment}"
            )
        return rounded  # 测试里不需要真 Quantity 类型


class _RecorderLog:
    """简易 logger 桩，记录 info/warning 调用次数与最后消息。"""

    def __init__(self) -> None:
        self.info_msgs: list[str] = []
        self.warning_msgs: list[str] = []
        self.error_msgs: list[str] = []

    def info(self, msg: str) -> None:
        self.info_msgs.append(msg)

    def warning(self, msg: str) -> None:
        self.warning_msgs.append(msg)

    def error(self, msg: str) -> None:
        self.error_msgs.append(msg)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


class TestSafeMakeQtyHappyPath:
    def test_aligned_qty_passes_through(self) -> None:
        inst = _FakeInstrument(size_increment=1.0)
        result = safe_make_qty(inst, 3.0)
        assert result == Decimal("3")

    def test_qty_above_lot_aligned_down(self) -> None:
        inst = _FakeInstrument(size_increment=1.0)
        # 3.7 → ROUND_DOWN → 3
        result = safe_make_qty(inst, 3.7)
        assert result == Decimal("3")

    def test_fractional_lot_size(self) -> None:
        inst = _FakeInstrument(size_increment=0.01)
        result = safe_make_qty(inst, 0.0567)
        assert result == Decimal("0.05")

    def test_negative_qty_uses_abs(self) -> None:
        # 调用方可能传 delta 带正负号
        inst = _FakeInstrument(size_increment=1.0)
        result = safe_make_qty(inst, -3.0)
        assert result == Decimal("3")

    def test_decimal_input_accepted(self) -> None:
        inst = _FakeInstrument(size_increment=0.001)
        result = safe_make_qty(inst, Decimal("0.123456"))
        assert result == Decimal("0.123")


# ---------------------------------------------------------------------------
# 关键防御：返回 None 而非抛异常
# ---------------------------------------------------------------------------


class TestSafeMakeQtyReturnsNone:
    def test_below_lot_size_returns_none_not_raise(self) -> None:
        """复现 5/9 14:39:13 那次崩：Kelly 缩到 0.575，size_increment=1。"""
        inst = _FakeInstrument(size_increment=1.0)
        result = safe_make_qty(inst, 0.575)
        assert result is None  # 关键：没抛 ValueError

    def test_zero_qty_returns_none(self) -> None:
        inst = _FakeInstrument(size_increment=1.0)
        assert safe_make_qty(inst, 0.0) is None

    def test_qty_just_under_increment(self) -> None:
        inst = _FakeInstrument(size_increment=0.1)
        assert safe_make_qty(inst, 0.099) is None

    def test_invalid_lot_size_returns_none(self) -> None:
        # 防御性：lot_sz <= 0 不应该发生（provider 会过滤），但还是 skip
        inst = _FakeInstrument(size_increment=0.0)
        assert safe_make_qty(inst, 5.0) is None


# ---------------------------------------------------------------------------
# 日志记录
# ---------------------------------------------------------------------------


class TestSafeMakeQtyLogging:
    def test_skip_logs_info_when_log_provided(self) -> None:
        inst = _FakeInstrument(size_increment=1.0)
        log = _RecorderLog()
        safe_make_qty(inst, 0.5, log, ctx="rebalance BTC-USDT-SWAP")
        assert len(log.info_msgs) == 1
        assert "0.5" in log.info_msgs[0]
        assert "lot_sz=1" in log.info_msgs[0]
        assert "rebalance BTC-USDT-SWAP" in log.info_msgs[0]

    def test_skip_silent_when_no_log(self) -> None:
        # exit 路径有时不传 log 进 helper（自己在外面记 error）
        inst = _FakeInstrument(size_increment=1.0)
        result = safe_make_qty(inst, 0.5)  # 没 log
        assert result is None  # 不应该报错

    def test_invalid_lot_logs_warning(self) -> None:
        inst = _FakeInstrument(size_increment=0.0)
        log = _RecorderLog()
        safe_make_qty(inst, 5.0, log, ctx="enter")
        assert len(log.warning_msgs) == 1
        assert "lot_sz<=0" in log.warning_msgs[0]

    def test_happy_path_no_log(self) -> None:
        inst = _FakeInstrument(size_increment=1.0)
        log = _RecorderLog()
        result = safe_make_qty(inst, 3.0, log)
        assert result == Decimal("3")
        # 正常路径不该刷日志
        assert log.info_msgs == []
        assert log.warning_msgs == []


# ---------------------------------------------------------------------------
# 回归：保证不再触发 NT ValueError 路径
# ---------------------------------------------------------------------------


class TestSafeMakeQtyNoValueErrorRegression:
    @pytest.mark.parametrize(
        "size_increment,qty",
        [
            (1.0, 0.25),    # 复现 5/9 14:39:13 那次崩的实际值（traceback 里是 0.25）
            (1.0, 0.4),
            (1.0, 0.499),
            (0.1, 0.04),
            (0.01, 0.0049),
            (10.0, 4.0),
        ],
    )
    def test_below_half_increment_does_not_raise(
        self, size_increment: float, qty: float
    ) -> None:
        inst = _FakeInstrument(size_increment=size_increment)
        # 这些 qty 直接传给 inst.make_qty 都会 raise（NT round HALF_EVEN to 0）
        with pytest.raises(ValueError):
            inst.make_qty(Decimal(str(qty)))
        # safe_make_qty 必须返回 None 而不是 raise
        assert safe_make_qty(inst, qty) is None
