"""价格 / 数量精度对齐。

scalp 项目的 ``_round_to_tick`` 和 ``_fmt`` 用 float 实现，存在精度风险（特别是
``0.1 + 0.2 != 0.3`` 这类经典浮点坑会传染到下单大小）。本模块用 ``Decimal``
重写，输入接受 ``Decimal | float | int | str``，内部统一转 Decimal。
"""
from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

Number = Decimal | float | int | str


def _to_decimal(x: Number) -> Decimal:
    """统一转 Decimal。float 先转 str 避免 repr 噪声（如 0.1 → '0.1' 而非
    '0.1000000000000000055511151231257827021181583404541015625'）。"""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(str(x))
    return Decimal(x)


def round_to_tick(price: Number, tick: Number) -> Decimal:
    """将价格四舍五入到 tick 的整数倍。

    >>> round_to_tick("12345.678", "0.1")
    Decimal('12345.7')
    >>> round_to_tick("12345.64", "0.5")
    Decimal('12345.5')
    """
    p = _to_decimal(price)
    t = _to_decimal(tick)
    if t <= 0:
        return p
    # quantize 到 tick 的小数位，再按 tick 的整数倍对齐
    n = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (n * t).quantize(t)


def floor_to_lot(size: Number, lot: Number) -> Decimal:
    """向下对齐到 lot 的整数倍——下单数量必须是 lot 的倍数，向下避免超过本金。

    >>> floor_to_lot("3.7", "0.1")
    Decimal('3.7')
    >>> floor_to_lot("3.78", "0.1")
    Decimal('3.7')
    >>> floor_to_lot("3.78", "1")
    Decimal('3')
    """
    s = _to_decimal(size)
    lt = _to_decimal(lot)
    if lt <= 0:
        return s
    n = (s / lt).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return (n * lt).quantize(lt)


def fmt_size(value: Number, lot: Number) -> str:
    """按 lot 的小数位数把数量格式化为字符串（OKX API 接受 string）。

    OKX 对 sz 字段较严格：必须是 lot_sz 的倍数，且小数位数不能超过 lot_sz 暗示的精度。

    >>> fmt_size("3.7", "0.1")
    '3.7'
    >>> fmt_size("100", "1")
    '100'
    """
    v = _to_decimal(value)
    lt = _to_decimal(lot)
    decimals = _decimal_places(lt)
    quantized = v.quantize(Decimal(10) ** -decimals if decimals > 0 else Decimal("1"))
    if decimals == 0:
        return f"{int(quantized)}"
    return f"{quantized:.{decimals}f}"


def fmt_price(value: Number, tick: Number) -> str:
    """按 tick 的小数位数把价格格式化为字符串。"""
    v = _to_decimal(value)
    tk = _to_decimal(tick)
    decimals = _decimal_places(tk)
    quantized = v.quantize(Decimal(10) ** -decimals if decimals > 0 else Decimal("1"))
    if decimals == 0:
        return f"{int(quantized)}"
    return f"{quantized:.{decimals}f}"


def _decimal_places(d: Decimal) -> int:
    """计算 Decimal 的有效小数位数。``0.001`` → 3，``1`` → 0，``0.5`` → 1。"""
    s = format(d.normalize(), "f")
    if "." in s:
        return len(s.split(".", 1)[1])
    return 0


__all__ = [
    "floor_to_lot",
    "fmt_price",
    "fmt_size",
    "round_to_tick",
]
