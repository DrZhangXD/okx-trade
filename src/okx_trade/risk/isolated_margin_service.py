"""OKX isolated-margin policy singleton service (2026-05-26 Phase 1).

Holds posMode cache + (inst, posSide) → lever cache + a shared OKXRestClient.
Strategies access via DI from build_live_context. See:
``docs/superpowers/specs/2026-05-26-isolated-margin-services-design.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..enums import PosSide, TdMode

if TYPE_CHECKING:
    from ..config import OKXSettings
    from ..rest.client import OKXRestClient


@dataclass(slots=True, frozen=True)
class BatchEnsureResult:
    """Result of ``IsolatedMarginService.batch_ensure_leverage``.

    ``all_ok`` is True only when every item's set-leverage succeeded (or was
    a cache hit). ``failed`` lists the (inst_id, error_msg) of failures so
    callers (multi-leg strategies) can log and abort their open-phase.
    """
    all_ok: bool
    failed: list[tuple[str, str]] = field(default_factory=list)


class IsolatedMarginService:
    """Singleton: one per process, owned by build_live_context.

    Subsequent tasks add ``get_pos_mode``, ``ensure_leverage``,
    ``batch_ensure_leverage``, ``make_isolated_tags``, ``is_backtest``.
    """

    def __init__(self, rest_settings: "OKXSettings", log) -> None:
        self._rest_settings = rest_settings
        self._rest: "OKXRestClient | None" = None
        self._lever_cache: dict[tuple[str, str], float] = {}
        self._pos_mode: str | None = None
        self._log = log
