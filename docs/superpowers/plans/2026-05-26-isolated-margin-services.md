# Phase 1a + 1b — IsolatedMarginService + VolatilityFilter + OkxStrategyBase + FundingXS migrate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract FundingXS three-layer defense into two shared singleton services + a thin Strategy base class, then migrate FundingXS to consume them — proving the interface works on a strategy already in production. Phase 1c-1f follow as separate plans, one per opt-in strategy.

**Architecture:** Two side-effect-bearing singletons (`IsolatedMarginService` for OKX `set-leverage` REST calls + cache, `VolatilityFilter` for shared 1m bar buffer + outlier check) are constructed in `build_live_context` and DI'd into every strategy. `OkxStrategyBase` provides a `submit_isolated_order(...)` helper that strategies use instead of `submit_order(...)` for opt-in isolated margin. Existing pure helpers in `_isolated_helpers.py` stay as the underlying math.

**Tech Stack:** Python 3.11, NautilusTrader, OKX REST, pytest, asyncio (single-threaded, no thread locks).

**Spec:** [docs/superpowers/specs/2026-05-26-isolated-margin-services-design.md](../specs/2026-05-26-isolated-margin-services-design.md)

---

## File Structure

| File | Purpose | New/Modified |
|---|---|---|
| `src/okx_trade/risk/isolated_margin_service.py` | `IsolatedMarginService` + `BatchEnsureResult` | **New** |
| `src/okx_trade/risk/volatility_filter.py` | `VolatilityFilter` + `VolatilityFilterConfig` | **New** |
| `src/okx_trade/strategies/_okx_base.py` | `OkxStrategyBase` + `submit_isolated_order` + `vol_filter_allow` | **New** |
| `tests/unit/test_risk_isolated_margin_service.py` | Unit tests for service | **New** |
| `tests/unit/test_risk_volatility_filter.py` | Unit tests for filter | **New** |
| `tests/unit/test_strategies_okx_base.py` | Unit tests for base helper | **New** |
| `src/okx_trade/runtime/live_node.py` | Construct services in `build_live_context`, inject into strategies + monitor | Modified |
| `src/okx_trade/monitor/live.py` | Accept `iso_service` + `vol_filter` ctor args (for later diagnostics) | Modified |
| `configs/live.yaml` | Add `volatility_filter` top-level block | Modified |
| `src/okx_trade/strategies/funding_cross_section.py` | Replace inline `_set_leverage_cached` / `_get_account_pos_mode` / `_closes_1m_by_inst` / `_is_backtest_context` with service calls; inherit `OkxStrategyBase` | Modified (major) |
| `tests/unit/test_strategy_funding_xs_isolated.py` | Update tests that mocked the inline methods to mock the service instead | Modified |

---

## Task 1: `BatchEnsureResult` dataclass + `IsolatedMarginService` skeleton

**Files:**
- Create: `src/okx_trade/risk/isolated_margin_service.py`
- Create: `tests/unit/test_risk_isolated_margin_service.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_risk_isolated_margin_service.py`:

```python
"""Unit tests for IsolatedMarginService (2026-05-26 Phase 1)."""
from __future__ import annotations

import pytest

from okx_trade.risk.isolated_margin_service import (
    BatchEnsureResult,
    IsolatedMarginService,
)


class TestBatchEnsureResult:
    def test_all_ok_true_when_no_failures(self) -> None:
        r = BatchEnsureResult(all_ok=True, failed=[])
        assert r.all_ok is True
        assert r.failed == []

    def test_all_ok_false_when_failures_present(self) -> None:
        r = BatchEnsureResult(all_ok=False, failed=[("BTC-USDT-SWAP", "err")])
        assert r.all_ok is False
        assert len(r.failed) == 1
        assert r.failed[0] == ("BTC-USDT-SWAP", "err")

    def test_is_frozen(self) -> None:
        r = BatchEnsureResult(all_ok=True, failed=[])
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            r.all_ok = False  # type: ignore[misc]


class TestIsolatedMarginServiceInit:
    def test_constructs_with_empty_caches(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())
        assert svc._lever_cache == {}
        assert svc._pos_mode is None
        assert svc._rest is None


def _make_null_log():
    class _Null:
        def info(self, *_a, **_kw): pass
        def warning(self, *_a, **_kw): pass
        def error(self, *_a, **_kw): pass
        def debug(self, *_a, **_kw): pass
    return _Null()
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py -v`
Expected: ImportError because the module doesn't exist yet.

- [ ] **Step 3: Create the service skeleton**

Create `src/okx_trade/risk/isolated_margin_service.py`:

```python
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
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/risk/isolated_margin_service.py tests/unit/test_risk_isolated_margin_service.py
git commit -m "feat(risk): scaffold IsolatedMarginService + BatchEnsureResult"
```

---

## Task 2: `IsolatedMarginService.make_isolated_tags` + `is_backtest`

**Files:**
- Modify: `src/okx_trade/risk/isolated_margin_service.py`
- Modify: `tests/unit/test_risk_isolated_margin_service.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_risk_isolated_margin_service.py`:

```python
class TestMakeIsolatedTags:
    def test_returns_td_mode_isolated(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())
        assert svc.make_isolated_tags() == ["td_mode:isolated"]


class TestIsBacktest:
    def test_returns_true_when_api_key_empty(self) -> None:
        from okx_trade.config import OKXSettings
        # Default OKXSettings reads from env; force empty api_key
        settings = OKXSettings(api_key="", api_secret="", passphrase="")
        svc = IsolatedMarginService(settings, log=_make_null_log())
        assert svc.is_backtest() is True

    def test_returns_false_when_api_key_set(self) -> None:
        from okx_trade.config import OKXSettings
        settings = OKXSettings(
            api_key="real-key", api_secret="real-secret", passphrase="real-pass",
        )
        svc = IsolatedMarginService(settings, log=_make_null_log())
        assert svc.is_backtest() is False
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py -v`
Expected: AttributeError or method-missing failure on `make_isolated_tags` / `is_backtest`.

- [ ] **Step 3: Add methods**

Append to `IsolatedMarginService` class body in `src/okx_trade/risk/isolated_margin_service.py`:

```python
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
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/risk/isolated_margin_service.py tests/unit/test_risk_isolated_margin_service.py
git commit -m "feat(risk): IsolatedMarginService.make_isolated_tags + is_backtest"
```

---

## Task 3: `IsolatedMarginService.get_pos_mode` (cached, WARN on unknown)

**Files:**
- Modify: `src/okx_trade/risk/isolated_margin_service.py`
- Modify: `tests/unit/test_risk_isolated_margin_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
class TestGetPosMode:
    @pytest.mark.asyncio
    async def test_first_call_fetches_and_caches(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockTransport:
            calls = 0
            async def request(self, method, path, *, params=None, private=None, group=None):
                _MockTransport.calls += 1
                return [{"posMode": "long_short_mode"}]

        class _MockRest:
            transport = _MockTransport()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "long_short_mode"
        assert svc._pos_mode == "long_short_mode"
        # Second call hits cache, transport.request not called again
        result2 = await svc.get_pos_mode()
        assert result2 == "long_short_mode"
        assert _MockTransport.calls == 1

    @pytest.mark.asyncio
    async def test_net_mode_returned(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockTransport:
            async def request(self, *a, **kw):
                return [{"posMode": "net_mode"}]

        class _MockRest:
            transport = _MockTransport()

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "net_mode"

    @pytest.mark.asyncio
    async def test_unknown_pos_mode_warns_and_falls_back_to_net(self) -> None:
        from okx_trade.config import OKXSettings
        class _CapturingLog:
            def __init__(self): self.warnings = []
            def info(self, *_a, **_kw): pass
            def warning(self, msg, **_kw): self.warnings.append(msg)
            def error(self, *_a, **_kw): pass
            def debug(self, *_a, **_kw): pass

        log = _CapturingLog()
        svc = IsolatedMarginService(OKXSettings(), log=log)

        class _MockTransport:
            async def request(self, *a, **kw):
                return [{"posMode": "future_unexpected_mode"}]

        class _MockRest:
            transport = _MockTransport()

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "net_mode"  # safe fallback
        assert any("UNEXPECTED posMode" in w for w in log.warnings)

    @pytest.mark.asyncio
    async def test_rest_failure_falls_back_to_net(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockTransport:
            async def request(self, *a, **kw):
                raise RuntimeError("network down")

        class _MockRest:
            transport = _MockTransport()

        svc._rest = _MockRest()
        result = await svc.get_pos_mode()
        assert result == "net_mode"
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py::TestGetPosMode -v`
Expected: AttributeError because `get_pos_mode` doesn't exist.

