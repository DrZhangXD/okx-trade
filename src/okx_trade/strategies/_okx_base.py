"""Thin OKX-aware Strategy base (2026-05-26 Phase 1).

Inheriting it gives a strategy:
  - DI slots for IsolatedMarginService and VolatilityFilter
  - submit_isolated_order(...) one-call helper for the typical isolated path
  - vol_filter_allow(...) convenience wrapper

Strategies don't HAVE to inherit it; the services are equally accessible
via direct attribute injection. But the helper reduces boilerplate.

In contexts where NautilusTrader isn't importable (pure-helper unit tests),
the base degrades to a thin ``object``-derived class so the helpers can
still be exercised via ``__get__`` binding.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

try:
    from nautilus_trader.trading.strategy import Strategy as _NTStrategy
    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover — NT always installed in this repo
    _NTStrategy = object  # type: ignore[assignment,misc]
    _NT_AVAILABLE = False

if TYPE_CHECKING:
    from ..enums import PosSide
    from ..risk.isolated_margin_service import IsolatedMarginService
    from ..risk.volatility_filter import VolatilityFilter


def drive_coro_sync(coro) -> bool:
    """把协程同步推进一步;完成(StopIteration)返回 True,挂起(yield 了 future）
    返回 False。

    NT backtest 修复(2026-06-01）:``on_bar`` 是同步回调,async rebalance 过去用
    ``loop.create_task`` 派发。但 NT ``BacktestEngine`` 同步跑数据流,这些 detached
    task 要等 run 结束才被事件循环执行,那时 ``BacktestExecClient`` 已断开 →
    ``submit_order`` 抛 ``ValueError: not connected`` → 0 成交。

    在 backtest 里 rebalance 协程的 await 都不阻塞(``submit_isolated_order`` 因
    ``_iso_service is None`` 走 cross 分支、不 await 真实 I/O),所以一次 ``send(None)``
    即跑完 → 订单在 ``on_bar`` 同步上下文里、exec client 连接时提交。live 路径会因
    真实 ``await``(set-leverage REST)挂起 → 返回 False,由调用方交回事件循环。
    """
    try:
        coro.send(None)
    except StopIteration:
        return True
    return False


class OkxStrategyBase(_NTStrategy):  # type: ignore[misc,valid-type]
    """Optional base for OKX-aware strategies. See module docstring."""

    _iso_service: "IsolatedMarginService | None" = None
    _vol_filter: "VolatilityFilter | None" = None

    def _in_backtest(self) -> bool:
        """NT ``BacktestEngine`` 用 ``TestClock``,live ``TradingNode`` 用 ``LiveClock``。"""
        return type(getattr(self, "clock", None)).__name__ == "TestClock"

    def dispatch_rebalance(self, coro) -> None:
        """派发一个 async rebalance 协程。backtest 同步泵到完成(否则订单在
        exec client 断开后提交 → 'not connected');live 交给事件循环 create_task。"""
        if self._in_backtest():
            if not drive_coro_sync(coro):
                coro.close()
                self.log.warning(
                    "rebalance suspended in backtest (unexpected real await); skipped",
                )
            return
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(coro)
            # 保引用防 GC(detached task 没人持有会被回收 → "Task was destroyed")
            tasks = getattr(self, "_rebalance_tasks", None)
            if tasks is None:
                tasks = set()
                self._rebalance_tasks = tasks
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        except RuntimeError:
            coro.close()
            self.log.warning("no running loop; rebalance skipped")

    def vol_filter_allow(self, inst_id_okx: str) -> tuple[bool, str]:
        """Convenience wrapper.

        Returns ``(True, "no_filter")`` if no filter is injected (e.g.,
        backtest); else delegates to ``self._vol_filter.allow(inst_id_okx)``.
        """
        if self._vol_filter is None:
            return True, "no_filter"
        return self._vol_filter.allow(inst_id_okx)

    async def submit_isolated_order(
        self, order, *, lever: float, pos_side: "PosSide | None" = None,
    ) -> bool:
        """One-call isolated-margin order submit. Returns True iff the order
        was submitted to OKX.

        Reads ``enable_isolated_margin`` from ``self.config``. Each strategy
        that uses this helper must declare ``enable_isolated_margin: bool``
        in its Config dataclass.

        7 branches (see corresponding unit tests):
          1. config.enable_isolated_margin = False     → submit cross, return True
          2. _iso_service is None                      → submit cross, return True
          3. _iso_service.is_backtest()                → submit cross, return True
          4. net_mode (forces pos_side=None)           → ensure + tag + submit
          5. long_short_mode (derive pos_side if None) → ensure + tag + submit
          6. ensure_leverage fails                     → log + return False (no submit)
          7. happy path                                → submit isolated, return True
        """
        # Branches 1-3: cross fallback
        if (not getattr(self.config, "enable_isolated_margin", False)
                or self._iso_service is None
                or self._iso_service.is_backtest()):
            self.submit_order(order)
            return True

        # Resolve pos_side per account posMode
        pos_mode = await self._iso_service.get_pos_mode()
        if pos_mode == "long_short_mode":
            if pos_side is None:
                # Lazy import — NT order side enum. Falls back to string
                # comparison for stubs that pass plain "BUY"/"SELL".
                is_buy = False
                try:
                    from nautilus_trader.model.enums import OrderSide
                    is_buy = order.side == OrderSide.BUY
                except ImportError:
                    is_buy = False
                if not is_buy:
                    is_buy = str(order.side).upper().endswith("BUY")
                from ..enums import PosSide
                pos_side = PosSide.LONG if is_buy else PosSide.SHORT
        else:
            pos_side = None  # net_mode: account.py auto-fills NET

        inst_id_okx = order.instrument_id.value.split(".")[0]
        ok, err = await self._iso_service.ensure_leverage(inst_id_okx, lever, pos_side)
        if not ok:
            self.log.warning(
                f"{type(self).__name__} skip leg inst={inst_id_okx} "
                f"(set_leverage failed: {err})"
            )
            return False

        # NT Order.tags is a read-only Cython attribute — can't be reassigned
        # after construction. Rebuild the order through order_factory with
        # merged tags. Optimized for MarketOrder (the dominant case in this
        # repo's strategies); falls back to plain submit on rebuild failure.
        iso_tags = self._iso_service.make_isolated_tags()
        existing_tags = list(order.tags) if order.tags else []
        new_tags = existing_tags + iso_tags

        try:
            rebuilt = self.order_factory.market(
                instrument_id=order.instrument_id,
                order_side=order.side,
                quantity=getattr(order, "quantity", None),
                time_in_force=getattr(order, "time_in_force", None),
                reduce_only=getattr(order, "is_reduce_only", False),
                tags=new_tags,
            )
            self.submit_order(rebuilt)
        except (AttributeError, TypeError) as exc:
            self.log.warning(
                f"{type(self).__name__} submit_isolated_order: failed to "
                f"rebuild order with isolated tag ({exc}); falling back to "
                f"plain submit (no isolated tag)"
            )
            self.submit_order(order)
        return True
