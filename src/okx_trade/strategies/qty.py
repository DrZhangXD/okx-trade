"""共享下单辅助：把浮点 qty 安全地量化为 NT ``Quantity``。

动机
----
NT 的 ``Instrument.make_qty`` 在传入值四舍五入到 0（小于 ``size_increment / 2``）时会抛
``ValueError``；这会冒到 ``Strategy.on_bar`` / ``on_event`` 之外，被 NT
``DataEngine`` 捕获后 **整个 TradingNode 终止**（observed in M5 paper trading on 2026-05-09:
``XSMomentumStrategy`` 在 RiskManager / Kelly 缩放后 qty=0.575、size_increment=1
触发 ``ValueError(... rounded to zero ...)``，systemd 反复重启 6 次）。

策略层有 9 处裸 ``inst.make_qty(Decimal(str(qty)))``，全部存在隐患。本 helper 统一封装：

1. 用 ``ROUND_DOWN`` 把 qty 对齐到 ``size_increment`` 整数倍；
2. 若对齐后 ``<=0``（即原 qty < lot_sz），返回 ``None`` 让调用方 skip；
3. 否则返回 ``Quantity``。

对 RiskManager / Kelly 缩放后的尺寸再次做 lot 对齐，是核心目的。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nautilus_trader.common.component import Logger
    from nautilus_trader.model.instruments.base import Instrument
    from nautilus_trader.model.objects import Quantity


def safe_make_qty(
    inst: "Instrument",
    qty: float | Decimal,
    log: "Logger | None" = None,
    *,
    ctx: str = "",
) -> "Quantity | None":
    """量化 ``qty`` 到 ``inst.size_increment`` 整数倍并构造 ``Quantity``。

    Parameters
    ----------
    inst
        NT ``Instrument`` 对象（必须有 ``size_increment`` 与 ``make_qty``）。
    qty
        想下单的张数（绝对值或带符号都可以，函数取 ``abs``）。
    log
        可选：传入 strategy 的 logger，用于记录 skip 原因。
    ctx
        可选：日志上下文（"rebalance BTC-USDT-SWAP" 之类），便于排查。

    Returns
    -------
    Quantity | None
        - ``Quantity`` —— 调用方应用此值下单
        - ``None``     —— qty 对齐后变成 0，调用方应跳过下单（不应再调 ``make_qty``）
    """
    lot_sz = float(inst.size_increment)
    if lot_sz <= 0:
        # instrument_provider 应当已过滤；保险起见还是 skip
        if log is not None:
            log.warning(f"safe_make_qty: lot_sz<=0 for {inst.id} ({ctx}), skip")
        return None

    qty_abs = abs(float(qty))
    step = Decimal(str(lot_sz))
    raw = Decimal(str(qty_abs))
    aligned = (raw / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step

    if aligned <= 0:
        if log is not None:
            log.info(
                f"safe_make_qty: qty={qty_abs} < lot_sz={lot_sz} for {inst.id} ({ctx}), skip"
            )
        return None

    return inst.make_qty(aligned)


__all__ = ["safe_make_qty"]
