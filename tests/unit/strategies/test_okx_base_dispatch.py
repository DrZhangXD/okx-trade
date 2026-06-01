"""drive_coro_sync 单测:NT backtest 下同步泵 rebalance 协程。

2026-06-01 修 backtest bug:factor_portfolio / funding_cross_section 的 on_bar 用
`loop.create_task(self._rebalance(...))` 派发 async rebalance。NT BacktestEngine
同步跑,这些 detached task 要等 run 结束才执行,那时 BacktestExecClient 已断开 →
submit_order 抛 `ValueError: not connected` → 0 成交、PnL 恒 0。修法:backtest 里
把协程同步泵到完成(其 await 在 backtest 都不阻塞:iso set-leverage 路径被跳过)。
"""
from __future__ import annotations

from okx_trade.strategies._okx_base import drive_coro_sync


class _SuspendOnce:
    """awaitable that suspends exactly once (no event loop needed)."""

    def __await__(self):
        yield


def test_completes_non_suspending_coroutine() -> None:
    calls: list[str] = []

    async def coro() -> None:
        calls.append("a")
        # 多个非阻塞 await(模拟 backtest 下 submit_isolated_order 走 cross 分支)
        await _noop()
        calls.append("b")

    async def _noop() -> None:
        return None

    done = drive_coro_sync(coro())
    assert done is True              # 一步 send 跑完
    assert calls == ["a", "b"]       # 整个 body(含非阻塞 await 后)都执行了


def test_detects_suspending_coroutine() -> None:
    async def coro() -> None:
        await _SuspendOnce()         # 真正挂起(模拟 live 的 set-leverage REST)

    c = coro()
    done = drive_coro_sync(c)
    assert done is False             # 挂起 → 调用方应交给事件循环
    c.close()
