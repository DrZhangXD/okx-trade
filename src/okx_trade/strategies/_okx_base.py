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


class OkxStrategyBase(_NTStrategy):  # type: ignore[misc,valid-type]
    """Optional base for OKX-aware strategies. See module docstring."""

    _iso_service: "IsolatedMarginService | None" = None
    _vol_filter: "VolatilityFilter | None" = None

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

        order.tags = list(order.tags or []) + self._iso_service.make_isolated_tags()
        self.submit_order(order)
        return True