- [ ] **Step 3: Add `get_pos_mode` and `_ensure_rest_client` private helper**

Append to `IsolatedMarginService` class body:

```python
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
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py::TestGetPosMode -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/risk/isolated_margin_service.py tests/unit/test_risk_isolated_margin_service.py
git commit -m "feat(risk): IsolatedMarginService.get_pos_mode with cache + WARN"
```

---

## Task 4: `IsolatedMarginService.ensure_leverage` (idempotent + suffix strip)

**Files:**
- Modify: `src/okx_trade/risk/isolated_margin_service.py`
- Modify: `tests/unit/test_risk_isolated_margin_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
class TestEnsureLeverage:
    @pytest.fixture
    def service_with_mock_rest(self):
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())
        captured = {"calls": []}

        class _MockAccount:
            async def set_leverage(self_inner, *, inst_id, leverage, mgn_mode, pos_side):
                captured["calls"].append({
                    "inst_id": inst_id, "leverage": leverage,
                    "mgn_mode": mgn_mode, "pos_side": pos_side,
                })

        class _MockRest:
            account = _MockAccount()

        svc._rest = _MockRest()
        return svc, captured

    @pytest.mark.asyncio
    async def test_first_call_invokes_rest(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide, TdMode
        svc, captured = service_with_mock_rest
        ok, err = await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        assert ok is True
        assert err is None
        assert len(captured["calls"]) == 1
        c = captured["calls"][0]
        assert c["inst_id"] == "BTC-USDT-SWAP"
        assert c["leverage"] == 5
        assert c["mgn_mode"] == TdMode.ISOLATED
        assert c["pos_side"] == PosSide.LONG

    @pytest.mark.asyncio
    async def test_strips_okx_venue_suffix(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("DOT-USDT-SWAP.OKX", 3.0, PosSide.SHORT)
        assert captured["calls"][0]["inst_id"] == "DOT-USDT-SWAP"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_rest(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        assert len(captured["calls"]) == 1

    @pytest.mark.asyncio
    async def test_different_pos_side_separate_cache(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.SHORT)
        assert len(captured["calls"]) == 2

    @pytest.mark.asyncio
    async def test_changed_lever_re_invokes(self, service_with_mock_rest) -> None:
        from okx_trade.enums import PosSide
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, PosSide.LONG)
        await svc.ensure_leverage("BTC-USDT-SWAP", 8.0, PosSide.LONG)
        assert len(captured["calls"]) == 2
        assert captured["calls"][0]["leverage"] == 5
        assert captured["calls"][1]["leverage"] == 8

    @pytest.mark.asyncio
    async def test_none_pos_side_uses_net_cache_key(self, service_with_mock_rest) -> None:
        svc, captured = service_with_mock_rest
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, None)
        await svc.ensure_leverage("BTC-USDT-SWAP", 5.0, None)
        assert len(captured["calls"]) == 1
        # PosSide passed to account.set_leverage is None (account.py auto-fills NET)
        assert captured["calls"][0]["pos_side"] is None

    @pytest.mark.asyncio
    async def test_rest_failure_returns_false_with_err(self) -> None:
        from okx_trade.config import OKXSettings
        from okx_trade.enums import PosSide
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockAccount:
            async def set_leverage(self_inner, **kw):
                raise RuntimeError("OKX 51001: instId mismatch")

        class _MockRest:
            account = _MockAccount()

        svc._rest = _MockRest()
        ok, err = await svc.ensure_leverage("BAD-USDT", 5.0, PosSide.LONG)
        assert ok is False
        assert err is not None
        assert "51001" in err
        # Failed call should NOT populate cache
        assert ("BAD-USDT", "long") not in svc._lever_cache
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py::TestEnsureLeverage -v`
Expected: AttributeError because `ensure_leverage` missing.

- [ ] **Step 3: Add `ensure_leverage` method**

Append to `IsolatedMarginService` class body:

```python
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
        cached = self._lever_cache.get(cache_key)
        if cached is not None and abs(cached - lever) < 0.01:
            return True, None
        rest = await self._ensure_rest_client()
        try:
            await rest.account.set_leverage(
                inst_id=inst_id_okx,
                leverage=int(round(lever)),
                mgn_mode=TdMode.ISOLATED,
                pos_side=pos_side,
            )
            self._lever_cache[cache_key] = lever
            self._log.info(
                f"iso_margin set leverage inst={inst_id_okx} "
                f"mgnMode=isolated posSide={ps_key} lever={int(round(lever))}"
            )
            return True, None
        except Exception as exc:
            err_msg = str(exc)
            self._log.warning(
                f"iso_margin set_leverage failed inst={inst_id_okx} "
                f"posSide={ps_key} lever={lever}: {err_msg}"
            )
            return False, err_msg
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py::TestEnsureLeverage -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/risk/isolated_margin_service.py tests/unit/test_risk_isolated_margin_service.py
git commit -m "feat(risk): IsolatedMarginService.ensure_leverage idempotent w/ suffix strip"
```

---

## Task 5: `IsolatedMarginService.batch_ensure_leverage`

**Files:**
- Modify: `src/okx_trade/risk/isolated_margin_service.py`
- Modify: `tests/unit/test_risk_isolated_margin_service.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
class TestBatchEnsureLeverage:
    @pytest.mark.asyncio
    async def test_all_succeed_all_ok_true(self) -> None:
        from okx_trade.config import OKXSettings
        from okx_trade.enums import PosSide
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockAccount:
            async def set_leverage(self_inner, **kw): pass

        class _MockRest:
            account = _MockAccount()

        svc._rest = _MockRest()
        items = [
            ("BTC-USDT-SWAP", 5.0, PosSide.LONG),
            ("ETH-USDT-SWAP", 5.0, PosSide.SHORT),
        ]
        result = await svc.batch_ensure_leverage(items)
        assert result.all_ok is True
        assert result.failed == []

    @pytest.mark.asyncio
    async def test_partial_failure_collects_failed_list(self) -> None:
        from okx_trade.config import OKXSettings
        from okx_trade.enums import PosSide
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())

        class _MockAccount:
            async def set_leverage(self_inner, *, inst_id, **kw):
                if inst_id == "ETH-USDT-SWAP":
                    raise RuntimeError("OKX 51001: eth broken")

        class _MockRest:
            account = _MockAccount()

        svc._rest = _MockRest()
        items = [
            ("BTC-USDT-SWAP", 5.0, PosSide.LONG),
            ("ETH-USDT-SWAP", 5.0, PosSide.SHORT),
            ("SOL-USDT-SWAP", 5.0, PosSide.LONG),
        ]
        result = await svc.batch_ensure_leverage(items)
        assert result.all_ok is False
        assert len(result.failed) == 1
        assert result.failed[0][0] == "ETH-USDT-SWAP"
        assert "51001" in result.failed[0][1]

    @pytest.mark.asyncio
    async def test_empty_items_all_ok_trivially(self) -> None:
        from okx_trade.config import OKXSettings
        svc = IsolatedMarginService(OKXSettings(), log=_make_null_log())
        result = await svc.batch_ensure_leverage([])
        assert result.all_ok is True
        assert result.failed == []
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py::TestBatchEnsureLeverage -v`
Expected: AttributeError on `batch_ensure_leverage`.

