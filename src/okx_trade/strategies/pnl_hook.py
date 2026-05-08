"""策略 ↔ PnL tracker 的胶水（M5 闭环 M3 占位）。

为什么单独一个 module？
----------------------
- ``pnl/`` 不应该知道策略实例细节（``ScalpSignal`` / ``_active_direction`` 等）；
- 各策略的 entry/exit 逻辑差异较大，但所有策略都要做同两件事：
  1. 平仓时把 ``(pnl_usdt, r_multiple)`` 写进 tracker；
  2. 每个 UTC 日切换时，把账户 equity snapshot 写进 tracker。
- 这里抽出三个无状态 helper，每个策略自己调即可。

入口函数：

| 函数 | 用途 |
|---|---|
| ``realized_pnl_and_r`` | 纯函数：(entry/exit/contracts/ct_val/direction/risk_usdt) → (pnl_usdt, r) |
| ``record_strategy_trade`` | tracker 不为 None 时写一条 trade；否则静默跳过 |
| ``record_strategy_equity_daily`` | 同 day 已写过则跳过；否则写并返回新 day（用于状态记忆） |
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..pnl import PnLTracker

Direction = Literal["long", "short"]


def realized_pnl_and_r(
    *,
    direction: Direction,
    entry_price: float,
    exit_price: float,
    contracts: float,
    ct_val: float,
    risk_usdt: float,
) -> tuple[Decimal, float]:
    """近似计算一笔已平仓交易的 (PnL_usdt, r_multiple)。

    PnL = sign × (exit - entry) × contracts × ct_val
    r   = PnL / risk_usdt   （risk_usdt = equity × risk_pct，即入场时承担的最大亏损）

    Args:
        direction: ``long`` / ``short``。
        entry_price: 入场价。
        exit_price: 平仓价（SL 触发用 stop_price 近似；TP 用 tp_price；其余用
            bar.close）。
        contracts: 平仓张数。
        ct_val: 合约面值（USDT-Margined SWAP 的 multiplier，如 BTC = 0.01）。
        risk_usdt: 入场时的"风险预算"（USDT，正值），用于算 r_multiple。0 时
            r_multiple 取 0（避免 div-by-zero）。

    Returns:
        ``(pnl_usdt, r_multiple)``。``pnl_usdt`` 是 ``Decimal``（写库）。
    """
    sign = 1.0 if direction == "long" else -1.0
    pnl_f = sign * (exit_price - entry_price) * contracts * ct_val
    r = pnl_f / risk_usdt if risk_usdt > 0 else 0.0
    return Decimal(str(pnl_f)), float(r)


def record_strategy_trade(
    tracker: PnLTracker | None,
    *,
    strategy_id: str,
    instrument_id: str,
    closed_ts_ms: int,
    direction: Direction,
    entry_price: float,
    exit_price: float,
    contracts: float,
    ct_val: float,
    risk_usdt: float,
) -> None:
    """把一笔已平仓交易写进 tracker。``tracker is None`` 时静默跳过。"""
    if tracker is None:
        return
    pnl, r = realized_pnl_and_r(
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        contracts=contracts,
        ct_val=ct_val,
        risk_usdt=risk_usdt,
    )
    from ..pnl import TradeRecord
    tracker.record_trade(TradeRecord(
        strategy_id=strategy_id,
        instrument_id=instrument_id,
        closed_ts_ms=closed_ts_ms,
        pnl_usdt=pnl,
        r_multiple=r,
    ))


def utc_day(ts_ms: int) -> str:
    """ts_ms → ``"YYYY-MM-DD"``（UTC）。"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def record_strategy_equity_daily(
    tracker: PnLTracker | None,
    *,
    strategy_id: str,
    ts_ms: int,
    equity_usdt: float,
    last_day: str | None,
) -> str | None:
    """如果 ``ts_ms`` 跨入新 UTC 日（或第一次），写一条 equity snapshot。

    Args:
        tracker: ``None`` 时静默跳过。
        strategy_id / ts_ms / equity_usdt: snapshot 内容。
        last_day: 该策略上一次 snapshot 的 UTC 日字符串；首次传 ``None``。

    Returns:
        - 写入了 → 返回新的 ``"YYYY-MM-DD"``，调用方应保存供下次比对；
        - 未写（同日 / tracker is None） → 返回 ``last_day``。
    """
    if tracker is None or equity_usdt <= 0:
        return last_day
    today = utc_day(ts_ms)
    if today == last_day:
        return last_day
    from ..pnl import EquitySnapshot
    tracker.record_equity(EquitySnapshot(
        strategy_id=strategy_id,
        ts_ms=ts_ms,
        equity_usdt=Decimal(str(equity_usdt)),
    ))
    return today


__all__ = [
    "realized_pnl_and_r",
    "record_strategy_equity_daily",
    "record_strategy_trade",
    "utc_day",
]
