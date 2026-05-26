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
