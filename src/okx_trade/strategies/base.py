"""策略层共享工具。

包含：
- ``ScalpSignal`` —— 策略输出的入场信号（NT 无关，可独立单测）
- ``BarBuffer`` —— 滚动 K 线缓冲，持有最近 N 根 confirm 的 ``Bar``
- 仓位计算辅助（按风险比例反推合约张数）
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

Direction = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class ScalpSignal:
    """策略层输出的入场信号——NT 无关。

    上层（NT Strategy）拿到 Signal 后构造 NT MarketOrder + 跟踪 SL/TP。
    """

    direction: Direction
    entry_price: float
    stop_price: float
    take_profit_price: float
    breakout_extreme: float
    used_swing_stop: bool
    trigger_bar_ts: int      # 触发那根 bar 的 close 时间（毫秒，用于回测对齐）
    reason: str

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def reward_distance(self) -> float:
        return abs(self.take_profit_price - self.entry_price)


class BarBuffer:
    """简单的环形 K 线缓冲。

    NT 自己有 ``Cache.bars()`` 但每次 query 都要 list 拷贝；策略热路径里我们维护
    自己的 deque，按需迭代更轻量。
    """

    def __init__(self, maxlen: int) -> None:
        self._buf: deque[BarSnapshot] = deque(maxlen=maxlen)

    def append(self, bar: BarSnapshot) -> None:
        self._buf.append(bar)

    def __len__(self) -> int:
        return len(self._buf)

    def __iter__(self) -> Iterator[BarSnapshot]:
        return iter(self._buf)

    def tail(self, n: int) -> list[BarSnapshot]:
        if n >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-n:]

    def latest(self) -> BarSnapshot | None:
        return self._buf[-1] if self._buf else None


@dataclass(frozen=True, slots=True)
class BarSnapshot:
    """轻量 K 线快照（与 NT Bar 解耦，便于纯函数测试）。

    ts: 毫秒时间戳（OKX 原生）
    """

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    confirm: bool = True


def position_contracts(
    account_equity_usdt: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    ct_val: float,
    min_sz: float,
    lot_sz: float,
) -> float:
    """按风险百分比反推下单合约张数。

    公式：
        risk_amount = equity × risk_pct           # 单笔最大可亏 USDT
        coins       = risk_amount / |entry-stop|  # 可承担多少枚币
        contracts   = coins / ct_val              # 转为合约张数
        contracts   = floor_to(lot_sz)            # 向下对齐到 lot_sz 的倍数

    - 向下对齐避免「超过本金」；
    - 用 Decimal 避免 ``0.29 // 0.01 = 28`` 这类浮点坑（来自 scalp 经验）。

    Args:
        account_equity_usdt: 账户净值（USDT 计价）。
        risk_pct: 单笔最大风险占净值比例（0.005 = 0.5%）。
        entry_price / stop_price: 入场价 / 止损价。
        ct_val: 合约面值（如 BTC-USDT-SWAP 是 0.01 BTC）。
        min_sz / lot_sz: OKX instrument 的最小下单量 / 数量精度。

    Returns:
        合约张数（float，已对齐到 lot_sz）。``0.0`` 表示不下单（风险太小或参数无效）。
    """
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0 or ct_val <= 0 or account_equity_usdt <= 0:
        return 0.0
    risk_amount = account_equity_usdt * risk_pct
    coins = risk_amount / stop_dist
    raw_contracts = coins / ct_val
    step = lot_sz if lot_sz > 0 else 1.0

    step_d = Decimal(str(step))
    raw_d = Decimal(str(raw_contracts))
    contracts_d = (raw_d / step_d).quantize(Decimal("1"), rounding=ROUND_DOWN) * step_d
    contracts = float(contracts_d)
    if contracts < min_sz:
        return 0.0
    return contracts


def to_bar_snapshots(bars: Iterable) -> list[BarSnapshot]:
    """把 NT ``Bar`` 序列转成 ``BarSnapshot`` 序列（便于走纯函数管线）。

    NT Bar 的 ts 是纳秒，这里转毫秒。
    """
    out: list[BarSnapshot] = []
    for b in bars:
        out.append(BarSnapshot(
            ts=int(b.ts_event // 1_000_000),
            open=b.open.as_double(),
            high=b.high.as_double(),
            low=b.low.as_double(),
            close=b.close.as_double(),
            volume=b.volume.as_double(),
            confirm=True,  # NT bar 推送默认 confirmed
        ))
    return out


__all__ = [
    "BarBuffer",
    "BarSnapshot",
    "Direction",
    "ScalpSignal",
    "position_contracts",
    "to_bar_snapshots",
]
