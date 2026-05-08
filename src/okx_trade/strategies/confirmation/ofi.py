"""OFI（OrderFlow Imbalance）确认门控。

直觉
----
OKX ``trades`` 频道每条成交都标了 ``side``（taker 方向）。把最近 N 秒内
的 buy / sell taker 名义额按 side 分桶累加，可得到一个 [-1, +1] 的标量：

    OFI = (buy_qty - sell_qty) / (buy_qty + sell_qty)

OFI 越大说明 taker 在主动买（市场向上压力），越负则相反。

用法：策略下单前问 OFI 一句「同向吗？」，
- **同向**（OFI 与 intended direction 一致）→ 正常下单；
- **弱反向**（OFI 与 intended 相反但幅度小）→ 仓位缩 50%；
- **强反向** → 跳过这次入场。

为什么用 trades 流不用 books5？
- ``books5`` 的 bid/ask size 受 quoter 行为影响重，做市商挂单/撤单频繁，
  瞬时 imbalance 噪声大；
- ``trades`` 的 ``side`` 是真实成交方向（taker），抗噪、语义明确，对应
  「实际花钱进场的资金压力」。

输出与单测都是纯函数，``OFIFilter`` 只是带 deque 状态的薄包装。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

Direction = Literal["long", "short"]
TradeSide = Literal["buy", "sell"]
OFIDecision = Literal["pass", "scale_50", "skip"]


@dataclass(frozen=True, slots=True)
class TradeFlow:
    """单条 taker 成交的简化记录。

    Attributes:
        ts_ms: 成交时间戳（毫秒，OKX 原生）。
        side: ``"buy"`` 表示 taker 主动买入；``"sell"`` 反之。
        sz: 成交张数（合约）或基础币数量（现货）—— 单位语义由调用方统一即可，
            ``ofi_score`` 只看相对量。
    """

    ts_ms: int
    side: TradeSide
    sz: float


def ofi_score(
    trades: deque[TradeFlow] | list[TradeFlow],
    window_ms: int,
    now_ms: int,
) -> float:
    """对最近 ``window_ms`` 内的 trades 算 OFI 标量。

    Args:
        trades: 升序时间序列（``ts_ms`` 单调不减）。
        window_ms: 取 ``[now_ms - window_ms, now_ms]`` 区间内的 trades。
        now_ms: 当前时间戳（毫秒）。

    Returns:
        ``[-1.0, 1.0]`` 的浮点；空区间或全零 size 返回 ``0.0``。
    """
    cutoff = now_ms - window_ms
    buy_qty = 0.0
    sell_qty = 0.0
    for t in trades:
        if t.ts_ms < cutoff:
            continue
        if t.side == "buy":
            buy_qty += t.sz
        else:
            sell_qty += t.sz
    total = buy_qty + sell_qty
    if total <= 0.0:
        return 0.0
    return (buy_qty - sell_qty) / total


def ofi_filter_decision(
    ofi: float,
    intended: Direction,
    same_dir_threshold: float = 0.1,
    counter_dir_threshold: float = -0.3,
) -> OFIDecision:
    """三档过滤决策。

    定义「同向 OFI」 ``=  +ofi`` 当 long、``= -ofi`` 当 short。

    - ``aligned >= same_dir_threshold``  → ``pass``（趋势同向，正常下单）
    - ``counter_dir_threshold <= aligned < same_dir_threshold`` → ``scale_50``（弱反向 / 中性，缩半仓）
    - ``aligned < counter_dir_threshold`` → ``skip``（强反向，跳过）

    Args:
        ofi: ``ofi_score`` 输出，``[-1, 1]``。
        intended: 策略想下的方向。
        same_dir_threshold: 同向通过的最低 ``aligned`` 值。默认 +0.1。
        counter_dir_threshold: 强反向阈值（注意：这是「``aligned`` 多负」，
            **小于**该值表示强反向）。默认 -0.3。

    Returns:
        ``"pass"`` / ``"scale_50"`` / ``"skip"``。
    """
    aligned = ofi if intended == "long" else -ofi
    if aligned >= same_dir_threshold:
        return "pass"
    if aligned < counter_dir_threshold:
        return "skip"
    return "scale_50"


class OFIFilter:
    """带 deque 状态的 OFI 门控。

    用法::

        ofi_filter = OFIFilter(window_ms=60_000, maxlen=10_000)

        # 在 trades WS 回调里：
        ofi_filter.update(TradeFlow(ts_ms=..., side="buy", sz=...))

        # 策略下单前：
        decision = ofi_filter.evaluate("long", now_ms=current_ts_ms)
        if decision == "skip":
            return
        size = base_size * (0.5 if decision == "scale_50" else 1.0)
    """

    def __init__(
        self,
        window_ms: int = 60_000,
        maxlen: int = 10_000,
        same_dir_threshold: float = 0.1,
        counter_dir_threshold: float = -0.3,
    ) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._window_ms = window_ms
        self._same = same_dir_threshold
        self._counter = counter_dir_threshold
        self._buf: deque[TradeFlow] = deque(maxlen=maxlen)

    @property
    def window_ms(self) -> int:
        return self._window_ms

    def update(self, trade: TradeFlow) -> None:
        """追加一条 trade。窗口外的旧数据靠 deque maxlen 自然淘汰。"""
        self._buf.append(trade)

    def score(self, now_ms: int) -> float:
        return ofi_score(self._buf, self._window_ms, now_ms)

    def evaluate(self, intended: Direction, now_ms: int) -> OFIDecision:
        return ofi_filter_decision(
            self.score(now_ms),
            intended,
            same_dir_threshold=self._same,
            counter_dir_threshold=self._counter,
        )

    def __len__(self) -> int:
        return len(self._buf)


__all__ = [
    "Direction",
    "OFIDecision",
    "OFIFilter",
    "TradeFlow",
    "TradeSide",
    "ofi_filter_decision",
    "ofi_score",
]