- [ ] **Step 3: Add `batch_ensure_leverage`**

Append to `IsolatedMarginService` class body:

```python
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
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_isolated_margin_service.py::TestBatchEnsureLeverage -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/risk/isolated_margin_service.py tests/unit/test_risk_isolated_margin_service.py
git commit -m "feat(risk): IsolatedMarginService.batch_ensure_leverage"
```

---

## Task 6: `VolatilityFilterConfig` + `VolatilityFilter` skeleton + `feed_bar`

**Files:**
- Create: `src/okx_trade/risk/volatility_filter.py`
- Create: `tests/unit/test_risk_volatility_filter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_risk_volatility_filter.py`:

```python
"""Unit tests for VolatilityFilter (2026-05-26 Phase 1)."""
from __future__ import annotations

import pytest

from okx_trade.risk.volatility_filter import (
    VolatilityFilter,
    VolatilityFilterConfig,
)


def _make_null_log():
    class _Null:
        def info(self, *_a, **_kw): pass
        def warning(self, *_a, **_kw): pass
        def error(self, *_a, **_kw): pass
        def debug(self, *_a, **_kw): pass
    return _Null()


class TestVolatilityFilterConfigDefaults:
    def test_defaults(self) -> None:
        c = VolatilityFilterConfig()
        assert c.enable is False
        assert c.window_min == 60
        assert c.baseline_min == 1440
        assert c.warmup_min == 1440
        assert c.ratio_threshold == 3.0
        assert c.buffer_max == 2000

    def test_is_frozen(self) -> None:
        c = VolatilityFilterConfig()
        with pytest.raises(Exception):
            c.enable = True  # type: ignore[misc]


class TestFeedBar:
    def test_lazy_creates_deque(self) -> None:
        f = VolatilityFilter(VolatilityFilterConfig(), log=_make_null_log())
        assert f.buffer_size("BTC-USDT-SWAP") == 0
        f.feed_bar("BTC-USDT-SWAP", 100.0)
        assert f.buffer_size("BTC-USDT-SWAP") == 1

    def test_appends_to_existing(self) -> None:
        f = VolatilityFilter(VolatilityFilterConfig(), log=_make_null_log())
        f.feed_bar("BTC-USDT-SWAP", 100.0)
        f.feed_bar("BTC-USDT-SWAP", 100.1)
        f.feed_bar("BTC-USDT-SWAP", 100.2)
        assert f.buffer_size("BTC-USDT-SWAP") == 3

    def test_per_inst_isolation(self) -> None:
        f = VolatilityFilter(VolatilityFilterConfig(), log=_make_null_log())
        f.feed_bar("BTC-USDT-SWAP", 100.0)
        f.feed_bar("ETH-USDT-SWAP", 2000.0)
        assert f.buffer_size("BTC-USDT-SWAP") == 1
        assert f.buffer_size("ETH-USDT-SWAP") == 1

    def test_buffer_max_truncates(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(buffer_max=5), log=_make_null_log(),
        )
        for px in [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]:
            f.feed_bar("BTC-USDT-SWAP", px)
        assert f.buffer_size("BTC-USDT-SWAP") == 5
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_volatility_filter.py -v`
Expected: ImportError.

- [ ] **Step 3: Create module + skeleton**

Create `src/okx_trade/risk/volatility_filter.py`:

```python
"""Shared 1-minute bar buffer + outlier guard singleton (2026-05-26 Phase 1).

Strategies subscribe 1m bars via NT and call ``feed_bar`` on each tick.
NT DataEngine dedups bar subscriptions, so multiple strategies watching
the same inst share a single feed naturally — this service just maintains
the rolling buffer + delegates outlier math to ``outlier_check``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class VolatilityFilterConfig:
    """Global config; one instance shared across all strategies.

    Loaded from ``live.yaml.volatility_filter`` block.
    """
    enable: bool = False
    window_min: int = 60
    baseline_min: int = 1440
    warmup_min: int = 1440
    ratio_threshold: float = 3.0
    buffer_max: int = 2000


class VolatilityFilter:
    """1m bar buffer + outlier check. Singleton owned by build_live_context."""

    def __init__(self, config: VolatilityFilterConfig, log) -> None:
        self._cfg = config
        self._log = log
        self._closes: dict[str, deque[float]] = {}

    def feed_bar(self, inst_id: str, close: float) -> None:
        """Append a 1m close to the inst's buffer. Lazy-creates the deque
        with ``maxlen=config.buffer_max`` on first call for the inst.
        ``inst_id`` is OKX format (no ``.OKX`` suffix); callers strip.
        """
        buf = self._closes.get(inst_id)
        if buf is None:
            buf = deque(maxlen=self._cfg.buffer_max)
            self._closes[inst_id] = buf
        buf.append(float(close))

    def buffer_size(self, inst_id: str) -> int:
        """Diagnostic: number of bars accumulated. 0 if not yet fed."""
        return len(self._closes.get(inst_id, ()))
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_volatility_filter.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/risk/volatility_filter.py tests/unit/test_risk_volatility_filter.py
git commit -m "feat(risk): VolatilityFilter scaffold + feed_bar + buffer_size"
```

---

## Task 7: `VolatilityFilter.allow` (wraps outlier_check pure helper)

**Files:**
- Modify: `src/okx_trade/risk/volatility_filter.py`
- Modify: `tests/unit/test_risk_volatility_filter.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
import numpy as np


class TestAllow:
    def test_disabled_always_allows(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(enable=False), log=_make_null_log(),
        )
        ok, reason = f.allow("BTC-USDT-SWAP")
        assert ok is True
        assert reason == "disabled"

    def test_warmup_when_insufficient_data(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(enable=True, warmup_min=1440),
            log=_make_null_log(),
        )
        for px in [100.0, 100.1, 99.9]:
            f.feed_bar("BTC-USDT-SWAP", px)
        ok, reason = f.allow("BTC-USDT-SWAP")
        assert ok is True
        assert reason == "warmup"

    def test_calm_market_allows(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(enable=True), log=_make_null_log(),
        )
        rng = np.random.default_rng(seed=42)
        rets = rng.normal(0.0, 0.001, 1500)
        prices = 100.0 * np.exp(np.cumsum(rets))
        for px in prices:
            f.feed_bar("BTC-USDT-SWAP", float(px))
        ok, reason = f.allow("BTC-USDT-SWAP")
        assert ok is True
        assert reason == "ok"

    def test_wicky_market_rejects(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(enable=True), log=_make_null_log(),
        )
        rng = np.random.default_rng(seed=7)
        calm = rng.normal(0.0, 0.001, 1440)
        wick = rng.normal(0.0, 0.02, 60)
        prices = 100.0 * np.exp(np.cumsum(np.concatenate([calm, wick])))
        for px in prices:
            f.feed_bar("BTC-USDT-SWAP", float(px))
        ok, reason = f.allow("BTC-USDT-SWAP")
        assert ok is False
        assert "vol_ratio" in reason

    def test_flat_baseline_no_baseline(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(enable=True), log=_make_null_log(),
        )
        for _ in range(1500):
            f.feed_bar("BTC-USDT-SWAP", 100.0)
        ok, reason = f.allow("BTC-USDT-SWAP")
        assert ok is True
        assert reason == "no_baseline"

    def test_unknown_inst_allows_as_warmup(self) -> None:
        f = VolatilityFilter(
            VolatilityFilterConfig(enable=True), log=_make_null_log(),
        )
        ok, reason = f.allow("NEVER-FED-SWAP")
        assert ok is True
        assert reason == "warmup"
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_volatility_filter.py::TestAllow -v`
Expected: AttributeError on `allow`.

- [ ] **Step 3: Add `allow` method**

Append to `VolatilityFilter` class body:

