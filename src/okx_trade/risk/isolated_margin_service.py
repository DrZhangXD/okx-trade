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
        # Cache value is the **rounded int** actually sent to OKX, not the
        # caller's continuous float. Strategies compute lever as a function of
        # edge_score and pass values like 3.81 / 3.95 / 5.55 that change every
        # rebalance even when OKX's effective leverage (int) hasn't moved.
        # Caching the int eliminates redundant API calls — important during
        # funding-hour bursts when OKX is most likely to return 50004/51290.
        self._lever_cache: dict[tuple[str, str], int] = {}
        self._pos_mode: str | None = None
        self._log = log

    def make_isolated_tags(self) -> list[str]:
        """Convenience: returns ``["td_mode:isolated"]`` for OrderTags."""
        return ["td_mode:isolated"]

    def is_backtest(self) -> bool:
        """Heuristic: empty / None api_key → backtest context.

        OKXSettings.api_key is pydantic ``SecretStr``. Empty SecretStr is
        falsy via ``get_secret_value()``. None is also falsy. Real keys
        evaluate truthy.
        """
        api_key_attr = getattr(self._rest_settings, "api_key", None)
        if api_key_attr is None:
            return True
        # SecretStr: extract underlying value if present; else use as-is
        try:
            value = api_key_attr.get_secret_value()
        except AttributeError:
            value = api_key_attr
        return not bool(value)

    async def _ensure_rest_client(self) -> "OKXRestClient":
        """Lazy-init the OKXRestClient on first REST need."""
        if self._rest is None:
            from ..rest.client import OKXRestClient
            self._rest = OKXRestClient(self._rest_settings)
            await self._rest.__aenter__()
        return self._rest

    async def get_pos_mode(self) -> str:
        """Cached fetch of OKX account posMode. Returns 'net_mode' or
        'long_short_mode'. Unexpected values log a strong WARN and fall
        back to 'net_mode'; REST failures also fall back silently with
        a WARN. Cached for service lifetime (mode is account-level).
        """
        if self._pos_mode is not None:
            return self._pos_mode
        rest = await self._ensure_rest_client()
        try:
            data = await rest.transport.request(
                "GET", "/api/v5/account/config",
                private=True, group=None,
            )
            if data and isinstance(data, list) and data[0]:
                mode = data[0].get("posMode")
                if mode in ("net_mode", "long_short_mode"):
                    self._pos_mode = str(mode)
                    self._log.info(f"iso_margin cached account posMode={mode}")
                    return self._pos_mode
                self._log.warning(
                    f"iso_margin UNEXPECTED posMode={mode!r} from OKX "
                    f"/account/config (expected net_mode or long_short_mode); "
                    f"falling back to net_mode — set-leverage may fail on "
                    f"a long_short account"
                )
        except Exception as exc:
            self._log.warning(
                f"iso_margin get_pos_mode failed: {exc}; falling back to net_mode"
            )
        self._pos_mode = "net_mode"
        return self._pos_mode

    async def ensure_leverage(
        self,
        inst_id: str,
        lever: float,
        pos_side: PosSide | None,
    ) -> tuple[bool, str | None]:
        """Idempotent OKX ``set-leverage`` for one (inst, posSide).

        Args:
            inst_id: OKX format ("BTC-USDT-SWAP") or NT format with ".OKX"
                suffix (service strips internally).
            lever: target leverage; rounded to int for OKX.
            pos_side: ``PosSide.LONG``/``PosSide.SHORT`` in long_short_mode;
                ``None`` in net_mode (``account.py`` auto-fills
                ``PosSide.NET`` for the isolated case).

        Returns:
            ``(True, None)`` on success or cache hit;
            ``(False, error_msg)`` on REST failure — caller skips the leg.
        """
        ps_key = pos_side.value if pos_side is not None else "net"
        inst_id_okx = inst_id.split(".")[0]
        cache_key = (inst_id_okx, ps_key)
        # OKX only accepts integer leverage; quantize before cache compare so
        # 3.81 vs 3.95 (both round to 4) hit the same cache entry. Prior impl
        # cached the raw float with 0.01 tolerance → continuous lever from
        # edge_score kept missing the cache and hammering set-leverage.
        lever_int = max(1, int(round(lever)))
        cached_int = self._lever_cache.get(cache_key)
        if cached_int is not None and cached_int == lever_int:
            return True, None
        rest = await self._ensure_rest_client()
        try:
            await rest.account.set_leverage(
                inst_id=inst_id_okx,
                leverage=lever_int,
                mgn_mode=TdMode.ISOLATED,
                pos_side=pos_side,
            )
            self._lever_cache[cache_key] = lever_int
            self._log.info(
                f"iso_margin set leverage inst={inst_id_okx} "
                f"mgnMode=isolated posSide={ps_key} lever={lever_int}"
            )
            return True, None
        except Exception as exc:
            err_msg = str(exc)
            self._log.warning(
                f"iso_margin set_leverage failed inst={inst_id_okx} "
                f"posSide={ps_key} lever={lever}: {err_msg}"
            )
            return False, err_msg

    async def batch_ensure_leverage(
        self,
        items: list[tuple[str, float, PosSide | None]],
    ) -> BatchEnsureResult:
        """Pre-validate set-leverage for every item. Sequential (preserves
        OKX rate-limit headroom + makes error attribution clear). Caller
        (multi-leg strategy) inspects ``all_ok`` and aborts open-phase if
        False to avoid directional residual.
        """
        failed: list[tuple[str, str]] = []
        for inst_id, lever, pos_side in items:
            ok, err = await self.ensure_leverage(inst_id, lever, pos_side)
            if not ok:
                inst_id_okx = inst_id.split(".")[0]
                failed.append((inst_id_okx, err or "unknown"))
        return BatchEnsureResult(all_ok=(not failed), failed=failed)
