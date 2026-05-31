"""Per-strategy 下单频率熔断（TradeRateCheck）。

动机
----
2026-05-25 ``stat_arb_pairs`` 单日刷 5501 单,-550 USDT 几乎全是手续费,但整日
净值只跌 ~1.7%,**没触发 3% daily drawdown** —— ``DrawdownCheck`` 看的是净值跌幅,
挡不住"高频小亏 churn":每笔亏一点点 + 手续费,累积成大出血。

本 check 在**下单频率**层面兜底:滚动 ``window_sec`` 窗口内放行的 entry 数达到
``max_trades`` 即 REJECT 后续 entry,直到窗口滑过把旧记录剪掉。opt-in,默认关。

放在 pipeline **末尾**:``RiskManager`` 命中任一 REJECT 即早退,所以被
account-drawdown / kelly 等提前拒掉的 entry 不会走到这里、不计数;只有"通过其它
所有 check"的 entry 才被记一笔。命中上限只 REJECT,不缩仓(不返回 SCALE),
因此不干扰其它 check 的 sizing。
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from .base import RiskCheck, RiskCheckResult, RiskIntent


class TradeRateCheck(RiskCheck):
    """滚动窗口下单频率上限。

    Attributes:
        max_trades: 窗口内允许放行的 entry 数上限(达到即拒)。
        window_sec: 滚动窗口长度(秒)。
        _entries: 已放行 entry 的毫秒时间戳队列(左旧右新)。

    Args:
        now_ms: 注入的"当前毫秒"函数,默认 ``time.time()*1000``;测试用可控时钟。
    """

    name = "trade_rate"

    def __init__(
        self,
        *,
        max_trades: int,
        window_sec: int,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_trades <= 0:
            raise ValueError("max_trades must be > 0")
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        self.max_trades = max_trades
        self.window_sec = window_sec
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._entries: deque[int] = deque()

    def _prune(self, now: int) -> None:
        cutoff = now - self.window_sec * 1000
        while self._entries and self._entries[0] < cutoff:
            self._entries.popleft()

    def check(self, intent: RiskIntent) -> RiskCheckResult:
        now = self._now_ms()
        self._prune(now)
        if len(self._entries) >= self.max_trades:
            return RiskCheckResult.reject(
                f"trade rate {len(self._entries)}/{self.max_trades} "
                f"in {self.window_sec}s window",
                check_name=self.name,
            )
        self._entries.append(now)
        return RiskCheckResult.approve(intent.size, check_name=self.name)


__all__ = ["TradeRateCheck"]