```python
    def allow(self, inst_id: str) -> tuple[bool, str]:
        """Decide if a new leg on ``inst_id`` should be allowed by the
        outlier guard.

        Returns:
            ``(True, "disabled")`` if config.enable is False.
            ``(True, "warmup")`` if buffer has < warmup_min entries.
            ``(True, "no_baseline")`` if baseline std == 0 (flat history).
            ``(True, "ok")`` if recent vol within ratio_threshold of baseline.
            ``(False, "vol_ratio=R>T")`` otherwise.
        """
        if not self._cfg.enable:
            return True, "disabled"
        from ..strategies._isolated_helpers import outlier_check
        closes = self._closes.get(inst_id)
        if closes is None:
            return True, "warmup"
        return outlier_check(
            closes=list(closes),
            window=self._cfg.window_min,
            baseline=self._cfg.baseline_min,
            warmup=self._cfg.warmup_min,
            ratio_threshold=self._cfg.ratio_threshold,
        )
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_risk_volatility_filter.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/risk/volatility_filter.py tests/unit/test_risk_volatility_filter.py
git commit -m "feat(risk): VolatilityFilter.allow delegates to outlier_check"
```

---

## Task 8: `OkxStrategyBase` skeleton + `vol_filter_allow`

**Files:**
- Create: `src/okx_trade/strategies/_okx_base.py`
- Create: `tests/unit/test_strategies_okx_base.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_strategies_okx_base.py`:

```python
"""Unit tests for OkxStrategyBase (2026-05-26 Phase 1)."""
from __future__ import annotations

import pytest


class TestVolFilterAllow:
    def test_returns_no_filter_when_filter_is_none(self) -> None:
        from okx_trade.strategies._okx_base import OkxStrategyBase

        class _Stub:
            _vol_filter = None

        result = OkxStrategyBase.vol_filter_allow(_Stub(), "BTC-USDT-SWAP")  # type: ignore[arg-type]
        assert result == (True, "no_filter")

    def test_delegates_to_filter_when_present(self) -> None:
        from okx_trade.strategies._okx_base import OkxStrategyBase

        class _FakeFilter:
            def allow(self, inst_id):
                return (False, "vol_ratio=4.5>3.0")

        class _Stub:
            _vol_filter = _FakeFilter()

        result = OkxStrategyBase.vol_filter_allow(_Stub(), "BTC-USDT-SWAP")  # type: ignore[arg-type]
        assert result == (False, "vol_ratio=4.5>3.0")
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_strategies_okx_base.py -v`
Expected: ImportError.

- [ ] **Step 3: Create module + skeleton**

Create `src/okx_trade/strategies/_okx_base.py`:

```python
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
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_strategies_okx_base.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/_okx_base.py tests/unit/test_strategies_okx_base.py
git commit -m "feat(strategies): OkxStrategyBase skeleton + vol_filter_allow"
```

---

## Task 9: `OkxStrategyBase.submit_isolated_order` — the 7-branch helper

**Files:**
- Modify: `src/okx_trade/strategies/_okx_base.py`
- Modify: `tests/unit/test_strategies_okx_base.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
class TestSubmitIsolatedOrder:
    """Test all 7 branches of submit_isolated_order:
       1. enable=False                  → cross fallback
       2. _iso_service is None          → cross fallback
       3. is_backtest()                 → cross fallback
       4. net_mode + caller pos_side=X  → forced None
       5. long_short_mode + no pos_side → derived from order.side
       6. ensure_leverage fails         → return False, no submit
       7. happy path                    → tag attached + submit + True
    """

    def _make_stub_strategy(
        self,
        *,
        enable_isolated_margin=True,
        iso_service=None,
        order_side="BUY",
    ):
        from okx_trade.strategies._okx_base import OkxStrategyBase

        class _Config:
            pass

        class _MockOrderSide:
            BUY = "BUY"
            SELL = "SELL"

        class _Order:
            def __init__(self):
                self.side = order_side
                self.tags = None

                class _InstId:
                    value = "DOT-USDT-SWAP.OKX"
                self.instrument_id = _InstId()

        class _Log:
            def __init__(self): self.warnings = []
            def warning(self, msg): self.warnings.append(msg)
            def info(self, msg): pass

        config = _Config()
        config.enable_isolated_margin = enable_isolated_margin

        m = type("Stub", (), {})()
        m.config = config
        m._iso_service = iso_service
        m._vol_filter = None
        m.log = _Log()
        m._submitted = []
        m.submit_order = lambda order: m._submitted.append(order)
        m.submit_isolated_order = OkxStrategyBase.submit_isolated_order.__get__(m)
        return m, _Order()

    @pytest.mark.asyncio
    async def test_branch1_disabled_falls_back_to_cross(self) -> None:
        from okx_trade.enums import PosSide
        m, order = self._make_stub_strategy(enable_isolated_margin=False)
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert len(m._submitted) == 1
        assert order.tags is None  # no isolated tag attached

    @pytest.mark.asyncio
    async def test_branch2_no_service_falls_back(self) -> None:
        from okx_trade.enums import PosSide
        m, order = self._make_stub_strategy(iso_service=None)
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert len(m._submitted) == 1
        assert order.tags is None

    @pytest.mark.asyncio
    async def test_branch3_backtest_falls_back(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            def is_backtest(self): return True

        m, order = self._make_stub_strategy(iso_service=_FakeService())
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert len(m._submitted) == 1
        assert order.tags is None

    @pytest.mark.asyncio
    async def test_branch4_net_mode_forces_pos_side_none(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "net_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                self.calls.append((inst_id, lever, pos_side))
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        svc = _FakeService()
        m, order = self._make_stub_strategy(iso_service=svc)
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert svc.calls == [("DOT-USDT-SWAP", 5, None)]  # forced None
        assert order.tags == ["td_mode:isolated"]
        assert len(m._submitted) == 1

    @pytest.mark.asyncio
    async def test_branch5_long_short_mode_derives_from_buy(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                self.calls.append((inst_id, lever, pos_side))
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        svc = _FakeService()
        m, order = self._make_stub_strategy(iso_service=svc, order_side="BUY")
        ok = await m.submit_isolated_order(order, lever=5)  # no pos_side
        assert ok is True
        assert svc.calls[0][2] == PosSide.LONG  # derived from BUY

    @pytest.mark.asyncio
    async def test_branch5b_long_short_mode_derives_from_sell(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                self.calls.append((inst_id, lever, pos_side))
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        svc = _FakeService()
        m, order = self._make_stub_strategy(iso_service=svc, order_side="SELL")
        ok = await m.submit_isolated_order(order, lever=5)
        assert svc.calls[0][2] == PosSide.SHORT  # derived from SELL

    @pytest.mark.asyncio
    async def test_branch6_ensure_leverage_failure_no_submit(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                return False, "51001 broken"
            def make_isolated_tags(self): return ["td_mode:isolated"]

        m, order = self._make_stub_strategy(iso_service=_FakeService())
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is False
        assert len(m._submitted) == 0  # did NOT submit
        assert order.tags is None
        assert any("skip leg" in w for w in m.log.warnings)

    @pytest.mark.asyncio
    async def test_branch7_happy_path_appends_to_existing_tags(self) -> None:
        from okx_trade.enums import PosSide

        class _FakeService:
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def ensure_leverage(self, inst_id, lever, pos_side):
                return True, None
            def make_isolated_tags(self): return ["td_mode:isolated"]

        m, order = self._make_stub_strategy(iso_service=_FakeService())
        order.tags = ["existing:tag"]
        ok = await m.submit_isolated_order(order, lever=5, pos_side=PosSide.LONG)
        assert ok is True
        assert order.tags == ["existing:tag", "td_mode:isolated"]
```

- [ ] **Step 2: Run tests, verify FAIL**

Run: `.venv/bin/python -m pytest tests/unit/test_strategies_okx_base.py::TestSubmitIsolatedOrder -v`
Expected: AttributeError on `submit_isolated_order`.

- [ ] **Step 3: Implement `submit_isolated_order`**

Append to `OkxStrategyBase` class body:

```python
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
                # Lazy import — NT order side enum
                try:
                    from nautilus_trader.model.enums import OrderSide
                    is_buy = order.side == OrderSide.BUY
                except ImportError:
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
```

- [ ] **Step 4: Run tests, verify PASS**

Run: `.venv/bin/python -m pytest tests/unit/test_strategies_okx_base.py -v`
Expected: 10 passed (2 + 8).

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/_okx_base.py tests/unit/test_strategies_okx_base.py
git commit -m "feat(strategies): OkxStrategyBase.submit_isolated_order (7-branch helper)"
```

---

## Task 10: Wire services into `build_live_context` + `LiveMonitor` ctor

**Files:**
- Modify: `src/okx_trade/runtime/live_node.py`
- Modify: `src/okx_trade/monitor/live.py`
- Modify: `tests/unit/test_runtime_live_node.py`

- [ ] **Step 1: Read existing `build_live_context` to find injection points**

Run: `grep -n "account_drawdown_tracker\|build_live_context\|strategies\\[" src/okx_trade/runtime/live_node.py | head -20`

Look at how `account_drawdown_tracker` is constructed and injected — the new services follow the same pattern.

- [ ] **Step 2: Add LiveMonitor ctor args**

Read `src/okx_trade/monitor/live.py` ~lines 60-145 to find the existing ctor (signature lists `equity_provider`, `allocator`, `account_drawdown_tracker`, etc.). Add two new optional kwargs:

```python
        iso_service: Any = None,
        vol_filter: Any = None,
```

And store them on `self`:

```python
        self._iso_service = iso_service
        self._vol_filter = vol_filter
```

(These aren't used by LiveMonitor itself yet — Phase 1c+ may add diagnostic endpoints. For now they're held to keep references alive and make future wiring trivial.)

- [ ] **Step 3: Construct services in `build_live_context`**

In `src/okx_trade/runtime/live_node.py`, find where `account_drawdown_tracker` is constructed (around the LiveMonitor build). Add immediately above the LiveMonitor construction:

```python
    # 2026-05-26 Phase 1: shared services injected into every strategy
    from ..risk.isolated_margin_service import IsolatedMarginService
    from ..risk.volatility_filter import VolatilityFilter, VolatilityFilterConfig
    from ..utils.logging import get_logger

    iso_service = IsolatedMarginService(
        rest_settings=OKXSettings(),
        log=get_logger("iso_margin"),
    )
    vol_cfg_dict = live_cfg.get("volatility_filter") or {}
    vol_filter = VolatilityFilter(
        config=VolatilityFilterConfig(**vol_cfg_dict),
        log=get_logger("vol_filter"),
    )

    for strategy in strategies.values():
        if hasattr(strategy, "_iso_service"):
            strategy._iso_service = iso_service
        if hasattr(strategy, "_vol_filter"):
            strategy._vol_filter = vol_filter
```

Then pass to LiveMonitor:

```python
    monitor = LiveMonitor(
        ...,  # existing args
        iso_service=iso_service,
        vol_filter=vol_filter,
    )
```

The exact spot depends on the current `LiveMonitor(...)` call site — locate via `grep -n "LiveMonitor(" src/okx_trade/runtime/live_node.py`.

- [ ] **Step 4: Add tests for service injection**

Open `tests/unit/test_runtime_live_node.py`. Find the existing test for `build_live_context` (e.g., `test_builds_strategies` or similar). Add:

```python
def test_build_live_context_injects_iso_service_and_vol_filter(
    tmp_path: Path, fake_live_yaml_path: Path,  # use existing fixtures
) -> None:
    """Phase 1: build_live_context constructs the two singletons and
    assigns them to every strategy that has the DI slots."""
    from okx_trade.runtime.live_node import build_live_context
    from okx_trade.risk.isolated_margin_service import IsolatedMarginService
    from okx_trade.risk.volatility_filter import VolatilityFilter

    live_cfg = _minimal_live_cfg()  # whatever the existing test uses
    ctx = build_live_context(live_cfg, build_node=False)

    # All strategies got both services
    for name, strategy in ctx.strategies.items():
        assert isinstance(getattr(strategy, "_iso_service", None), IsolatedMarginService), (
            f"strategy {name} missing _iso_service"
        )
        assert isinstance(getattr(strategy, "_vol_filter", None), VolatilityFilter), (
            f"strategy {name} missing _vol_filter"
        )

    # LiveMonitor also holds references
    assert isinstance(ctx.monitor._iso_service, IsolatedMarginService)
    assert isinstance(ctx.monitor._vol_filter, VolatilityFilter)
```

If the existing test file uses different fixture names, adapt. Read the file first via:

Run: `grep -n "^def test_\|@pytest.fixture\|build_live_context\|_minimal_live_cfg" tests/unit/test_runtime_live_node.py | head -20`

- [ ] **Step 5: Run the new test**

Run: `.venv/bin/python -m pytest tests/unit/test_runtime_live_node.py::test_build_live_context_injects_iso_service_and_vol_filter -v`
Expected: PASS.

- [ ] **Step 6: Run the full runtime suite**

Run: `.venv/bin/python -m pytest tests/unit/test_runtime_live_node.py -v`
Expected: all passed (existing tests + new one).

- [ ] **Step 7: Commit**

```bash
git add src/okx_trade/runtime/live_node.py src/okx_trade/monitor/live.py tests/unit/test_runtime_live_node.py
git commit -m "feat(runtime): construct + DI IsolatedMarginService + VolatilityFilter"
```

---

## Task 11: Add `volatility_filter` block to `configs/live.yaml`

**Files:**
- Modify: `configs/live.yaml`

- [ ] **Step 1: Locate where to add the block**

Read the top of `configs/live.yaml` to find a sensible location — alongside other top-level blocks like `account`, `execution`, etc. (NOT under `strategies:`).

- [ ] **Step 2: Add the block**

After the `account:` or `execution:` block (or wherever fits the existing layout):

```yaml
# 2026-05-26 Phase 1: global volatility filter (shared across all opt-in strategies)
volatility_filter:
  enable: true
  window_min: 60
  baseline_min: 1440
  warmup_min: 1440
  ratio_threshold: 3.0
  buffer_max: 2000
```

- [ ] **Step 3: Verify config loads**

Run: `.venv/bin/python -c "from okx_trade.runtime.live_node import build_live_context; import yaml; cfg = yaml.safe_load(open('configs/live.yaml')); ctx = build_live_context(cfg, build_node=False); print('OK', ctx.monitor._vol_filter._cfg)"`
Expected: prints `OK VolatilityFilterConfig(enable=True, window_min=60, ...)`.

- [ ] **Step 4: Commit**

```bash
git add configs/live.yaml
git commit -m "feat(config): add volatility_filter block to live.yaml"
```

---

## Task 12: Run full unit suite gate after infrastructure tasks

**Files:** none (gate task).

- [ ] **Step 1: Run full suite**

Run: `.venv/bin/python -m pytest tests/unit/ -q`
Expected: 949 prior + ~32 new = ~981 tests pass; 0 failures.

If any failure, fix in-place before proceeding to Task 13.

---

## Task 13: Migrate FundingXSStrategy — switch base class + use services for posMode + leverage

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py`

This is the largest task — replace inline `_set_lever_cache` / `_get_account_pos_mode` / `_set_leverage_cached` / `_is_backtest_context` with service calls. Keep all existing behavior; just relocate the state.

- [ ] **Step 1: Read existing FundingXSStrategy to find all the methods to replace**

Run: `grep -n "_set_lever_cache\|_get_account_pos_mode\|_set_leverage_cached\|_is_backtest_context\|_account_pos_mode" src/okx_trade/strategies/funding_cross_section.py`

Confirm the list: `__init__` field declarations + 3 methods + their callsites in `_execute_diff` / `_open_leg`.

- [ ] **Step 2: Change base class**

Locate the class declaration. Currently:

```python
        class FundingXSStrategy(Strategy):  # type: ignore[misc]
```

Change to:

```python
        from ._okx_base import OkxStrategyBase
        class FundingXSStrategy(OkxStrategyBase):  # type: ignore[misc]
```

Place the import at the top of the file with other strategies imports.

- [ ] **Step 3: Delete now-redundant `__init__` state**

In `__init__`, remove these lines (they're now on the service / base):

```python
            self._set_lever_cache: dict[tuple[str, str], float] = {}
            self._account_pos_mode: str | None = None
```

Leave the rest (including `self._closes_1m_by_inst` for now — Task 14 handles it).

- [ ] **Step 4: Delete `_get_account_pos_mode` and `_set_leverage_cached` methods**

Find each method body in the class and delete them entirely (they're replaced by `self._iso_service.get_pos_mode()` and `self._iso_service.ensure_leverage(...)`).

- [ ] **Step 5: Delete `_is_backtest_context` method**

Replaced by `self._iso_service.is_backtest()` when service is available; the base helper handles the None-service backtest case.

- [ ] **Step 6: Update `_execute_diff` to use `batch_ensure_leverage`**

Find `_execute_diff`. The Phase-3 pre-validate block currently looks like:

```python
            if use_isolated:
                pos_mode = await self._get_account_pos_mode()
                set_lever_results: list[tuple[str, "_LegTarget", bool]] = []
                for inst_value, leg in to_open:
                    pos_side_arg = leg.pos_side if pos_mode == "long_short_mode" else None
                    ok = await self._set_leverage_cached(inst_value, leg.lever, pos_side_arg)
                    set_lever_results.append((inst_value, leg, ok))
                if not all(r[2] for r in set_lever_results):
                    failed = [r[0] for r in set_lever_results if not r[2]]
                    self.log.warning(
                        f"funding_xs ABORT rebalance: set-leverage failed for "
                        f"{failed}; skipping all opens this round to avoid "
                        f"directional residual (next rebalance retries)"
                    )
                    return
                to_open_validated = [(iv, leg) for iv, leg, _ in set_lever_results]
            else:
                to_open_validated = to_open
```

Replace with:

```python
            use_isolated = (
                self.config.margin_mode == "isolated"
                and self.config.enable_dynamic_lever
                and self._iso_service is not None
                and not self._iso_service.is_backtest()
            )
            if use_isolated:
                pos_mode = await self._iso_service.get_pos_mode()
                items = [
                    (
                        inst_value,
                        leg.lever,
                        leg.pos_side if pos_mode == "long_short_mode" else None,
                    )
                    for inst_value, leg in to_open
                ]
                result = await self._iso_service.batch_ensure_leverage(items)
                if not result.all_ok:
                    self.log.warning(
                        f"funding_xs ABORT rebalance: set-leverage failed for "
                        f"{[f[0] for f in result.failed]}; skipping all opens "
                        f"this round to avoid directional residual"
                    )
                    return
            to_open_validated = to_open  # all passed pre-validation (or use_isolated False)
```

- [ ] **Step 7: Update `_open_leg` to either use base helper or skip set-leverage (already cached by batch)**

`_open_leg` currently calls `await self._set_leverage_cached(...)`. Since `_execute_diff` Phase 3 already populated the service cache, the per-leg call would now hit cache (no-op REST). Keep simplicity by replacing with `self._iso_service.ensure_leverage(...)` which is also cache-aware:

Find the section in `_open_leg`:

```python
            if use_isolated:
                pos_mode = await self._get_account_pos_mode()
                pos_side_arg = leg.pos_side if pos_mode == "long_short_mode" else None
                ok = await self._set_leverage_cached(inst_value, leg.lever, pos_side_arg)
                if not ok:
                    ...
                    return
```

Replace with:

```python
            if use_isolated:
                pos_mode = await self._iso_service.get_pos_mode()
                pos_side_arg = leg.pos_side if pos_mode == "long_short_mode" else None
                ok, err = await self._iso_service.ensure_leverage(
                    inst_value, leg.lever, pos_side_arg,
                )
                if not ok:
                    self.log.warning(
                        f"funding_xs skip leg inst={inst_value} "
                        f"(set_leverage failed: {err})"
                    )
                    return
```

Note: `use_isolated` here mirrors the check used in `_execute_diff` — define once, share. Either inline the check or hoist to a small instance method.

- [ ] **Step 8: Run FundingXS tests**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py -v`
Expected: tests that mocked `_set_leverage_cached` or `_get_account_pos_mode` directly will FAIL because those methods no longer exist on the strategy.

This is expected; Task 14 fixes the tests.

- [ ] **Step 9: Do NOT commit yet** — proceed to Task 14 to fix the broken tests.

---

## Task 14: Update FundingXS tests to mock the service instead of strategy methods

**Files:**
- Modify: `tests/unit/test_strategy_funding_xs_isolated.py`

- [ ] **Step 1: Identify which tests mocked the deleted strategy methods**

Run: `grep -n "_set_leverage_cached\|_get_account_pos_mode\|_account_pos_mode\|_is_backtest_context\|_set_lever_cache" tests/unit/test_strategy_funding_xs_isolated.py`

The two test classes affected from Plan 7:
- `TestSetLeverageCache` — tested `FundingXSStrategy._set_leverage_cached` directly via `__get__` binding.
- `TestPartialRebalanceAbort` — tested `_execute_diff` with a mocked `_set_leverage_cached`.
- `TestUnknownPosModeWarn` — tested `_get_account_pos_mode` directly.
- `TestBacktestFallback` — tested `_is_backtest_context`.

- [ ] **Step 2: Delete `TestSetLeverageCache` and `TestUnknownPosModeWarn`**

The behaviors they tested are now in `test_risk_isolated_margin_service.py` (Tasks 3-5). Remove them from `test_strategy_funding_xs_isolated.py` to avoid duplication.

- [ ] **Step 3: Delete `TestBacktestFallback`**

Behavior moved to `IsolatedMarginService.is_backtest` (Task 2). Remove.

- [ ] **Step 4: Rewrite `TestPartialRebalanceAbort` to mock the service**

Replace the existing class with:

```python
class TestPartialRebalanceAbort:
    """Verify _execute_diff aborts open-phase if service.batch_ensure_leverage
    returns all_ok=False."""

    @pytest.mark.asyncio
    async def test_partial_failure_aborts_all_opens(self) -> None:
        from okx_trade.strategies.funding_cross_section import (
            FundingXSStrategy, _LegTarget,
        )
        from okx_trade.enums import PosSide
        from okx_trade.risk.isolated_margin_service import BatchEnsureResult

        class _MockLog:
            def __init__(self): self.warnings = []
            def warning(self, msg): self.warnings.append(msg)
            def info(self, msg): pass

        class _MockConfig:
            margin_mode = "isolated"
            enable_dynamic_lever = True

        class _MockService:
            calls: list = []
            def is_backtest(self): return False
            async def get_pos_mode(self): return "long_short_mode"
            async def batch_ensure_leverage(self, items):
                self.calls.append(items)
                # First leg succeeds, second fails — emulate partial failure
                return BatchEnsureResult(
                    all_ok=False,
                    failed=[("B-USDT-SWAP", "51001 broken")],
                )

        async def fake_open_leg(inst_value, leg):
            pytest.fail(f"_open_leg should not be called on abort; got {inst_value}")

        m = type("Mock", (), {})()
        m.config = _MockConfig()
        m.log = _MockLog()
        m._positions = {}
        m._iso_service = _MockService()
        m._open_leg = fake_open_leg
        m._close_leg = lambda *_a, **_kw: None
        m._execute_diff = FundingXSStrategy._execute_diff.__get__(m)

        target = {
            "A-USDT-SWAP": _LegTarget(
                direction="long", contracts=1.0, lever=5.0, edge_score=1.0,
                pos_side=PosSide.LONG,
            ),
            "B-USDT-SWAP": _LegTarget(
                direction="short", contracts=1.0, lever=5.0, edge_score=1.0,
                pos_side=PosSide.SHORT,
            ),
        }
        await m._execute_diff(target)

        assert any("ABORT" in w for w in m.log.warnings)
        # batch_ensure_leverage was called once with both items
        assert len(m._iso_service.calls) == 1
        assert len(m._iso_service.calls[0]) == 2
```

- [ ] **Step 5: Run all FundingXS tests**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py -v`
Expected: all passed (some classes deleted, one rewritten, others unchanged).

- [ ] **Step 6: Run broader FundingXS-related suite to catch any other regression**

Run: `.venv/bin/python -m pytest tests/unit/ -k "funding_cross or funding_xs_isolated" -v`
Expected: all passed.

- [ ] **Step 7: Commit the migration (both Task 13 + 14)**

```bash
git add src/okx_trade/strategies/funding_cross_section.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "refactor(funding_xs): use IsolatedMarginService instead of inline state"
```

---

## Task 15: Migrate FundingXS outlier guard to `VolatilityFilter`

**Files:**
- Modify: `src/okx_trade/strategies/funding_cross_section.py`

- [ ] **Step 1: Find existing 1m bar feed + outlier check sites**

Run: `grep -n "_closes_1m_by_inst\|outlier_check\|outlier_window_min\|outlier_baseline_min" src/okx_trade/strategies/funding_cross_section.py`

You'll find:
- `__init__`: `self._closes_1m_by_inst: dict[str, deque] = {}`
- `on_bar`: appends to `_closes_1m_by_inst` for 1m bars
- `_compute_target_positions`: calls `outlier_check(closes=list(self._closes_1m_by_inst...))`
- `_warmup_closes_via_rest`: also populates `_closes_1m_by_inst`

- [ ] **Step 2: Delete `_closes_1m_by_inst` from `__init__`**

Remove the deque initialization (it lived in `self._closes_1m_by_inst`).

- [ ] **Step 3: Change `on_bar` to feed the filter**

Find the 1m branch:

```python
            if is_one_minute:
                buf = self._closes_1m_by_inst.setdefault(
                    inst_value, deque(maxlen=2000),
                )
                buf.append(snap.close)
                return
```

Change to:

```python
            if is_one_minute:
                if self._vol_filter is not None:
                    inst_id_okx = inst_value.split(".")[0]
                    self._vol_filter.feed_bar(inst_id_okx, snap.close)
                return
```

- [ ] **Step 4: Change `_compute_target_positions` outlier guard**

Find:

```python
                    if self.config.enable_outlier_guard:
                        ok, reason = outlier_check(
                            closes=list(self._closes_1m_by_inst.get(inst_value, [])),
                            window=self.config.outlier_window_min,
                            baseline=self.config.outlier_baseline_min,
                            warmup=self.config.outlier_warmup_min,
                            ratio_threshold=self.config.outlier_vol_ratio,
                        )
                        if not ok:
                            self.log.warning(
                                f"funding_xs OUTLIER_SKIP inst={inst_value} "
                                f"direction={direction} reason={reason}"
                            )
                            continue
```

Change to:

```python
                    if self.config.enable_outlier_guard:
                        inst_id_okx = inst_value.split(".")[0]
                        ok, reason = self.vol_filter_allow(inst_id_okx)
                        if not ok:
                            self.log.warning(
                                f"funding_xs OUTLIER_SKIP inst={inst_value} "
                                f"direction={direction} reason={reason}"
                            )
                            continue
```

- [ ] **Step 5: Update `_warmup_closes_via_rest` to feed `vol_filter` instead of local dict**

Find the 1m closes warmup section that does `self._closes_1m_by_inst[inst_value] = deque(...)`. Replace with feeding the filter:

```python
                # 2026-05-26 Phase 1: feed shared VolatilityFilter buffer
                if self._vol_filter is not None:
                    inst_id_okx = inst_value.split(".")[0]
                    for c in closes_1m:
                        self._vol_filter.feed_bar(inst_id_okx, c)
```

- [ ] **Step 6: Remove the now-unused `outlier_check` import + `deque` for 1m if no other use**

Run: `grep -n "outlier_check\|deque" src/okx_trade/strategies/funding_cross_section.py`

If `outlier_check` is now only imported from `_isolated_helpers` but not called inside the strategy file (it lives in the service now), drop the import. Keep `deque` if it's still used for other buffers; otherwise drop.

- [ ] **Step 7: Update integration tests that fed `_closes_1m_by_inst` directly**

Run: `grep -n "_closes_1m_by_inst" tests/unit/test_strategy_funding_xs_isolated.py`

In `TestComputeTargetsIntegration` (added in P-Task 18 of Plan 7), the mock fixture sets `m._closes_1m_by_inst = {...}`. Replace by mocking `m._vol_filter`:

```python
        from okx_trade.risk.volatility_filter import VolatilityFilter, VolatilityFilterConfig
        m._vol_filter = VolatilityFilter(
            VolatilityFilterConfig(enable=True), log=_make_null_log(),
        )
        for px in calm_closes:
            m._vol_filter.feed_bar("CALM-USDT-SWAP", float(px))
        for px in wicky_closes:
            m._vol_filter.feed_bar("WICK-USDT-SWAP", float(px))
```

Where `_make_null_log` is the same null-log helper used in other test files (paste a copy into the test file or import from a conftest if convenient).

Also ensure `mock_strategy` returns `vol_filter_allow` bound:

```python
        m.vol_filter_allow = FundingXSStrategy.vol_filter_allow.__get__(m)
```

- [ ] **Step 8: Run FundingXS tests**

Run: `.venv/bin/python -m pytest tests/unit/test_strategy_funding_xs_isolated.py -v`
Expected: all passed.

- [ ] **Step 9: Commit**

```bash
git add src/okx_trade/strategies/funding_cross_section.py tests/unit/test_strategy_funding_xs_isolated.py
git commit -m "refactor(funding_xs): use VolatilityFilter instead of local 1m buffer"
```

---

## Task 16: Full unit suite gate after migration

**Files:** none (gate task).

- [ ] **Step 1: Run full suite**

Run: `.venv/bin/python -m pytest tests/unit/ -q`
Expected: total around 981 pass; no failures.

- [ ] **Step 2: Spot-check imports**

Run: `.venv/bin/python -c "
from okx_trade.risk.isolated_margin_service import IsolatedMarginService, BatchEnsureResult
from okx_trade.risk.volatility_filter import VolatilityFilter, VolatilityFilterConfig
from okx_trade.strategies._okx_base import OkxStrategyBase
from okx_trade.strategies.funding_cross_section import FundingXSStrategy
print('IMPORTS OK')
"`
Expected: `IMPORTS OK`.

- [ ] **Step 3: Verify FundingXS now inherits OkxStrategyBase**

Run: `.venv/bin/python -c "
from okx_trade.strategies.funding_cross_section import FundingXSStrategy
from okx_trade.strategies._okx_base import OkxStrategyBase
print(issubclass(FundingXSStrategy, OkxStrategyBase))
"`
Expected: `True`.

---

## Task 17: Local backtest smoke (FundingXS still works in backtest)

**Files:** none.

- [ ] **Step 1: Run a short FundingXS backtest**

Run:
```
.venv/bin/python scripts/backtest.py --strategy funding_cross_section \
  --instrument-ids BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,DOT-USDT-SWAP,LINK-USDT-SWAP,DOGE-USDT-SWAP \
  --total-bars 72 --top-n 2 --bot-n 2 --reuse-data 2>&1 | tail -20
```
Expected: backtest completes; `events=0 orders=0 positions=0` is fine (short window), key is **no Python tracebacks**.

- [ ] **Step 2: Confirm no isolated-mode log lines in backtest output**

Backtest should fall back to cross mode (service is constructed but `is_backtest()` returns True for the empty-key OKXSettings). Check that the output doesn't contain `set leverage isolated` lines.

---

## Task 18: VPS pre-deploy smoke probe

**Files:** none (smoke verification).

- [ ] **Step 1: Push to origin/main + VPS pull (dry run via probe)**

Use the existing `scripts/probe_okx_isolated.py` to verify OKX still accepts isolated set-leverage from the VPS:

Run: `ssh okx-vps "cd /home/okxtrade/okx-trade && sudo -u okxtrade .venv/bin/python scripts/probe_okx_isolated.py"`
Expected: `PASS: DOT-USDT-SWAP set to isolated lever=3 (posMode=long_short_mode)`.

If PASS, the OKX path is healthy.

---

## Task 19: Deploy to VPS

**Files:** none (deploy).

- [ ] **Step 1: Confirm `git status` is clean and on main**

Run: `git status && git branch --show-current`
Expected: `nothing to commit, working tree clean` and `main`.

- [ ] **Step 2: Push origin/main**

Run: `git push origin main 2>&1 | tail -3`
Expected: push succeeds.

- [ ] **Step 3: VPS pull + restart**

Run:
```
ssh okx-vps "sudo -u okxtrade git -C /home/okxtrade/okx-trade pull --ff-only && sudo systemctl restart okx-trade.service && sleep 8 && systemctl status okx-trade.service --no-pager | head -10"
```
Expected: `Active: active (running)`.

- [ ] **Step 4: Check for startup errors**

Run:
```
ssh okx-vps "journalctl -u okx-trade.service --since '1 minute ago' --no-pager | grep -iE 'error|traceback|fatal' | head -10 || echo NO_ERRORS"
```
Expected: NO_ERRORS (51015 warnings should be gone since the 2026-05-26 fix; iso_margin / vol_filter init logs may appear).

- [ ] **Step 5: Verify service init logs**

Run:
```
ssh okx-vps "journalctl -u okx-trade.service --since '1 minute ago' --no-pager | grep -E 'iso_margin|vol_filter' | head -10"
```
Expected: see `iso_margin` and/or `vol_filter` logger names in some log line, confirming the services were constructed.

---

## Task 20: Post-deploy verify at next funding window

**Files:** none.

Funding windows are at 00:00 / 08:00 / 16:00 UTC. From the deploy time, identify the next window and wait for it.

- [ ] **Step 1: At next funding window + 30s**

Run:
```
ssh okx-vps "journalctl -u okx-trade.service --since '<funding_window_time_utc>' --no-pager | grep -E 'iso_margin|funding_xs (set leverage|ABORT|OUTLIER_SKIP|cached account posMode|OPEN.*mode=isolated)' | head -40"
```

- [ ] **Step 2: Expected behaviors (equivalent to pre-migration FundingXS)**

- `iso_margin cached account posMode=long_short_mode` (first time)
- `iso_margin set leverage inst=XXX mgnMode=isolated posSide=long lever=N` for each opened leg
- If conditions match: `funding_xs OUTLIER_SKIP inst=YYY ...` for skipped legs
- If any leg's set-leverage fails: `funding_xs ABORT rebalance: set-leverage failed for [...]`
- For successful legs: `OPEN short XXX-USDT-SWAP qty=N lever=M.M edge=+X.XX mode=isolated`

- [ ] **Step 3: Verify OKX-side positions**

Run:
```
ssh okx-vps "cd /home/okxtrade/okx-trade && sudo -u okxtrade .venv/bin/python -c '
import asyncio
from okx_trade import OKXRestClient, OKXSettings
async def m():
    async with OKXRestClient(OKXSettings()) as c:
        pos = await c.transport.request(\"GET\", \"/api/v5/account/positions\", private=True, group=None)
        for p in pos:
            if float(p.get(\"pos\", 0) or 0) != 0:
                print(f\"{p[\\\"instId\\\"]:25s} mgnMode={p[\\\"mgnMode\\\"]:10s} posSide={p.get(\\\"posSide\\\")} lever={p.get(\\\"lever\\\")} pos={p[\\\"pos\\\"]}\")
asyncio.run(m())'"
```

Expected: any FundingXS-opened legs show `mgnMode=isolated`.

---

## Task 21: Docs update — ARCHITECTURE.md + CHANGELOG.md

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update ARCHITECTURE.md design decision #10**

Find the existing `### 10. Isolated margin per leg + two-phase set-leverage` section. Add a sub-heading or append:

```markdown
### 10.1 Phase 1 (2026-05-26): extract to shared services

FundingXS-specific implementation was extracted into two singletons in
`src/okx_trade/risk/`:

- **`IsolatedMarginService`** — owns posMode cache + (inst, posSide) →
  lever cache + shared OKXRestClient. Strategies opt-in via DI from
  `build_live_context` and call `ensure_leverage` / `batch_ensure_leverage`.
- **`VolatilityFilter`** — owns per-inst 1m close deques. Strategies that
  subscribe 1m bars call `feed_bar(inst_id, close)`; opt-in strategies
  call `allow(inst_id)` to gate new legs.

Plus an optional thin base class **`OkxStrategyBase`** in
`src/okx_trade/strategies/_okx_base.py` providing a 1-line
`submit_isolated_order(order, lever, pos_side)` helper.

The 9 other strategies (besides FundingXS, already migrated as part of
Phase 1b) opt in incrementally via per-strategy `enable_isolated_margin`
yaml flag — see follow-up plans for each.
```

- [ ] **Step 2: Update CHANGELOG.md `[Unreleased]` block**

Add at the top of the `[Unreleased]` section:

```markdown
### Added / Refactored (Phase 1 — shared isolated-margin services, 2026-05-26)

- **`feat(risk)`** — `IsolatedMarginService` (singleton): posMode cache +
  (inst, posSide)→lever cache + REST plumbing; `ensure_leverage` (idempotent)
  + `batch_ensure_leverage` (two-phase commit) + `is_backtest`.
- **`feat(risk)`** — `VolatilityFilter` (singleton): per-inst 1m close
  deques + `allow(inst_id)` wraps existing `outlier_check` helper.
- **`feat(strategies)`** — `OkxStrategyBase` thin optional base with
  `submit_isolated_order(...)` (7-branch helper) and `vol_filter_allow(...)`.
- **`feat(runtime)`** — `build_live_context` constructs the two services
  and DI-injects them into every strategy + `LiveMonitor` (mirroring
  `account_drawdown_tracker` pattern).
- **`refactor(funding_xs)`** — FundingXS now consumes the services
  instead of inline `_set_leverage_cached` / `_get_account_pos_mode` /
  `_closes_1m_by_inst`. Behavior 100% equivalent (verified at next
  funding window post-deploy). Phase 1b complete; Phase 1c-1f
  (opt-in for 9 other strategies) tracked in follow-up plans.
- **`feat(config)`** — new top-level `volatility_filter` block in
  `configs/live.yaml` (global; not per-strategy).
- Unit tests +~32 (service + filter + base) — total ~981.
```

- [ ] **Step 3: Commit + push**

```bash
git add ARCHITECTURE.md CHANGELOG.md
git commit -m "docs: Phase 1 — extract isolated-margin to shared services"
git push origin main
```

---

## Done. Final acceptance checklist

- [ ] All ~981 unit tests pass (`pytest tests/unit/ -q`)
- [ ] Probe script returns PASS on VPS (Task 18)
- [ ] Backtest runs without traceback (Task 17)
- [ ] Deploy succeeded; service `active (running)` (Task 19)
- [ ] Next funding window logs show `iso_margin set leverage isolated` + FundingXS behavior equivalent to pre-migration (Task 20)
- [ ] OKX REST `/positions` returns `mgnMode=isolated` for FundingXS legs (Task 20)
- [ ] ARCHITECTURE.md + CHANGELOG.md updated (Task 21)
- [ ] Phase 1c-1f follow-up plans (single-leg + multi-leg opt-ins) tracked separately
