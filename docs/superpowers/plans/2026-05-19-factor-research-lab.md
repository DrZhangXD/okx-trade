# Factor Research Lab + FactorPortfolioStrategy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI-driven factor research lab that grades arbitrary candidate factors (IC/IR/decay/turnover/net-PnL) and a generic `FactorPortfolioStrategy` that consumes approved factors from yaml.

**Architecture:** New top-level `src/okx_trade/research/` module (pure numpy + sqlite, zero NT deps) for the research pipeline; a new `strategies/factor_portfolio.py` that reads `configs/factor_portfolio.yaml` and synthesizes weighted z-scores into top-K long / bot-K short trades using existing risk/PnL infrastructure. Two new OKX REST endpoints (`get_open_interest` + history) are the only SDK extension.

**Tech Stack:** Python 3.11+ asyncio, numpy, pyarrow/parquet, sqlite3, pydantic, pytest, nautilus_trader (strategy only).

**Spec:** `docs/superpowers/specs/2026-05-19-factor-research-lab-design.md`

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `src/okx_trade/models/market.py` | Add `OpenInterest` + `OpenInterestPoint` | modify |
| `src/okx_trade/rest/public.py` | Add `get_open_interest` + `get_open_interest_history` + `_extended` | modify |
| `src/okx_trade/research/__init__.py` | Module root + factor auto-import | create |
| `src/okx_trade/research/panel.py` | `FactorPanel` dataclass + builder | create |
| `src/okx_trade/research/registry.py` | `@register_factor` decorator + `_REGISTRY` + lookup | create |
| `src/okx_trade/research/compute.py` | `compute_factor(id, panel) → np.ndarray` | create |
| `src/okx_trade/research/data.py` | `fetch_panel(rest, ...)` + parquet cache | create |
| `src/okx_trade/research/factors/__init__.py` | Trigger all factor module imports | create |
| `src/okx_trade/research/factors/momentum.py` | 4 momentum factors | create |
| `src/okx_trade/research/factors/funding_oi.py` | 4 funding/OI factors | create |
| `src/okx_trade/research/factors/basis.py` | 2 basis factors | create |
| `src/okx_trade/research/factors/volatility.py` | 3 volatility factors | create |
| `src/okx_trade/research/factors/flow.py` | 2 flow factors | create |
| `src/okx_trade/research/grade.py` | `FactorGrade` + `grade_factor()` | create |
| `src/okx_trade/research/store.py` | sqlite schema + ops (`save_grade`, `approve`, …) | create |
| `src/okx_trade/research/walk_forward_grade.py` | OOS rolling IC for one factor | create |
| `src/okx_trade/research/report.py` | Markdown report generator | create |
| `src/okx_trade/research/cli.py` | `python -m okx_trade.research.factor <cmd>` | create |
| `src/okx_trade/research/__main__.py` | Delegates to `cli.main()` | create |
| `src/okx_trade/strategies/factor_portfolio.py` | Pure synth funcs + `FactorPortfolioStrategy` NT class | create |
| `configs/factor_portfolio.yaml` | Initial approved factors + weights | create |
| `scripts/factor_research_smoke.sh` | End-to-end smoke regression | create |
| `tests/unit/research/__init__.py` | Test package marker | create |
| `tests/unit/research/test_*.py` | One test file per research module | create |
| `tests/unit/research/factors/test_*.py` | One test file per factor module | create |
| `tests/unit/strategies/test_strategy_factor_portfolio.py` | Pure-func + NT strategy tests | create |
| `tests/unit/test_rest_open_interest.py` | OI endpoint tests | create |
| `docs/strategy_roadmap.md` | Add `FactorPortfolioStrategy` row + P1 note | modify |
| `README.md` | Mention factor research lab in 3-layer diagram | modify |

---

## Conventions Used Throughout

- **Tests live at** `tests/unit/<mirror-of-src-path>` (e.g., `src/okx_trade/research/grade.py` → `tests/unit/research/test_grade.py`).
- **Commit message style** (matches recent log): `<type>(<scope>): <subject>` where type ∈ {`add`, `fix`, `enable`, `refactor`, `docs`}.
- **Run all tests** with `pytest tests/unit -v`. Project currently has 449 unit tests; target ≥ 530 after this plan.
- **Imports**: `from __future__ import annotations` at top of every new file (project-wide style).
- **Type hints**: use `list[X]` / `dict[K, V]` / `X | None` (PEP 604), not `Optional[X]`.
- **Dataclasses**: prefer `@dataclass(frozen=True, slots=True)` for pure data types (project style).
- **No emojis** in code/docs/commits (project style).

---

## Task 1: Add `OpenInterest` + `OpenInterestPoint` models

**Files:**
- Modify: `src/okx_trade/models/market.py` (append after existing classes)
- Test:   `tests/unit/test_rest_open_interest.py` (model tests at top)

- [ ] **Step 1: Write failing test for `OpenInterest.model_validate`**

Create `tests/unit/test_rest_open_interest.py`:

```python
"""Tests for OKX open-interest endpoints + models."""
from __future__ import annotations

from decimal import Decimal

import pytest

from okx_trade.models.market import OpenInterest, OpenInterestPoint


def test_open_interest_parses_okx_response() -> None:
    raw = {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "oi": "12345.6",        # 张数（contracts）
        "oiCcy": "123.456",     # base ccy 名义
        "oiUsd": "8765432.10",  # USD 名义
        "ts": "1716120000000",
    }
    obj = OpenInterest.model_validate(raw)
    assert obj.inst_id == "BTC-USDT-SWAP"
    assert obj.oi == Decimal("12345.6")
    assert obj.oi_ccy == Decimal("123.456")
    assert obj.oi_usd == Decimal("8765432.10")
    assert obj.ts == 1716120000000


def test_open_interest_point_parses_history_row() -> None:
    # rubik/stat/contracts/open-interest-volume returns arrays [ts, oi_ccy, vol_ccy]
    row = ["1716120000000", "123456.78", "9876543.21"]
    obj = OpenInterestPoint.from_array(row)
    assert obj.ts == 1716120000000
    assert obj.oi_ccy == Decimal("123456.78")
    assert obj.vol_ccy == Decimal("9876543.21")
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/test_rest_open_interest.py -v`
Expected: `ImportError: cannot import name 'OpenInterest' from 'okx_trade.models.market'`

- [ ] **Step 3: Add models to `src/okx_trade/models/market.py`**

Append (do not change existing classes):

```python
class OpenInterest(OKXModel):
    """``GET /api/v5/public/open-interest`` 单条响应。

    OKX 返回三种 OI 口径：
    - ``oi``: 张数（contracts），最原始
    - ``oi_ccy``: base 币种名义（适合跨币种比较）
    - ``oi_usd``: USD 名义（适合 OI/volume 比例计算）
    """
    inst_id: str = Field(alias="instId")
    inst_type: str = Field(default="SWAP", alias="instType")
    oi: Decimal = Field(default=Decimal("0"), alias="oi")
    oi_ccy: Decimal = Field(default=Decimal("0"), alias="oiCcy")
    oi_usd: Decimal = Field(default=Decimal("0"), alias="oiUsd")
    ts: int = Field(default=0, alias="ts")


class OpenInterestPoint(OKXModel):
    """``GET /api/v5/rubik/stat/contracts/open-interest-volume`` 单条历史点。

    rubik 端点返回数组 ``[ts, oiCcy, volCcy]`` 而非 dict，需要 ``from_array`` 解析。
    历史 OI 只暴露 base 币种口径（``oi_ccy``），不带 USD/contracts。
    """
    ts: int
    oi_ccy: Decimal = Decimal("0")
    vol_ccy: Decimal = Decimal("0")

    @classmethod
    def from_array(cls, row: list[str]) -> OpenInterestPoint:
        ts, oi_ccy, vol_ccy = row[0], row[1], row[2]
        return cls(ts=int(ts), oi_ccy=Decimal(oi_ccy), vol_ccy=Decimal(vol_ccy))
```

Update the existing `__all__` at bottom of `market.py` to include `"OpenInterest"` and `"OpenInterestPoint"`.

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/test_rest_open_interest.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/models/market.py tests/unit/test_rest_open_interest.py
git commit -m "add(models): OpenInterest + OpenInterestPoint for factor research"
```

---

## Task 2: Add `public.get_open_interest` + history endpoints

**Files:**
- Modify: `src/okx_trade/rest/public.py` (append two methods to `PublicEndpoints`)
- Test:   `tests/unit/test_rest_open_interest.py` (append endpoint tests)

- [ ] **Step 1: Write failing tests for `get_open_interest` + `_history` + `_extended`**

Append to `tests/unit/test_rest_open_interest.py`:

```python
class _FakeTransport:
    """Pure-Python transport stub: record call, return canned response."""
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method, path, *, params=None, group=None, **_):
        self.calls.append((method, path, dict(params or {})))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_get_open_interest_calls_correct_endpoint() -> None:
    fake = _FakeTransport(responses=[[{
        "instId": "ETH-USDT-SWAP", "instType": "SWAP",
        "oi": "100", "oiCcy": "10", "oiUsd": "30000", "ts": "1716120000000",
    }]])
    from okx_trade.rest.public import PublicEndpoints
    pub = PublicEndpoints(fake)  # type: ignore[arg-type]
    res = await pub.get_open_interest("ETH-USDT-SWAP")
    assert res.inst_id == "ETH-USDT-SWAP"
    assert res.oi_usd == Decimal("30000")
    method, path, params = fake.calls[0]
    assert path == "/api/v5/public/open-interest"
    assert params == {"instType": "SWAP", "instId": "ETH-USDT-SWAP"}


@pytest.mark.asyncio
async def test_get_open_interest_history_parses_array_rows() -> None:
    fake = _FakeTransport(responses=[[
        ["1716120000000", "123.4", "9876.5"],
        ["1716123600000", "125.0", "10000.0"],
    ]])
    from okx_trade.rest.public import PublicEndpoints
    pub = PublicEndpoints(fake)  # type: ignore[arg-type]
    rows = await pub.get_open_interest_history("BTC-USDT", period="1H")
    assert len(rows) == 2
    assert rows[0].ts == 1716120000000
    assert rows[1].oi_ccy == Decimal("125.0")
    method, path, params = fake.calls[0]
    assert path == "/api/v5/rubik/stat/contracts/open-interest-volume"
    assert params["ccy"] == "BTC"
    assert params["period"] == "1H"


@pytest.mark.asyncio
async def test_get_open_interest_history_extended_pages_until_total() -> None:
    page1 = [["1700000000000", "1", "1"], ["1700003600000", "2", "2"]]
    page2 = [["1699996400000", "3", "3"]]
    page3: list = []  # empty → loop exits
    fake = _FakeTransport(responses=[page1, page2, page3])
    from okx_trade.rest.public import PublicEndpoints
    pub = PublicEndpoints(fake)  # type: ignore[arg-type]
    rows = await pub.get_open_interest_history_extended("BTC-USDT", period="1H", total=3)
    assert len(rows) == 3
    assert rows[0].ts < rows[1].ts < rows[2].ts  # ascending
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/test_rest_open_interest.py -v`
Expected: 3 new failures: `AttributeError: 'PublicEndpoints' object has no attribute 'get_open_interest'` etc.

- [ ] **Step 3: Add endpoints to `src/okx_trade/rest/public.py`**

At top of file, extend the existing import line:

```python
from ..models.common import FundingRate, Instrument, OptionSummary
from ..models.market import OpenInterest, OpenInterestPoint
```

Append these methods to the `PublicEndpoints` class (before `__all__`):

```python
    async def get_open_interest(
        self,
        inst_id: str,
        inst_type: str = "SWAP",
    ) -> OpenInterest:
        """``GET /api/v5/public/open-interest`` —— 即时持仓量。

        ``inst_type`` 默认 ``SWAP``（永续）；交割合约传 ``FUTURES``。
        """
        params = {"instType": inst_type, "instId": inst_id}
        data = await self._t.request(
            "GET", "/api/v5/public/open-interest",
            params=params, group="public.open_interest",
        )
        if not data:
            raise OKXAPIError(
                code="not_found",
                message=f"no open interest for {inst_id}",
                endpoint="/api/v5/public/open-interest",
            )
        return OpenInterest.model_validate(data[0])

    async def get_open_interest_history(
        self,
        inst_id: str,
        *,
        period: str = "1H",
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
    ) -> list[OpenInterestPoint]:
        """``GET /api/v5/rubik/stat/contracts/open-interest-volume`` —— 历史 OI + volume。

        参数说明：
        - ``inst_id``: rubik 端点用 ``ccy``（base 币种），这里接受 ``BTC-USDT`` 形式，
          内部取首段当 ccy；也接受裸 ``BTC``。
        - ``period``: ``5m / 1H / 1D``（rubik 限定枚举）。
        - 翻页：``after``=毫秒时间戳，向更早翻页。

        返回按 ``ts`` 升序。
        """
        ccy = inst_id.split("-", 1)[0] if "-" in inst_id else inst_id
        params: dict[str, str] = {"ccy": ccy, "period": period, "limit": str(limit)}
        if before is not None:
            params["begin"] = str(before)
        if after is not None:
            params["end"] = str(after)
        data = await self._t.request(
            "GET", "/api/v5/rubik/stat/contracts/open-interest-volume",
            params=params, group="public.open_interest_history",
        )
        points = [OpenInterestPoint.from_array(row) for row in data]
        points.sort(key=lambda p: p.ts)
        return points

    async def get_open_interest_history_extended(
        self,
        inst_id: str,
        *,
        period: str = "1H",
        total: int = 720,
    ) -> list[OpenInterestPoint]:
        """分页拉历史 OI（每页 100 条），按时间升序返回。

        与 ``get_funding_rate_history_extended`` 同样的去重 + 游标逻辑。
        """
        all_pts: list[OpenInterestPoint] = []
        seen: set[int] = set()
        cursor: int | None = None
        while len(all_pts) < total:
            page_size = min(100, total - len(all_pts))
            batch = await self.get_open_interest_history(
                inst_id, period=period, limit=page_size, after=cursor,
            )
            new_rows = [p for p in batch if p.ts not in seen]
            if not new_rows:
                break
            all_pts.extend(new_rows)
            seen.update(p.ts for p in new_rows)
            cursor = min(p.ts for p in new_rows)
        all_pts.sort(key=lambda p: p.ts)
        return all_pts
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/test_rest_open_interest.py -v`
Expected: 5 passed (2 model + 3 endpoint tests).

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/rest/public.py tests/unit/test_rest_open_interest.py
git commit -m "add(rest): open-interest + history endpoints for factor research"
```

---

## Task 3: `research/panel.py` — `FactorPanel` dataclass

**Files:**
- Create: `src/okx_trade/research/__init__.py` (empty stub for now)
- Create: `src/okx_trade/research/panel.py`
- Create: `tests/unit/research/__init__.py` (empty)
- Create: `tests/unit/research/test_panel.py`

- [ ] **Step 1: Create empty package files**

```bash
mkdir -p src/okx_trade/research/factors tests/unit/research/factors
: > src/okx_trade/research/__init__.py
: > src/okx_trade/research/factors/__init__.py
: > tests/unit/research/__init__.py
: > tests/unit/research/factors/__init__.py
```

- [ ] **Step 2: Write failing tests**

Create `tests/unit/research/test_panel.py`:

```python
"""Tests for FactorPanel: shape invariants + builder."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.panel import FactorPanel, panel_from_dicts


def test_factor_panel_shapes_match() -> None:
    p = FactorPanel(
        inst_ids=("BTC-USDT-SWAP", "ETH-USDT-SWAP"),
        timestamps_ms=(1_700_000_000_000, 1_700_003_600_000),
        close=np.array([[10.0, 1.0], [11.0, 1.1]]),
        volume_usdt=np.array([[1e6, 1e5], [1.1e6, 1.05e5]]),
        funding_rate=None,
        open_interest=None,
        basis_apr=None,
    )
    assert p.t == 2
    assert p.n == 2
    assert p.close.shape == (2, 2)


def test_factor_panel_rejects_mismatched_shapes() -> None:
    with pytest.raises(ValueError, match="shape"):
        FactorPanel(
            inst_ids=("A", "B"),
            timestamps_ms=(1, 2, 3),  # T=3 but close says T=2
            close=np.array([[1.0, 2.0], [3.0, 4.0]]),
            volume_usdt=np.array([[1.0, 1.0], [1.0, 1.0]]),
            funding_rate=None, open_interest=None, basis_apr=None,
        )


def test_panel_from_dicts_aligns_timestamps_outer_join() -> None:
    # BTC has 3 bars (1000, 2000, 3000), ETH only has 2 (2000, 3000)
    by_inst = {
        "BTC": {
            "close":  [(1000, 10.0), (2000, 11.0), (3000, 12.0)],
            "volume_usdt": [(1000, 100.0), (2000, 110.0), (3000, 120.0)],
        },
        "ETH": {
            "close":  [(2000, 1.0), (3000, 1.1)],
            "volume_usdt": [(2000, 10.0), (3000, 11.0)],
        },
    }
    p = panel_from_dicts(by_inst)
    assert p.inst_ids == ("BTC", "ETH")
    assert p.timestamps_ms == (1000, 2000, 3000)
    # ETH at ts=1000 should be NaN
    assert np.isnan(p.close[0, 1])
    assert p.close[1, 1] == 1.0
    assert p.close[2, 0] == 12.0
```

- [ ] **Step 3: Run, verify fail**

Run: `pytest tests/unit/research/test_panel.py -v`
Expected: `ModuleNotFoundError: No module named 'okx_trade.research.panel'`.

- [ ] **Step 4: Implement `src/okx_trade/research/panel.py`**

```python
"""FactorPanel: aligned (T, N) snapshot of close + funding + OI + basis for a set of instruments.

设计：所有时序数组 shape 严格 (T, N)；NaN 表示该 (t, inst) 缺数据。因子函数自己决定
nan-aware 还是 skip。inst_ids 顺序固定 = numpy 列顺序，与 yaml/sqlite 内的 id 列表一一对应。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class FactorPanel:
    inst_ids: tuple[str, ...]
    timestamps_ms: tuple[int, ...]
    close: np.ndarray
    volume_usdt: np.ndarray
    funding_rate: np.ndarray | None
    open_interest: np.ndarray | None
    basis_apr: np.ndarray | None

    def __post_init__(self) -> None:
        t, n = len(self.timestamps_ms), len(self.inst_ids)
        for name in ("close", "volume_usdt"):
            arr = getattr(self, name)
            if arr.shape != (t, n):
                raise ValueError(
                    f"{name} shape {arr.shape} != expected ({t}, {n})"
                )
        for name in ("funding_rate", "open_interest", "basis_apr"):
            arr = getattr(self, name)
            if arr is not None and arr.shape != (t, n):
                raise ValueError(
                    f"{name} shape {arr.shape} != expected ({t}, {n})"
                )

    @property
    def t(self) -> int:
        return len(self.timestamps_ms)

    @property
    def n(self) -> int:
        return len(self.inst_ids)


def panel_from_dicts(
    by_inst: dict[str, dict[str, list[tuple[int, float]]]],
) -> FactorPanel:
    """Build a FactorPanel from per-instrument per-field timeseries.

    ``by_inst[inst_id][field] = [(ts_ms, value), ...]`` ascending ts.
    All series across all instruments are outer-joined by ts; missing → NaN.

    Supported fields: ``close`` (required), ``volume_usdt`` (required),
    ``funding_rate``, ``open_interest``, ``basis_apr``.
    """
    inst_ids = tuple(sorted(by_inst.keys()))
    all_ts: set[int] = set()
    for fields in by_inst.values():
        for series in fields.values():
            all_ts.update(ts for ts, _ in series)
    timestamps = tuple(sorted(all_ts))
    ts_index = {ts: i for i, ts in enumerate(timestamps)}
    T, N = len(timestamps), len(inst_ids)

    def _alloc(field: str, *, required: bool) -> np.ndarray | None:
        any_present = any(field in by_inst[i] for i in inst_ids)
        if not required and not any_present:
            return None
        arr = np.full((T, N), np.nan, dtype=float)
        for col, inst in enumerate(inst_ids):
            series = by_inst[inst].get(field, [])
            for ts, val in series:
                arr[ts_index[ts], col] = val
        return arr

    close = _alloc("close", required=True)
    volume_usdt = _alloc("volume_usdt", required=True)
    funding_rate = _alloc("funding_rate", required=False)
    open_interest = _alloc("open_interest", required=False)
    basis_apr = _alloc("basis_apr", required=False)

    assert close is not None and volume_usdt is not None  # required=True
    return FactorPanel(
        inst_ids=inst_ids,
        timestamps_ms=timestamps,
        close=close,
        volume_usdt=volume_usdt,
        funding_rate=funding_rate,
        open_interest=open_interest,
        basis_apr=basis_apr,
    )


__all__ = ["FactorPanel", "panel_from_dicts"]
```

- [ ] **Step 5: Run, verify pass**

Run: `pytest tests/unit/research/test_panel.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/research/__init__.py src/okx_trade/research/panel.py \
        src/okx_trade/research/factors/__init__.py \
        tests/unit/research/__init__.py tests/unit/research/factors/__init__.py \
        tests/unit/research/test_panel.py
git commit -m "add(research): FactorPanel dataclass + panel_from_dicts builder"
```

---

## Task 4: `research/registry.py` — `@register_factor` decorator

**Files:**
- Create: `src/okx_trade/research/registry.py`
- Create: `tests/unit/research/test_registry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_registry.py`:

```python
"""Tests for the factor registry."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.registry import (
    FactorSpec,
    clear_registry,
    get_factor,
    list_factors,
    register_factor,
)
from okx_trade.research.panel import FactorPanel


@pytest.fixture(autouse=True)
def _isolate_registry():
    clear_registry()
    yield
    clear_registry()


def _toy_panel() -> FactorPanel:
    return FactorPanel(
        inst_ids=("A",), timestamps_ms=(1, 2),
        close=np.array([[1.0], [2.0]]),
        volume_usdt=np.array([[1.0], [1.0]]),
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_register_factor_stores_spec_and_callable() -> None:
    @register_factor(
        id="toy",
        category="test",
        description="toy",
        direction="long_high",
        required_data=("close",),
        min_history_bars=1,
        rebalance_minutes=60,
    )
    def f(panel: FactorPanel) -> np.ndarray:
        return panel.close.copy()

    spec = get_factor("toy")
    assert isinstance(spec, FactorSpec)
    assert spec.id == "toy"
    assert spec.direction == "long_high"
    assert spec.required_data == ("close",)
    assert spec.func(_toy_panel()).shape == (2, 1)


def test_duplicate_registration_raises() -> None:
    @register_factor(id="dup", category="t", description="", direction="long_high",
                     required_data=("close",), min_history_bars=1, rebalance_minutes=60)
    def f(p): return p.close

    with pytest.raises(ValueError, match="already registered"):
        @register_factor(id="dup", category="t", description="", direction="long_high",
                         required_data=("close",), min_history_bars=1, rebalance_minutes=60)
        def g(p): return p.close


def test_invalid_direction_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        @register_factor(id="bad", category="t", description="", direction="up",  # type: ignore[arg-type]
                         required_data=("close",), min_history_bars=1, rebalance_minutes=60)
        def f(p): return p.close


def test_get_factor_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="no_such_factor"):
        get_factor("no_such_factor")


def test_list_factors_returns_sorted_by_id() -> None:
    for fid in ("zeta", "alpha", "mu"):
        @register_factor(id=fid, category="t", description="", direction="long_high",
                         required_data=("close",), min_history_bars=1, rebalance_minutes=60)
        def f(p): return p.close
    ids = [s.id for s in list_factors()]
    assert ids == ["alpha", "mu", "zeta"]
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_registry.py -v`
Expected: `ModuleNotFoundError: No module named 'okx_trade.research.registry'`.

- [ ] **Step 3: Implement `src/okx_trade/research/registry.py`**

```python
"""Factor registry: @register_factor decorator + global lookup.

Pure module-level dict — no thread safety required (research pipeline is single-threaded;
strategy reads at startup only).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import numpy as np

from .panel import FactorPanel

FactorFunc = Callable[[FactorPanel], np.ndarray]
Direction = Literal["long_high", "long_low"]
_VALID_DIRECTIONS = ("long_high", "long_low")


@dataclass(frozen=True, slots=True)
class FactorSpec:
    id: str
    category: str
    description: str
    direction: Direction
    required_data: tuple[str, ...]
    min_history_bars: int
    rebalance_minutes: int
    func: FactorFunc


_REGISTRY: dict[str, FactorSpec] = {}


def register_factor(
    *,
    id: str,
    category: str,
    description: str,
    direction: Direction,
    required_data: tuple[str, ...],
    min_history_bars: int,
    rebalance_minutes: int,
) -> Callable[[FactorFunc], FactorFunc]:
    """Decorator that registers a factor function.

    Raises:
        ValueError: if ``id`` already registered or ``direction`` invalid.
    """
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(
            f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}"
        )

    def deco(func: FactorFunc) -> FactorFunc:
        if id in _REGISTRY:
            raise ValueError(f"factor {id!r} already registered")
        _REGISTRY[id] = FactorSpec(
            id=id, category=category, description=description,
            direction=direction, required_data=tuple(required_data),
            min_history_bars=min_history_bars,
            rebalance_minutes=rebalance_minutes, func=func,
        )
        return func

    return deco


def get_factor(factor_id: str) -> FactorSpec:
    if factor_id not in _REGISTRY:
        raise KeyError(f"factor {factor_id!r} not registered")
    return _REGISTRY[factor_id]


def list_factors() -> list[FactorSpec]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def clear_registry() -> None:
    """Test-only: wipe registry between tests."""
    _REGISTRY.clear()


__all__ = [
    "Direction", "FactorFunc", "FactorSpec",
    "clear_registry", "get_factor", "list_factors", "register_factor",
]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_registry.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/registry.py tests/unit/research/test_registry.py
git commit -m "add(research): factor registry with @register_factor decorator"
```

---

## Task 5: `research/compute.py` — apply factor to panel

**Files:**
- Create: `src/okx_trade/research/compute.py`
- Create: `tests/unit/research/test_compute.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_compute.py`:

```python
"""Tests for compute_factor: applies a registered factor to a panel, validates required_data."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    yield
    clear_registry()


def _panel_with(funding=False) -> FactorPanel:
    T, N = 4, 2
    fr = np.zeros((T, N)) if funding else None
    return FactorPanel(
        inst_ids=("A", "B"), timestamps_ms=(1, 2, 3, 4),
        close=np.arange(T * N, dtype=float).reshape(T, N),
        volume_usdt=np.ones((T, N)),
        funding_rate=fr, open_interest=None, basis_apr=None,
    )


def test_compute_factor_returns_shape_matching_panel() -> None:
    @register_factor(id="identity", category="t", description="",
                     direction="long_high", required_data=("close",),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.close.copy()

    out = compute_factor("identity", _panel_with())
    assert out.shape == (4, 2)
    np.testing.assert_array_equal(out, _panel_with().close)


def test_compute_factor_raises_if_required_data_missing() -> None:
    @register_factor(id="needs_funding", category="t", description="",
                     direction="long_high", required_data=("funding_rate",),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.funding_rate  # type: ignore[return-value]

    with pytest.raises(ValueError, match="funding_rate"):
        compute_factor("needs_funding", _panel_with(funding=False))


def test_compute_factor_raises_if_output_shape_wrong() -> None:
    @register_factor(id="bad_shape", category="t", description="",
                     direction="long_high", required_data=("close",),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return np.zeros((3, 3))  # wrong shape

    with pytest.raises(ValueError, match="shape"):
        compute_factor("bad_shape", _panel_with())
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_compute.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/compute.py`**

```python
"""compute_factor: validate panel has required data, run factor, validate output shape."""
from __future__ import annotations

import numpy as np

from .panel import FactorPanel
from .registry import get_factor


def compute_factor(factor_id: str, panel: FactorPanel) -> np.ndarray:
    """Run a registered factor on a panel.

    Raises:
        ValueError: if panel is missing any of the factor's ``required_data``
            (the corresponding panel attribute is ``None``), or if the factor
            function returns an array whose shape != ``(panel.t, panel.n)``.
    """
    spec = get_factor(factor_id)
    for field in spec.required_data:
        if getattr(panel, field, None) is None:
            raise ValueError(
                f"factor {factor_id!r} requires panel.{field} but it is None"
            )
    out = spec.func(panel)
    if not isinstance(out, np.ndarray):
        raise ValueError(
            f"factor {factor_id!r} returned {type(out).__name__}, expected np.ndarray"
        )
    if out.shape != (panel.t, panel.n):
        raise ValueError(
            f"factor {factor_id!r} returned shape {out.shape}, "
            f"expected ({panel.t}, {panel.n})"
        )
    return out


__all__ = ["compute_factor"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_compute.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/compute.py tests/unit/research/test_compute.py
git commit -m "add(research): compute_factor with required_data + shape validation"
```

---

## Task 6: Momentum factors (4 factors)

**Files:**
- Create: `src/okx_trade/research/factors/momentum.py`
- Create: `tests/unit/research/factors/test_momentum.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/factors/test_momentum.py`:

```python
"""Tests for momentum factors."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, get_factor


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    # Re-import the factors module to re-register after wipe
    import importlib
    import okx_trade.research.factors.momentum as m
    importlib.reload(m)
    yield
    clear_registry()


def _panel(close: np.ndarray) -> FactorPanel:
    T, N = close.shape
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)),
        timestamps_ms=tuple(range(T)),
        close=close,
        volume_usdt=np.ones((T, N)),
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_momentum_1d_at_position_24_equals_24h_return() -> None:
    # 25 hourly bars, single inst, linear price
    closes = np.linspace(100.0, 124.0, 25).reshape(25, 1)
    out = compute_factor("momentum_1d", _panel(closes))
    # Before bar 24 → NaN (insufficient history)
    assert np.isnan(out[23, 0])
    # At bar 24: (124/100) - 1 = 0.24
    assert out[24, 0] == pytest.approx(0.24)


def test_momentum_7d_uses_168_bar_lookback() -> None:
    spec = get_factor("momentum_7d")
    assert spec.min_history_bars == 168
    closes = np.ones((200, 1)) * 100.0
    closes[168:, 0] = 110.0  # 10% jump at bar 168
    out = compute_factor("momentum_7d", _panel(closes))
    # At bar 168, price = 110, ref = closes[0] = 100 → 0.10
    assert out[168, 0] == pytest.approx(0.10)


def test_momentum_risk_adj_7d_divides_by_rv30d() -> None:
    # Constant price → rv=0 → factor should be NaN (no divide by zero)
    closes = np.ones((300, 1)) * 100.0
    out = compute_factor("momentum_risk_adj_7d", _panel(closes))
    assert np.isnan(out[-1, 0])


def test_all_momentum_factors_registered() -> None:
    for fid in ("momentum_1d", "momentum_3d", "momentum_7d", "momentum_risk_adj_7d"):
        spec = get_factor(fid)
        assert spec.category == "momentum"
        assert spec.required_data == ("close",) or spec.required_data == ("close", "volume_usdt")
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/factors/test_momentum.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/factors/momentum.py`**

```python
"""Momentum factors: 1d/3d/7d price momentum + risk-adjusted variant.

Panel frequency assumed 1H bars (24 bars = 1 day). Insufficient history → NaN.
"""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor


def _trailing_return(close: np.ndarray, lookback: int) -> np.ndarray:
    """(close_t / close_{t-lookback}) - 1, NaN for t < lookback."""
    T, N = close.shape
    out = np.full_like(close, np.nan, dtype=float)
    if T > lookback:
        ref = close[:-lookback]
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = close[lookback:] / ref
        out[lookback:] = ratio - 1.0
    return out


def _rolling_std(arr: np.ndarray, window: int) -> np.ndarray:
    """Population stdev over trailing ``window`` rows; NaN for rows < window-1."""
    T, N = arr.shape
    out = np.full_like(arr, np.nan, dtype=float)
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = arr[t - window + 1 : t + 1]
        out[t] = np.nanstd(slice_, axis=0)
    return out


@register_factor(
    id="momentum_1d", category="momentum",
    description="24h price momentum (close_t / close_{t-24h}) - 1",
    direction="long_high", required_data=("close",),
    min_history_bars=24, rebalance_minutes=240,
)
def momentum_1d(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 24)


@register_factor(
    id="momentum_3d", category="momentum",
    description="3-day price momentum",
    direction="long_high", required_data=("close",),
    min_history_bars=72, rebalance_minutes=240,
)
def momentum_3d(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 72)


@register_factor(
    id="momentum_7d", category="momentum",
    description="7-day price momentum",
    direction="long_high", required_data=("close",),
    min_history_bars=168, rebalance_minutes=240,
)
def momentum_7d(panel: FactorPanel) -> np.ndarray:
    return _trailing_return(panel.close, 168)


@register_factor(
    id="momentum_risk_adj_7d", category="momentum",
    description="momentum_7d / realized_vol_30d (Sharpe-like)",
    direction="long_high", required_data=("close",),
    min_history_bars=720, rebalance_minutes=240,
)
def momentum_risk_adj_7d(panel: FactorPanel) -> np.ndarray:
    mom = _trailing_return(panel.close, 168)
    # log returns over 30d=720 bars, std as rv proxy
    with np.errstate(divide="ignore", invalid="ignore"):
        log_ret = np.log(panel.close[1:] / panel.close[:-1])
    log_ret = np.vstack([np.full((1, panel.n), np.nan), log_ret])
    rv = _rolling_std(log_ret, 720)
    out = np.full_like(mom, np.nan, dtype=float)
    mask = (rv > 0) & np.isfinite(rv) & np.isfinite(mom)
    out[mask] = mom[mask] / rv[mask]
    return out


__all__ = ["momentum_1d", "momentum_3d", "momentum_7d", "momentum_risk_adj_7d"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/factors/test_momentum.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/factors/momentum.py tests/unit/research/factors/test_momentum.py
git commit -m "add(research/factors): 4 momentum factors with rolling helpers"
```

---

## Task 7: Funding + OI factors (4 factors)

**Files:**
- Create: `src/okx_trade/research/factors/funding_oi.py`
- Create: `tests/unit/research/factors/test_funding_oi.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/factors/test_funding_oi.py`:

```python
"""Tests for funding + open-interest factors."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, get_factor


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    import okx_trade.research.factors.funding_oi as m
    importlib.reload(m)
    yield
    clear_registry()


def _panel(T: int, N: int = 2, *, with_funding=True, with_oi=True) -> FactorPanel:
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)),
        timestamps_ms=tuple(range(T)),
        close=np.ones((T, N)) * 100.0,
        volume_usdt=np.ones((T, N)) * 1e6,
        funding_rate=(np.ones((T, N)) * 0.0001 if with_funding else None),
        open_interest=(np.ones((T, N)) * 1000.0 if with_oi else None),
        basis_apr=None,
    )


def test_funding_current_passes_through() -> None:
    p = _panel(10)
    p.funding_rate[5, 0] = 0.0005
    out = compute_factor("funding_current", p)
    assert out[5, 0] == pytest.approx(0.0005)


def test_funding_z_30d_normalizes_by_window() -> None:
    T = 800
    p = _panel(T)
    # Make funding mostly 0.0001 but spike at t=T-1
    p.funding_rate[:] = 0.0001
    p.funding_rate[T - 1, 0] = 0.0010
    out = compute_factor("funding_z_30d", p)
    assert out[T - 1, 0] > 5.0  # huge z-score


def test_oi_change_1d_is_24h_delta_ratio() -> None:
    T = 50
    p = _panel(T)
    p.open_interest[:] = 1000.0
    p.open_interest[T - 1, 0] = 1100.0  # +10% jump at last bar
    out = compute_factor("oi_change_1d", p)
    assert out[T - 1, 0] == pytest.approx(0.10)


def test_oi_to_volume_ratio_uses_24h_avg_volume() -> None:
    T = 30
    p = _panel(T)
    p.open_interest[:] = 5e6
    p.volume_usdt[:] = 1e6
    out = compute_factor("oi_to_volume_ratio", p)
    # OI/avg_vol = 5e6 / 1e6 = 5.0
    assert out[T - 1, 0] == pytest.approx(5.0)


def test_factors_raise_when_required_field_missing() -> None:
    p_no_funding = _panel(50, with_funding=False)
    with pytest.raises(ValueError, match="funding_rate"):
        compute_factor("funding_current", p_no_funding)
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/factors/test_funding_oi.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/factors/funding_oi.py`**

```python
"""Funding-rate + open-interest factors."""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor
from .momentum import _rolling_std, _trailing_return


@register_factor(
    id="funding_current", category="funding_oi",
    description="Current 8h funding rate (raw, low = expensive shorts → long signal)",
    direction="long_low", required_data=("funding_rate",),
    min_history_bars=1, rebalance_minutes=480,
)
def funding_current(panel: FactorPanel) -> np.ndarray:
    assert panel.funding_rate is not None
    return panel.funding_rate.copy()


@register_factor(
    id="funding_z_30d", category="funding_oi",
    description="Funding rate z-score over trailing 30 days (low z = long signal)",
    direction="long_low", required_data=("funding_rate",),
    min_history_bars=720, rebalance_minutes=480,
)
def funding_z_30d(panel: FactorPanel) -> np.ndarray:
    assert panel.funding_rate is not None
    fr = panel.funding_rate
    T = fr.shape[0]
    out = np.full_like(fr, np.nan, dtype=float)
    window = 720
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = fr[t - window + 1 : t + 1]
        mu = np.nanmean(slice_, axis=0)
        sd = np.nanstd(slice_, axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(sd > 0, (fr[t] - mu) / sd, np.nan)
    return out


@register_factor(
    id="oi_change_1d", category="funding_oi",
    description="24h change in open interest, (oi_t / oi_{t-24h}) - 1",
    direction="long_high", required_data=("open_interest",),
    min_history_bars=24, rebalance_minutes=240,
)
def oi_change_1d(panel: FactorPanel) -> np.ndarray:
    assert panel.open_interest is not None
    return _trailing_return(panel.open_interest, 24)


@register_factor(
    id="oi_to_volume_ratio", category="funding_oi",
    description="OI divided by trailing 24h avg volume (high = sticky positioning)",
    direction="long_high", required_data=("open_interest", "volume_usdt"),
    min_history_bars=24, rebalance_minutes=240,
)
def oi_to_volume_ratio(panel: FactorPanel) -> np.ndarray:
    assert panel.open_interest is not None
    vol, oi = panel.volume_usdt, panel.open_interest
    T = vol.shape[0]
    out = np.full_like(vol, np.nan, dtype=float)
    if T < 24:
        return out
    for t in range(23, T):
        avg_vol = np.nanmean(vol[t - 23 : t + 1], axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(avg_vol > 0, oi[t] / avg_vol, np.nan)
    return out


__all__ = [
    "funding_current", "funding_z_30d",
    "oi_change_1d", "oi_to_volume_ratio",
]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/factors/test_funding_oi.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/factors/funding_oi.py \
        tests/unit/research/factors/test_funding_oi.py
git commit -m "add(research/factors): funding_current/z + oi_change/ratio"
```

---

## Task 8: Basis factors (2 factors)

**Files:**
- Create: `src/okx_trade/research/factors/basis.py`
- Create: `tests/unit/research/factors/test_basis.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/factors/test_basis.py`:

```python
"""Tests for basis factors."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    import okx_trade.research.factors.basis as m
    importlib.reload(m)
    yield
    clear_registry()


def _panel(T: int, basis_value: float = 0.05) -> FactorPanel:
    return FactorPanel(
        inst_ids=("BTC", "ETH"), timestamps_ms=tuple(range(T)),
        close=np.ones((T, 2)) * 100.0,
        volume_usdt=np.ones((T, 2)) * 1e6,
        funding_rate=None, open_interest=None,
        basis_apr=np.ones((T, 2)) * basis_value,
    )


def test_basis_apr_passes_through() -> None:
    p = _panel(10, basis_value=0.08)
    p.basis_apr[5, 0] = 0.15
    out = compute_factor("basis_apr", p)
    assert out[5, 0] == pytest.approx(0.15)


def test_basis_z_30d_normalizes_basis_over_window() -> None:
    T = 800
    p = _panel(T, basis_value=0.05)
    p.basis_apr[T - 1, 0] = 0.50  # big spike
    out = compute_factor("basis_z_30d", p)
    assert out[T - 1, 0] > 5.0
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/factors/test_basis.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/factors/basis.py`**

```python
"""Basis (perp vs spot annualized) factors."""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor


@register_factor(
    id="basis_apr", category="basis",
    description="Perp vs spot annualized basis (high = contango → short perp)",
    direction="long_low", required_data=("basis_apr",),
    min_history_bars=1, rebalance_minutes=240,
)
def basis_apr(panel: FactorPanel) -> np.ndarray:
    assert panel.basis_apr is not None
    return panel.basis_apr.copy()


@register_factor(
    id="basis_z_30d", category="basis",
    description="basis_apr z-score over trailing 30 days",
    direction="long_low", required_data=("basis_apr",),
    min_history_bars=720, rebalance_minutes=240,
)
def basis_z_30d(panel: FactorPanel) -> np.ndarray:
    assert panel.basis_apr is not None
    ba = panel.basis_apr
    T = ba.shape[0]
    out = np.full_like(ba, np.nan, dtype=float)
    window = 720
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = ba[t - window + 1 : t + 1]
        mu = np.nanmean(slice_, axis=0)
        sd = np.nanstd(slice_, axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(sd > 0, (ba[t] - mu) / sd, np.nan)
    return out


__all__ = ["basis_apr", "basis_z_30d"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/factors/test_basis.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/factors/basis.py tests/unit/research/factors/test_basis.py
git commit -m "add(research/factors): basis_apr + basis_z_30d"
```

---

## Task 9: Volatility factors (3 factors)

**Files:**
- Create: `src/okx_trade/research/factors/volatility.py`
- Create: `tests/unit/research/factors/test_volatility.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/factors/test_volatility.py`:

```python
"""Tests for volatility factors."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    import okx_trade.research.factors.volatility as m
    importlib.reload(m)
    yield
    clear_registry()


def _vol_panel(T: int, sigma: float = 0.01, seed: int = 0) -> FactorPanel:
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(0, sigma, size=(T, 2))
    prices = 100.0 * np.exp(np.cumsum(log_rets, axis=0))
    return FactorPanel(
        inst_ids=("A", "B"), timestamps_ms=tuple(range(T)),
        close=prices, volume_usdt=np.ones((T, 2)) * 1e6,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_rv_pct_365d_yields_value_in_unit_interval() -> None:
    p = _vol_panel(365 * 24 + 200)
    out = compute_factor("rv_pct_365d", p)
    last = out[-1]
    assert np.all((last >= 0.0) & (last <= 1.0))


def test_rv_skew_up_down_constant_price_is_nan() -> None:
    T = 800
    p = FactorPanel(
        inst_ids=("A",), timestamps_ms=tuple(range(T)),
        close=np.ones((T, 1)) * 100.0, volume_usdt=np.ones((T, 1)),
        funding_rate=None, open_interest=None, basis_apr=None,
    )
    out = compute_factor("rv_skew_up_down", p)
    assert np.isnan(out[-1, 0])


def test_vol_of_vol_30d_is_nonneg() -> None:
    p = _vol_panel(1000)
    out = compute_factor("vol_of_vol_30d", p)
    last = out[-1]
    assert np.all(np.isnan(last) | (last >= 0.0))
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/factors/test_volatility.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/factors/volatility.py`**

```python
"""Volatility factors."""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor
from .momentum import _rolling_std


def _log_returns(close: np.ndarray) -> np.ndarray:
    """Bar-to-bar log returns, NaN in row 0."""
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(close[1:] / close[:-1])
    return np.vstack([np.full((1, close.shape[1]), np.nan), lr])


@register_factor(
    id="rv_pct_365d", category="volatility",
    description="Trailing 30d RV as a percentile within trailing 365d RV history",
    direction="long_low", required_data=("close",),
    min_history_bars=365 * 24 + 30 * 24, rebalance_minutes=240,
)
def rv_pct_365d(panel: FactorPanel) -> np.ndarray:
    log_ret = _log_returns(panel.close)
    rv = _rolling_std(log_ret, 30 * 24)  # 30-day RV per bar
    T, N = rv.shape
    out = np.full_like(rv, np.nan, dtype=float)
    window = 365 * 24
    if T < window:
        return out
    for t in range(window - 1, T):
        history = rv[t - window + 1 : t + 1]
        current = rv[t]
        for col in range(N):
            col_hist = history[:, col]
            col_hist = col_hist[~np.isnan(col_hist)]
            if col_hist.size < 5 or np.isnan(current[col]):
                continue
            out[t, col] = float(np.sum(col_hist <= current[col]) / col_hist.size)
    return out


@register_factor(
    id="rv_skew_up_down", category="volatility",
    description="(up_day_rv - down_day_rv) / total_rv over trailing 30d",
    direction="long_high", required_data=("close",),
    min_history_bars=30 * 24, rebalance_minutes=240,
)
def rv_skew_up_down(panel: FactorPanel) -> np.ndarray:
    log_ret = _log_returns(panel.close)
    T, N = log_ret.shape
    out = np.full((T, N), np.nan, dtype=float)
    window = 30 * 24
    if T < window:
        return out
    for t in range(window - 1, T):
        slice_ = log_ret[t - window + 1 : t + 1]
        up = np.where(slice_ > 0, slice_, np.nan)
        dn = np.where(slice_ < 0, slice_, np.nan)
        rv_up = np.sqrt(np.nanmean(up ** 2, axis=0))
        rv_dn = np.sqrt(np.nanmean(dn ** 2, axis=0))
        total = rv_up + rv_dn
        with np.errstate(divide="ignore", invalid="ignore"):
            out[t] = np.where(total > 0, (rv_up - rv_dn) / total, np.nan)
    return out


@register_factor(
    id="vol_of_vol_30d", category="volatility",
    description="Stdev of daily RV over trailing 30 days",
    direction="long_low", required_data=("close",),
    min_history_bars=60 * 24, rebalance_minutes=240,
)
def vol_of_vol_30d(panel: FactorPanel) -> np.ndarray:
    log_ret = _log_returns(panel.close)
    # Daily RV: 24-bar rolling stdev
    daily_rv = _rolling_std(log_ret, 24)
    return _rolling_std(daily_rv, 30 * 24)


__all__ = ["rv_pct_365d", "rv_skew_up_down", "vol_of_vol_30d"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/factors/test_volatility.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/factors/volatility.py \
        tests/unit/research/factors/test_volatility.py
git commit -m "add(research/factors): rv_pct + rv_skew + vol_of_vol"
```

---

## Task 10: Flow factors (2 factors) + factors registry auto-import

**Files:**
- Create: `src/okx_trade/research/factors/flow.py`
- Create: `tests/unit/research/factors/test_flow.py`
- Modify: `src/okx_trade/research/factors/__init__.py` (auto-import all factor modules)
- Modify: `src/okx_trade/research/__init__.py` (trigger factor registration on package import)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/factors/test_flow.py`:

```python
"""Tests for flow factors (spread_avg + taker_buy_ratio).

Note: these factors rely on data not in the base FactorPanel — they require
optional ``spread_bps`` and ``taker_buy_ratio`` panel fields. Tests cover
the proxy implementations that use ``close + volume_usdt`` heuristics.
"""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.compute import compute_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    import okx_trade.research.factors.flow as m
    importlib.reload(m)
    yield
    clear_registry()


def _panel(T: int = 50) -> FactorPanel:
    return FactorPanel(
        inst_ids=("A", "B"), timestamps_ms=tuple(range(T)),
        close=np.ones((T, 2)) * 100.0,
        volume_usdt=np.ones((T, 2)) * 1e6,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_spread_avg_1d_proxy_returns_nan_when_no_intraday_range() -> None:
    # Constant close → no high/low range → proxy returns NaN
    out = compute_factor("spread_avg_1d", _panel(50))
    assert np.all(np.isnan(out[-1]))


def test_taker_buy_ratio_1d_proxy_neutral_when_no_signed_data() -> None:
    # Without signed volume in panel, proxy returns NaN (no fabrication)
    out = compute_factor("taker_buy_ratio_1d", _panel(50))
    assert np.all(np.isnan(out[-1]))
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/factors/test_flow.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/factors/flow.py`**

These two factors are placeholders for v1 — when the panel gains signed-volume / spread
columns in P2 they get real bodies. v1 returns NaN to signal "no data" rather than
fabricating values.

```python
"""Flow factors (spread proxy + taker buy ratio proxy).

v1 returns NaN because OKX candle/REST data does not expose bid-ask spread or signed
volume at bar resolution. P2 will add these via WS persistence; until then these factor
ids exist so the registry is complete but their grade always falls below the threshold.
"""
from __future__ import annotations

import numpy as np

from ..panel import FactorPanel
from ..registry import register_factor


@register_factor(
    id="spread_avg_1d", category="flow",
    description="Trailing 24h avg bid-ask spread in bps (v1: NaN — needs WS persistence)",
    direction="long_low", required_data=("close",),
    min_history_bars=24, rebalance_minutes=240,
)
def spread_avg_1d(panel: FactorPanel) -> np.ndarray:
    return np.full_like(panel.close, np.nan, dtype=float)


@register_factor(
    id="taker_buy_ratio_1d", category="flow",
    description="Aggressor buy / total volume (v1: NaN — needs WS persistence)",
    direction="long_high", required_data=("volume_usdt",),
    min_history_bars=24, rebalance_minutes=240,
)
def taker_buy_ratio_1d(panel: FactorPanel) -> np.ndarray:
    return np.full_like(panel.volume_usdt, np.nan, dtype=float)


__all__ = ["spread_avg_1d", "taker_buy_ratio_1d"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/factors/test_flow.py -v`
Expected: 2 passed.

- [ ] **Step 5: Wire auto-registration in `factors/__init__.py`**

Replace empty `src/okx_trade/research/factors/__init__.py` with:

```python
"""Importing this package triggers @register_factor side effects for all built-in factors."""
from __future__ import annotations

from . import basis, flow, funding_oi, momentum, volatility  # noqa: F401

__all__ = ["basis", "flow", "funding_oi", "momentum", "volatility"]
```

Replace empty `src/okx_trade/research/__init__.py` with:

```python
"""okx_trade.research — factor research lab (offline pipeline + registry).

Importing this package loads all built-in factor modules and populates the registry.
"""
from __future__ import annotations

from . import factors  # noqa: F401 — triggers factor registration

__all__ = ["factors"]
```

- [ ] **Step 6: Write & run integration test confirming all 15 factors registered**

Append to `tests/unit/research/test_registry.py`:

```python
def test_all_builtin_factors_register_on_package_import() -> None:
    clear_registry()
    import importlib
    import okx_trade.research
    importlib.reload(okx_trade.research)
    ids = {s.id for s in list_factors()}
    expected = {
        "momentum_1d", "momentum_3d", "momentum_7d", "momentum_risk_adj_7d",
        "funding_current", "funding_z_30d", "oi_change_1d", "oi_to_volume_ratio",
        "basis_apr", "basis_z_30d",
        "rv_pct_365d", "rv_skew_up_down", "vol_of_vol_30d",
        "spread_avg_1d", "taker_buy_ratio_1d",
    }
    assert ids == expected
```

Run: `pytest tests/unit/research/ -v`
Expected: All previous + new test pass. 15 factors registered.

- [ ] **Step 7: Commit**

```bash
git add src/okx_trade/research/factors/flow.py \
        src/okx_trade/research/factors/__init__.py \
        src/okx_trade/research/__init__.py \
        tests/unit/research/factors/test_flow.py \
        tests/unit/research/test_registry.py
git commit -m "add(research/factors): flow placeholders + package auto-registration"
```

---

## Task 11: `research/store.py` — sqlite for factor metadata + grade history

**Files:**
- Create: `src/okx_trade/research/store.py`
- Create: `tests/unit/research/test_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_store.py`:

```python
"""Tests for the factor sqlite store."""
from __future__ import annotations

from pathlib import Path

import pytest

from okx_trade.research.store import FactorStore, GradeRecord


@pytest.fixture
def store(tmp_path: Path) -> FactorStore:
    return FactorStore(tmp_path / "zoo.db")


def test_store_creates_schema_on_first_use(store: FactorStore) -> None:
    store.init_schema()
    # Should be idempotent
    store.init_schema()
    assert store.list_approved() == []


def test_upsert_factor_then_list(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(
        id="momentum_7d", category="momentum",
        direction="long_high", description="7d momentum",
    )
    rows = store.list_factors()
    assert len(rows) == 1
    assert rows[0]["id"] == "momentum_7d"
    assert rows[0]["approved"] == 0


def test_save_grade_appends_row(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(id="f", category="t", direction="long_high", description="")
    rec = GradeRecord(
        factor_id="f", panel_start_ms=1, panel_end_ms=2, horizon_bars=24,
        ic_mean=0.04, ic_std=0.08, ir=0.5, ic_t_stat=3.2, ic_positive_rate=0.6,
        turnover_avg=0.2, autocorr_1=0.4,
        long_short_spread=0.001, net_ls_spread_after_fees=0.0005,
        n_periods=100, n_instruments=10, verdict="pass",
        graded_at_ms=1_700_000_000_000,
    )
    store.save_grade(rec)
    history = store.grade_history("f")
    assert len(history) == 1
    assert history[0]["verdict"] == "pass"


def test_approve_writes_weight_and_timestamp(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(id="f", category="t", direction="long_high", description="")
    store.approve("f", weight=0.25, ts_ms=1_700_000_000_000)
    approved = store.list_approved()
    assert len(approved) == 1
    assert approved[0]["id"] == "f"
    assert approved[0]["approved_weight"] == pytest.approx(0.25)


def test_reject_clears_approval(store: FactorStore) -> None:
    store.init_schema()
    store.upsert_factor(id="f", category="t", direction="long_high", description="")
    store.approve("f", weight=0.25, ts_ms=1)
    store.reject("f")
    assert store.list_approved() == []
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_store.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/store.py`**

```python
"""SQLite-backed factor zoo: metadata + grade history + approval state."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class GradeRecord:
    factor_id: str
    panel_start_ms: int
    panel_end_ms: int
    horizon_bars: int
    ic_mean: float
    ic_std: float
    ir: float
    ic_t_stat: float
    ic_positive_rate: float
    turnover_avg: float
    autocorr_1: float
    long_short_spread: float
    net_ls_spread_after_fees: float
    n_periods: int
    n_instruments: int
    verdict: str  # "pass" | "fail"
    graded_at_ms: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS factors (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  direction TEXT NOT NULL,
  description TEXT,
  approved INTEGER NOT NULL DEFAULT 0,
  approved_weight REAL,
  approved_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS grade_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factor_id TEXT NOT NULL REFERENCES factors(id),
  panel_start_ms INTEGER NOT NULL,
  panel_end_ms INTEGER NOT NULL,
  horizon_bars INTEGER NOT NULL,
  ic_mean REAL, ic_std REAL, ir REAL, ic_t_stat REAL, ic_positive_rate REAL,
  turnover_avg REAL, autocorr_1 REAL,
  long_short_spread REAL, net_ls_spread_after_fees REAL,
  n_periods INTEGER, n_instruments INTEGER,
  verdict TEXT NOT NULL,
  graded_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grade_runs_factor
  ON grade_runs(factor_id, graded_at_ms DESC);
"""


class FactorStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def upsert_factor(
        self, *, id: str, category: str, direction: str, description: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO factors(id, category, direction, description) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "category=excluded.category, direction=excluded.direction, "
                "description=excluded.description",
                (id, category, direction, description),
            )

    def list_factors(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, category, direction, description, approved, "
                "approved_weight, approved_at_ms FROM factors ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_approved(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, category, direction, approved_weight, approved_at_ms "
                "FROM factors WHERE approved=1 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def save_grade(self, rec: GradeRecord) -> None:
        d = asdict(rec)
        cols = ", ".join(d.keys())
        placeholders = ", ".join("?" for _ in d)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO grade_runs({cols}) VALUES({placeholders})",
                tuple(d.values()),
            )

    def grade_history(self, factor_id: str, *, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM grade_runs WHERE factor_id=? "
                "ORDER BY graded_at_ms DESC LIMIT ?",
                (factor_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_grade(self, factor_id: str) -> dict | None:
        rows = self.grade_history(factor_id, limit=1)
        return rows[0] if rows else None

    def approve(self, factor_id: str, *, weight: float, ts_ms: int) -> None:
        with self._conn() as c:
            updated = c.execute(
                "UPDATE factors SET approved=1, approved_weight=?, approved_at_ms=? "
                "WHERE id=?",
                (weight, ts_ms, factor_id),
            ).rowcount
        if updated == 0:
            raise KeyError(f"factor {factor_id!r} not in store; upsert first")

    def reject(self, factor_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE factors SET approved=0, approved_weight=NULL, approved_at_ms=NULL "
                "WHERE id=?",
                (factor_id,),
            )


__all__ = ["FactorStore", "GradeRecord"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_store.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/store.py tests/unit/research/test_store.py
git commit -m "add(research): sqlite FactorStore for metadata + grade history"
```

---

## Task 12: `research/grade.py` — IC / IR / decay / turnover / PnL

**Files:**
- Create: `src/okx_trade/research/grade.py`
- Create: `tests/unit/research/test_grade.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_grade.py`:

```python
"""Tests for grade_factor: synthetic panel where ground truth is known."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.grade import GradeThresholds, grade_factor
from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    yield
    clear_registry()


def _make_panel(T: int, N: int, *, perfect_predictor: bool, seed: int = 0):
    """Build a panel where future close[t+1] = close[t] * (1 + signal[t]).

    If perfect_predictor=True, panel.volume_usdt[t] == signal[t]; we register a factor
    that returns volume_usdt to get IC ≈ 1.0. Otherwise random.
    """
    rng = np.random.default_rng(seed)
    signal = rng.normal(0, 0.01, size=(T, N))
    close = np.ones((T, N)) * 100.0
    for t in range(1, T):
        close[t] = close[t - 1] * (1.0 + signal[t - 1])
    if perfect_predictor:
        vol = signal.copy()
    else:
        vol = rng.normal(0, 0.01, size=(T, N))
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)), timestamps_ms=tuple(range(T)),
        close=close, volume_usdt=vol,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_perfect_predictor_yields_ic_near_one() -> None:
    @register_factor(id="oracle", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _make_panel(T=200, N=10, perfect_predictor=True)
    g = grade_factor("oracle", panel, horizon_bars=1)
    assert g.ic_mean > 0.9
    assert g.verdict == "pass"


def test_random_factor_yields_ic_near_zero_and_fails() -> None:
    @register_factor(id="noise", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _make_panel(T=200, N=10, perfect_predictor=False, seed=42)
    g = grade_factor("noise", panel, horizon_bars=1)
    assert abs(g.ic_mean) < 0.15
    assert g.verdict == "fail"


def test_long_low_direction_flips_score() -> None:
    """A long_low factor whose values negatively correlate with fwd-ret should pass."""
    @register_factor(id="inverted_oracle", category="t", description="",
                     direction="long_low", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return -p.volume_usdt.copy()  # negate the perfect signal

    panel = _make_panel(T=200, N=10, perfect_predictor=True)
    g = grade_factor("inverted_oracle", panel, horizon_bars=1)
    # After direction flip, IC mean should still be near +1
    assert g.ic_mean > 0.9


def test_custom_thresholds_override_default() -> None:
    @register_factor(id="weak", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p):
        # Mild predictor: 30% true signal + 70% noise
        rng = np.random.default_rng(1)
        return 0.3 * p.volume_usdt + 0.7 * rng.normal(0, 0.01, p.volume_usdt.shape)

    panel = _make_panel(T=500, N=20, perfect_predictor=True, seed=1)
    g_strict = grade_factor("weak", panel, horizon_bars=1,
                            thresholds=GradeThresholds(ic_t_stat=10.0, ir=2.0,
                                                       ic_positive_rate=0.95,
                                                       net_after_fees=0.01,
                                                       autocorr_1=0.9))
    assert g_strict.verdict == "fail"

    g_loose = grade_factor("weak", panel, horizon_bars=1,
                           thresholds=GradeThresholds(ic_t_stat=0.0, ir=-1.0,
                                                      ic_positive_rate=0.0,
                                                      net_after_fees=-1.0,
                                                      autocorr_1=-1.0))
    assert g_loose.verdict == "pass"


def test_decay_returns_six_horizons() -> None:
    @register_factor(id="oracle2", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _make_panel(T=300, N=10, perfect_predictor=True)
    g = grade_factor("oracle2", panel, horizon_bars=1)
    assert len(g.ic_decay) == 6
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_grade.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/grade.py`**

```python
"""Factor evaluation: cross-sectional IC + decay + turnover + L/S spread (net of fees)."""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .compute import compute_factor
from .panel import FactorPanel
from .registry import get_factor

_DECAY_HORIZONS = (1, 2, 4, 8, 16, 32)
_FEE_BPS_PER_LEG = 5.0  # OKX taker, round-trip = 2 legs


@dataclass(frozen=True, slots=True)
class GradeThresholds:
    ic_t_stat: float = 2.0
    ir: float = 0.3
    ic_positive_rate: float = 0.55
    net_after_fees: float = 0.0  # gross > 0 after fee deduction
    autocorr_1: float = 0.3


@dataclass(frozen=True, slots=True)
class FactorGrade:
    factor_id: str
    panel_start_ms: int
    panel_end_ms: int
    horizon_bars: int
    ic_mean: float
    ic_std: float
    ir: float
    ic_t_stat: float
    ic_positive_rate: float
    ic_decay: list[float]
    turnover_avg: float
    autocorr_1: float
    long_short_spread: float
    net_ls_spread_after_fees: float
    n_periods: int
    n_instruments: int
    verdict: str   # "pass" | "fail"
    graded_at_ms: int


def _spearman_row(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation; ignores NaN-paired entries. Returns 0.0 if degenerate."""
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 3:
        return 0.0
    ra = _rankdata(a[mask])
    rb = _rankdata(b[mask])
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    if denom == 0:
        return 0.0
    return float((ra * rb).sum() / denom)


def _rankdata(a: np.ndarray) -> np.ndarray:
    """Average-rank tie handling (matches scipy.stats.rankdata default)."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(a) + 1)
    # Tie correction
    sa = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2.0
            ranks[order[i : j + 1]] = avg
        i = j + 1
    return ranks


def _apply_direction(values: np.ndarray, direction: str) -> np.ndarray:
    return -values if direction == "long_low" else values


def grade_factor(
    factor_id: str,
    panel: FactorPanel,
    *,
    horizon_bars: int,
    top_k: int = 5,
    thresholds: GradeThresholds | None = None,
) -> FactorGrade:
    """Compute IC / decay / turnover / L-S spread for one factor on one panel.

    The factor's ``direction`` (long_high vs long_low) is applied here: long_low
    factors are negated before IC so the metrics always read "high score = expected
    long".
    """
    thresholds = thresholds or GradeThresholds()
    spec = get_factor(factor_id)
    raw = compute_factor(factor_id, panel)
    scored = _apply_direction(raw, spec.direction)

    close = panel.close
    T, N = close.shape

    # Forward returns at evaluation horizon
    fwd_ret = np.full_like(close, np.nan, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        fwd_ret[:-horizon_bars] = close[horizon_bars:] / close[:-horizon_bars] - 1.0

    # IC per row from min_history_bars to T-horizon
    start = max(spec.min_history_bars, 1)
    ic_series: list[float] = []
    top_sets: list[set[int]] = []
    bot_sets: list[set[int]] = []
    ls_returns: list[float] = []
    for t in range(start, T - horizon_bars):
        s = scored[t]
        r = fwd_ret[t]
        if np.all(np.isnan(s)) or np.all(np.isnan(r)):
            continue
        ic = _spearman_row(s, r)
        ic_series.append(ic)
        # top/bot sets for turnover + L/S spread
        valid = np.where(~(np.isnan(s) | np.isnan(r)))[0]
        if len(valid) < 2 * top_k:
            top_sets.append(set()); bot_sets.append(set())
            ls_returns.append(0.0)
            continue
        order = valid[np.argsort(s[valid])]  # ascending
        bot = set(order[:top_k].tolist())
        top = set(order[-top_k:].tolist())
        top_sets.append(top); bot_sets.append(bot)
        ls = np.nanmean(r[list(top)]) - np.nanmean(r[list(bot)])
        ls_returns.append(float(ls))

    ic_arr = np.asarray(ic_series, dtype=float)
    n_periods = len(ic_arr)
    if n_periods == 0:
        return _empty_grade(factor_id, panel, horizon_bars)
    ic_mean = float(ic_arr.mean())
    ic_std = float(ic_arr.std(ddof=1)) if n_periods > 1 else 0.0
    ir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_t_stat = ic_mean * np.sqrt(n_periods) / ic_std if ic_std > 0 else 0.0
    ic_positive_rate = float((ic_arr > 0).mean())

    # Decay
    decay = []
    for h in _DECAY_HORIZONS:
        if T - h <= start:
            decay.append(float("nan"))
            continue
        fwd_h = np.full_like(close, np.nan)
        fwd_h[:-h] = close[h:] / close[:-h] - 1.0
        ics = []
        for t in range(start, T - h):
            ics.append(_spearman_row(scored[t], fwd_h[t]))
        decay.append(float(np.mean(ics)) if ics else float("nan"))

    # Turnover (average per period; |S_t \ S_{t-1}| / k)
    if len(top_sets) >= 2 and top_k > 0:
        turns: list[float] = []
        for prev, cur in zip(top_sets[:-1], top_sets[1:]):
            if not cur:
                continue
            turns.append(len(cur - prev) / top_k)
        for prev, cur in zip(bot_sets[:-1], bot_sets[1:]):
            if not cur:
                continue
            turns.append(len(cur - prev) / top_k)
        turnover_avg = float(np.mean(turns)) if turns else 0.0
    else:
        turnover_avg = 0.0

    # Autocorr of raw factor (lag-1, cross-instrument-pooled, NaN-safe)
    autocorr_1 = _pooled_autocorr1(scored)

    # PnL
    ls_arr = np.asarray(ls_returns, dtype=float)
    long_short_spread = float(np.nanmean(ls_arr)) if ls_arr.size else 0.0
    # Net: subtract fee × 2 legs × turnover per period
    fee_pct = (_FEE_BPS_PER_LEG * 2 / 10_000.0) * turnover_avg
    net = long_short_spread - fee_pct

    verdict = (
        "pass" if (
            ic_t_stat >= thresholds.ic_t_stat
            and ir >= thresholds.ir
            and ic_positive_rate >= thresholds.ic_positive_rate
            and net >= thresholds.net_after_fees
            and autocorr_1 >= thresholds.autocorr_1
        ) else "fail"
    )

    return FactorGrade(
        factor_id=factor_id,
        panel_start_ms=panel.timestamps_ms[0],
        panel_end_ms=panel.timestamps_ms[-1],
        horizon_bars=horizon_bars,
        ic_mean=ic_mean, ic_std=ic_std, ir=ir,
        ic_t_stat=ic_t_stat, ic_positive_rate=ic_positive_rate,
        ic_decay=decay,
        turnover_avg=turnover_avg, autocorr_1=autocorr_1,
        long_short_spread=long_short_spread,
        net_ls_spread_after_fees=net,
        n_periods=n_periods, n_instruments=N,
        verdict=verdict, graded_at_ms=int(time.time() * 1000),
    )


def _pooled_autocorr1(arr: np.ndarray) -> float:
    """Cross-section-pooled lag-1 autocorr; NaN-safe; returns 0.0 if degenerate."""
    if arr.shape[0] < 2:
        return 0.0
    a = arr[:-1].ravel()
    b = arr[1:].ravel()
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 3:
        return 0.0
    a = a[mask] - a[mask].mean()
    b = b[mask] - b[mask].mean()
    denom = np.sqrt((a ** 2).sum() * (b ** 2).sum())
    if denom == 0:
        return 0.0
    return float((a * b).sum() / denom)


def _empty_grade(factor_id: str, panel: FactorPanel, horizon_bars: int) -> FactorGrade:
    return FactorGrade(
        factor_id=factor_id,
        panel_start_ms=panel.timestamps_ms[0],
        panel_end_ms=panel.timestamps_ms[-1],
        horizon_bars=horizon_bars,
        ic_mean=0.0, ic_std=0.0, ir=0.0,
        ic_t_stat=0.0, ic_positive_rate=0.0,
        ic_decay=[float("nan")] * len(_DECAY_HORIZONS),
        turnover_avg=0.0, autocorr_1=0.0,
        long_short_spread=0.0, net_ls_spread_after_fees=0.0,
        n_periods=0, n_instruments=panel.n,
        verdict="fail", graded_at_ms=int(time.time() * 1000),
    )


__all__ = ["FactorGrade", "GradeThresholds", "grade_factor"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_grade.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/grade.py tests/unit/research/test_grade.py
git commit -m "add(research): grade_factor with IC/IR/decay/turnover/net-PnL"
```

---

## Task 13: `research/data.py` — `fetch_panel` + parquet cache

**Files:**
- Create: `src/okx_trade/research/data.py`
- Create: `tests/unit/research/test_data.py`

This task pulls historical candles + funding + OI from REST and assembles a `FactorPanel`,
caching the result as parquet.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_data.py`:

```python
"""Tests for research/data.py fetch_panel."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from okx_trade.models.common import FundingRate
from okx_trade.models.market import Candle, OpenInterestPoint
from okx_trade.research.data import fetch_panel


@dataclass
class _StubMarket:
    """Stubs get_candles_extended."""
    candles_by_inst: dict[str, list[Candle]]

    async def get_candles_extended(self, inst_id, bar, *, total):
        return self.candles_by_inst.get(inst_id, [])


@dataclass
class _StubPublic:
    """Stubs funding + OI history."""
    funding_by_inst: dict[str, list[FundingRate]]
    oi_by_inst: dict[str, list[OpenInterestPoint]]

    async def get_funding_rate_history_extended(self, inst_id, *, total):
        return self.funding_by_inst.get(inst_id, [])

    async def get_open_interest_history_extended(self, inst_id, *, period, total):
        return self.oi_by_inst.get(inst_id, [])


@dataclass
class _StubRest:
    market: _StubMarket
    public: _StubPublic


def _candle(ts: int, close: float, vol_usdt: float) -> Candle:
    return Candle(
        ts=ts, open=Decimal("100"), high=Decimal("100"),
        low=Decimal("100"), close=Decimal(str(close)),
        volume=Decimal("1"), volume_ccy=Decimal("1"),
        volume_ccy_quote=Decimal(str(vol_usdt)), confirm=True,
    )


@pytest.mark.asyncio
async def test_fetch_panel_assembles_close_and_volume(tmp_path: Path) -> None:
    candles = {
        "BTC-USDT-SWAP": [_candle(1000, 100.0, 1e6), _candle(2000, 110.0, 1.1e6)],
        "ETH-USDT-SWAP": [_candle(1000, 2.0, 5e5), _candle(2000, 2.1, 5.5e5)],
    }
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    panel = await fetch_panel(
        rest_client=rest,  # type: ignore[arg-type]
        inst_ids=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        start_ms=1000, end_ms=2000,
        bar="1H",
        include=("close", "volume_usdt"),
        cache_dir=tmp_path,
    )
    assert panel.inst_ids == ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    assert panel.timestamps_ms == (1000, 2000)
    assert panel.close[1, 0] == pytest.approx(110.0)
    assert panel.close[1, 1] == pytest.approx(2.1)


@pytest.mark.asyncio
async def test_fetch_panel_caches_to_parquet_and_reuses(tmp_path: Path) -> None:
    candles = {"BTC-USDT-SWAP": [_candle(1000, 100.0, 1e6)]}
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    p1 = await fetch_panel(rest_client=rest, inst_ids=["BTC-USDT-SWAP"],  # type: ignore[arg-type]
                            start_ms=1000, end_ms=1000, bar="1H",
                            include=("close", "volume_usdt"), cache_dir=tmp_path)
    # Cache file exists
    cache_files = list(tmp_path.glob("*.parquet"))
    assert len(cache_files) == 1
    # Second call with empty stub: should still return same data via cache
    rest_empty = _StubRest(
        market=_StubMarket(candles_by_inst={}),
        public=_StubPublic(funding_by_inst={}, oi_by_inst={}),
    )
    p2 = await fetch_panel(rest_client=rest_empty, inst_ids=["BTC-USDT-SWAP"],  # type: ignore[arg-type]
                            start_ms=1000, end_ms=1000, bar="1H",
                            include=("close", "volume_usdt"), cache_dir=tmp_path)
    np.testing.assert_array_equal(p1.close, p2.close)


@pytest.mark.asyncio
async def test_fetch_panel_includes_funding_when_requested(tmp_path: Path) -> None:
    candles = {"BTC-USDT-SWAP": [_candle(1000, 100.0, 1e6), _candle(2000, 100.0, 1e6)]}
    funding = {"BTC-USDT-SWAP": [
        FundingRate(instType="SWAP", instId="BTC-USDT-SWAP",
                    fundingRate="0.0001", fundingTime=1000),
        FundingRate(instType="SWAP", instId="BTC-USDT-SWAP",
                    fundingRate="0.0002", fundingTime=2000),
    ]}
    rest = _StubRest(
        market=_StubMarket(candles_by_inst=candles),
        public=_StubPublic(funding_by_inst=funding, oi_by_inst={}),
    )
    panel = await fetch_panel(rest_client=rest, inst_ids=["BTC-USDT-SWAP"],  # type: ignore[arg-type]
                              start_ms=1000, end_ms=2000, bar="1H",
                              include=("close", "volume_usdt", "funding_rate"),
                              cache_dir=tmp_path)
    assert panel.funding_rate is not None
    assert panel.funding_rate[0, 0] == pytest.approx(0.0001)
    assert panel.funding_rate[1, 0] == pytest.approx(0.0002)
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_data.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/data.py`**

```python
"""fetch_panel: concurrent REST pull + parquet cache → FactorPanel.

Caching: results are keyed by SHA1 of (sorted inst_ids + bar + start + end + include).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np

from .panel import FactorPanel, panel_from_dicts


class _RestClient(Protocol):
    market: object
    public: object


_FUNDING_BARS_NEEDED = lambda T: max(T // 8 + 5, 100)  # 1H bars → 8h funding cycles
_OI_PERIOD = "1H"


def _cache_key(inst_ids: Iterable[str], bar: str, start_ms: int, end_ms: int,
               include: tuple[str, ...]) -> str:
    payload = json.dumps({
        "inst_ids": sorted(inst_ids), "bar": bar,
        "start": start_ms, "end": end_ms, "include": sorted(include),
    }, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _load_cache(cache_path: Path) -> FactorPanel | None:
    if not cache_path.exists():
        return None
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return None
    table = pq.read_table(cache_path)
    meta = json.loads(table.schema.metadata[b"panel_meta"].decode("utf-8"))
    inst_ids = tuple(meta["inst_ids"])
    timestamps = tuple(meta["timestamps"])
    T, N = len(timestamps), len(inst_ids)
    arrays: dict[str, np.ndarray | None] = {}
    for name in ("close", "volume_usdt", "funding_rate", "open_interest", "basis_apr"):
        col = f"{name}_flat"
        if col in table.column_names:
            arrays[name] = np.asarray(table[col].to_pylist(), dtype=float).reshape(T, N)
        else:
            arrays[name] = None
    return FactorPanel(
        inst_ids=inst_ids, timestamps_ms=timestamps,
        close=arrays["close"], volume_usdt=arrays["volume_usdt"],
        funding_rate=arrays["funding_rate"],
        open_interest=arrays["open_interest"], basis_apr=arrays["basis_apr"],
    )


def _save_cache(panel: FactorPanel, cache_path: Path) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return
    cols: dict[str, list] = {}
    for name in ("close", "volume_usdt", "funding_rate", "open_interest", "basis_apr"):
        arr = getattr(panel, name)
        if arr is not None:
            cols[f"{name}_flat"] = arr.flatten().tolist()
    table = pa.table(cols)
    meta = {
        "inst_ids": list(panel.inst_ids),
        "timestamps": list(panel.timestamps_ms),
    }
    table = table.replace_schema_metadata({b"panel_meta": json.dumps(meta).encode("utf-8")})
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, cache_path)


async def fetch_panel(
    *,
    rest_client: _RestClient,
    inst_ids: list[str],
    start_ms: int,
    end_ms: int,
    bar: str = "1H",
    include: tuple[str, ...] = ("close", "volume_usdt"),
    cache_dir: Path | None = None,
) -> FactorPanel:
    """Build a FactorPanel from OKX REST.

    ``cache_dir``: if provided, results are cached as parquet keyed by query params.
    """
    if cache_dir is not None:
        cache_path = cache_dir / f"panel_{_cache_key(inst_ids, bar, start_ms, end_ms, include)}.parquet"
        cached = _load_cache(cache_path)
        if cached is not None:
            return cached

    # Determine bar count required (used as ``total`` cap for the OKX paginator)
    bar_ms = _bar_ms(bar)
    if bar_ms <= 0:
        raise ValueError(f"unsupported bar: {bar!r}")
    n_bars = max(1, (end_ms - start_ms) // bar_ms + 1)

    async def _fetch_one(inst: str) -> tuple[str, dict[str, list[tuple[int, float]]]]:
        fields: dict[str, list[tuple[int, float]]] = {}
        candles = await rest_client.market.get_candles_extended(inst, bar, total=n_bars)
        ts_close: list[tuple[int, float]] = []
        ts_volusdt: list[tuple[int, float]] = []
        for c in candles:
            if c.ts < start_ms or c.ts > end_ms:
                continue
            ts_close.append((c.ts, float(c.close)))
            ts_volusdt.append((c.ts, float(c.volume_ccy_quote)))
        fields["close"] = ts_close
        fields["volume_usdt"] = ts_volusdt

        if "funding_rate" in include:
            frs = await rest_client.public.get_funding_rate_history_extended(
                inst, total=_FUNDING_BARS_NEEDED(n_bars),
            )
            fr_series = [(r.funding_time, float(r.funding_rate)) for r in frs
                         if start_ms <= r.funding_time <= end_ms]
            # Forward-fill funding rate to each bar timestamp (8h cycle, 1H bars)
            fields["funding_rate"] = _ffill_to_bars(fr_series, ts_close)

        if "open_interest" in include:
            oi_pts = await rest_client.public.get_open_interest_history_extended(
                inst, period=_OI_PERIOD, total=n_bars,
            )
            oi_series = [(p.ts, float(p.oi_ccy)) for p in oi_pts
                         if start_ms <= p.ts <= end_ms]
            fields["open_interest"] = oi_series

        return inst, fields

    results = await asyncio.gather(*(_fetch_one(i) for i in inst_ids))
    by_inst = dict(results)
    panel = panel_from_dicts(by_inst)

    if cache_dir is not None:
        cache_path = cache_dir / f"panel_{_cache_key(inst_ids, bar, start_ms, end_ms, include)}.parquet"
        _save_cache(panel, cache_path)

    return panel


def _bar_ms(bar: str) -> int:
    s = bar.strip().upper()
    if s.endswith("M"):  # 1m, 5m, 15m → OKX uses lowercase m for minutes
        try:
            return int(s[:-1]) * 60_000
        except ValueError:
            return 0
    if s.endswith("H"):
        return int(s[:-1]) * 3_600_000
    if s.endswith("D"):
        return int(s[:-1]) * 86_400_000
    return 0


def _ffill_to_bars(
    sparse: list[tuple[int, float]],
    bar_ts: list[tuple[int, float]],
) -> list[tuple[int, float]]:
    """Forward-fill the latest known sparse value to each bar timestamp."""
    if not sparse or not bar_ts:
        return []
    sparse = sorted(sparse)
    out: list[tuple[int, float]] = []
    j = 0
    last_val: float | None = None
    for ts, _ in bar_ts:
        while j < len(sparse) and sparse[j][0] <= ts:
            last_val = sparse[j][1]
            j += 1
        if last_val is not None:
            out.append((ts, last_val))
    return out


__all__ = ["fetch_panel"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_data.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/data.py tests/unit/research/test_data.py
git commit -m "add(research): fetch_panel with parquet cache (close/volume/funding/OI)"
```

---

## Task 14: `research/walk_forward_grade.py` — OOS rolling grade

**Files:**
- Create: `src/okx_trade/research/walk_forward_grade.py`
- Create: `tests/unit/research/test_walk_forward_grade.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_walk_forward_grade.py`:

```python
"""Tests for walk_forward_grade: rolling OOS IC for one factor."""
from __future__ import annotations

import numpy as np
import pytest

from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor
from okx_trade.research.walk_forward_grade import walk_forward_grade


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    yield
    clear_registry()


def _panel(T: int, N: int = 5, *, perfect: bool, seed: int = 0):
    rng = np.random.default_rng(seed)
    signal = rng.normal(0, 0.01, (T, N))
    close = np.ones((T, N)) * 100.0
    for t in range(1, T):
        close[t] = close[t - 1] * (1.0 + signal[t - 1])
    vol = signal.copy() if perfect else rng.normal(0, 0.01, (T, N))
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)), timestamps_ms=tuple(range(T)),
        close=close, volume_usdt=vol,
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_walk_forward_grade_returns_per_window_grades() -> None:
    @register_factor(id="oracle_wf", category="t", description="",
                     direction="long_high", required_data=("close", "volume_usdt"),
                     min_history_bars=0, rebalance_minutes=60)
    def f(p): return p.volume_usdt.copy()

    panel = _panel(T=600, perfect=True)
    grades = walk_forward_grade(
        "oracle_wf", panel, horizon_bars=1,
        train_window=200, test_window=100,
    )
    assert len(grades) == 4  # (600 - 200) / 100 = 4 windows
    # Perfect predictor should have high IC in every OOS test window
    assert all(g.ic_mean > 0.5 for g in grades)
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_walk_forward_grade.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/walk_forward_grade.py`**

```python
"""walk_forward_grade: roll a (train_window, test_window) split and grade each test slice.

For factor research we only need the OOS test slice — the factor itself doesn't have
trainable parameters in v1, so the "train" window is just the lookback that the factor
needs to warm up. Returns one FactorGrade per test window.
"""
from __future__ import annotations

import numpy as np

from .grade import FactorGrade, GradeThresholds, grade_factor
from .panel import FactorPanel


def walk_forward_grade(
    factor_id: str,
    panel: FactorPanel,
    *,
    horizon_bars: int,
    train_window: int,
    test_window: int,
    thresholds: GradeThresholds | None = None,
) -> list[FactorGrade]:
    """Rolling-OOS grades. Each window covers ``[start, start + train + test)``;
    the panel slice handed to ``grade_factor`` includes both train and test rows, but
    ``min_history_bars`` (from the factor's spec) ensures only the test segment
    contributes to IC since the warmup period yields NaN scores."""
    T = panel.t
    grades: list[FactorGrade] = []
    start = 0
    while start + train_window + test_window <= T:
        end = start + train_window + test_window
        sub = _slice_panel(panel, start, end)
        grades.append(grade_factor(
            factor_id, sub, horizon_bars=horizon_bars, thresholds=thresholds,
        ))
        start += test_window
    return grades


def _slice_panel(panel: FactorPanel, start: int, end: int) -> FactorPanel:
    def _maybe(arr: np.ndarray | None) -> np.ndarray | None:
        return None if arr is None else arr[start:end]

    return FactorPanel(
        inst_ids=panel.inst_ids,
        timestamps_ms=panel.timestamps_ms[start:end],
        close=panel.close[start:end],
        volume_usdt=panel.volume_usdt[start:end],
        funding_rate=_maybe(panel.funding_rate),
        open_interest=_maybe(panel.open_interest),
        basis_apr=_maybe(panel.basis_apr),
    )


__all__ = ["walk_forward_grade"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_walk_forward_grade.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/walk_forward_grade.py \
        tests/unit/research/test_walk_forward_grade.py
git commit -m "add(research): walk_forward_grade for OOS rolling factor IC"
```

---

## Task 15: `research/report.py` — markdown report generator

**Files:**
- Create: `src/okx_trade/research/report.py`
- Create: `tests/unit/research/test_report.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_report.py`:

```python
"""Tests for markdown report rendering."""
from __future__ import annotations

from okx_trade.research.grade import FactorGrade
from okx_trade.research.report import render_grade_report


def _fake_grade() -> FactorGrade:
    return FactorGrade(
        factor_id="momentum_7d",
        panel_start_ms=1_700_000_000_000,
        panel_end_ms=1_715_000_000_000,
        horizon_bars=24,
        ic_mean=0.045, ic_std=0.082, ir=0.549, ic_t_stat=4.31, ic_positive_rate=0.62,
        ic_decay=[0.05, 0.04, 0.03, 0.02, 0.01, 0.005],
        turnover_avg=0.22, autocorr_1=0.45,
        long_short_spread=0.0018, net_ls_spread_after_fees=0.0011,
        n_periods=4320, n_instruments=30,
        verdict="pass", graded_at_ms=1_716_000_000_000,
    )


def test_render_grade_report_includes_factor_id_and_verdict() -> None:
    md = render_grade_report(_fake_grade())
    assert "# Factor Grade: momentum_7d" in md
    assert "PASS" in md
    assert "ic_mean" in md and "0.045" in md
    # Decay table has 6 horizons
    assert "32" in md  # last decay bucket header


def test_render_grade_report_marks_fail() -> None:
    g = _fake_grade()
    g_fail = FactorGrade(**{**g.__dict__, "verdict": "fail"})
    md = render_grade_report(g_fail)
    assert "FAIL" in md
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_report.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/report.py`**

```python
"""Markdown report rendering for FactorGrade."""
from __future__ import annotations

from datetime import datetime, timezone

from .grade import FactorGrade


_DECAY_HORIZONS = (1, 2, 4, 8, 16, 32)


def render_grade_report(g: FactorGrade) -> str:
    """Render a FactorGrade as a self-contained markdown report."""
    def _iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    period_days = round((g.panel_end_ms - g.panel_start_ms) / 86_400_000)
    decay_header = " | ".join(str(h) for h in _DECAY_HORIZONS)
    decay_row = " | ".join(f"{x:.4f}" for x in g.ic_decay)
    verdict_badge = "PASS" if g.verdict == "pass" else "FAIL"

    return f"""# Factor Grade: {g.factor_id}

- Period: {_iso(g.panel_start_ms)} → {_iso(g.panel_end_ms)} ({period_days}d, {g.n_periods} periods × {g.n_instruments} inst)
- Horizon: {g.horizon_bars} bars
- Graded at: {_iso(g.graded_at_ms)}

## IC

| metric | value |
|---|---|
| ic_mean | {g.ic_mean:.4f} |
| ic_std | {g.ic_std:.4f} |
| ir | {g.ir:.4f} |
| t_stat | {g.ic_t_stat:.4f} |
| positive_rate | {g.ic_positive_rate:.4f} |

## Decay (IC by horizon, bars)

| {decay_header} |
|{('|'.join(['---'] * len(_DECAY_HORIZONS)))}|
| {decay_row} |

## Long-Short Spread (top-K vs bot-K)

| metric | value |
|---|---|
| gross (per period) | {g.long_short_spread:.6f} |
| net after fees | {g.net_ls_spread_after_fees:.6f} |
| turnover (avg) | {g.turnover_avg:.4f} |
| autocorr (lag-1) | {g.autocorr_1:.4f} |

## Verdict: {verdict_badge}
"""


__all__ = ["render_grade_report"]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_report.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/research/report.py tests/unit/research/test_report.py
git commit -m "add(research): markdown report renderer for FactorGrade"
```

---

## Task 16: `research/cli.py` + `__main__.py` — CLI entrypoints

**Files:**
- Create: `src/okx_trade/research/cli.py`
- Create: `src/okx_trade/research/__main__.py`
- Create: `tests/unit/research/test_cli.py`

The CLI wires together store + report + grade + approve. `fetch` and `backtest-portfolio`
subcommands are also defined here but their bodies delegate to the data layer / scripts.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/research/test_cli.py`:

```python
"""Tests for the CLI: cover argparse routing + approve writes yaml + sqlite."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from okx_trade.research.cli import build_parser, run
from okx_trade.research.store import FactorStore


def test_parser_recognizes_all_subcommands() -> None:
    p = build_parser()
    for cmd in ("list", "fetch", "eval", "grade-all", "approve", "reject",
                "backtest-portfolio", "report"):
        ns = p.parse_args([cmd, "--help"]) if False else None
        # Just confirm subparser exists by parsing the command with minimal args
        # (some require --factor; we only check the subparser registers)
        assert cmd in p._subparsers._group_actions[0].choices  # type: ignore[attr-defined]


def test_list_subcommand_prints_registered_factors(tmp_path, capsys) -> None:
    db = tmp_path / "z.db"
    yml = tmp_path / "p.yaml"
    rc = run(["list", "--db", str(db), "--yaml", str(yml)])
    assert rc == 0
    out = capsys.readouterr().out
    # Should list all 15 built-in factors
    assert "momentum_7d" in out
    assert "funding_z_30d" in out


def test_approve_writes_yaml_and_sqlite(tmp_path) -> None:
    db = tmp_path / "z.db"
    yml = tmp_path / "p.yaml"
    rc = run(["approve", "--factor", "momentum_7d", "--weight", "0.25",
              "--db", str(db), "--yaml", str(yml)])
    assert rc == 0
    store = FactorStore(db)
    approved = store.list_approved()
    assert len(approved) == 1 and approved[0]["id"] == "momentum_7d"
    cfg = yaml.safe_load(yml.read_text())
    assert any(f["id"] == "momentum_7d" and f["weight"] == 0.25 for f in cfg["factors"])


def test_reject_removes_factor_from_yaml(tmp_path) -> None:
    db = tmp_path / "z.db"; yml = tmp_path / "p.yaml"
    run(["approve", "--factor", "momentum_7d", "--weight", "0.25",
         "--db", str(db), "--yaml", str(yml)])
    rc = run(["reject", "--factor", "momentum_7d",
              "--db", str(db), "--yaml", str(yml)])
    assert rc == 0
    cfg = yaml.safe_load(yml.read_text())
    assert all(f["id"] != "momentum_7d" for f in cfg.get("factors", []))
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/research/test_cli.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/okx_trade/research/cli.py`**

```python
"""CLI for the factor research lab.

Entry: ``python -m okx_trade.research.factor <subcommand>`` (via __main__.py).

Subcommands route to small helper functions; expensive ones (fetch / eval / backtest)
return non-zero and print a clear error if asyncio + REST credentials aren't available
in the current environment, keeping CLI tests cheap.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Sequence

import yaml

from .registry import get_factor, list_factors
from .store import FactorStore

_DEFAULT_DB = Path("var/factor_research/factor_zoo.db")
_DEFAULT_YAML = Path("configs/factor_portfolio.yaml")
_DEFAULT_REPORT_DIR = Path("var/factor_research/reports")
_DEFAULT_PANEL_DIR = Path("var/factor_research/panel")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="okx_trade.research.factor")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--db", default=str(_DEFAULT_DB))
        sp.add_argument("--yaml", default=str(_DEFAULT_YAML))

    pl = sub.add_parser("list"); _common(pl)

    pf = sub.add_parser("fetch")
    pf.add_argument("--start", required=True, help="YYYY-MM-DD")
    pf.add_argument("--end", required=True)
    pf.add_argument("--universe", default="top30")
    pf.add_argument("--bar", default="1H")
    pf.add_argument("--cache-dir", default=str(_DEFAULT_PANEL_DIR))
    _common(pf)

    pe = sub.add_parser("eval")
    pe.add_argument("--factor", required=True)
    pe.add_argument("--horizon", default="1d", help="1d/4h/1h")
    pe.add_argument("--top-k", type=int, default=5)
    pe.add_argument("--panel-cache", default=str(_DEFAULT_PANEL_DIR))
    pe.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    _common(pe)

    pa = sub.add_parser("grade-all")
    pa.add_argument("--horizon", default="1d")
    pa.add_argument("--panel-cache", default=str(_DEFAULT_PANEL_DIR))
    pa.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    _common(pa)

    pap = sub.add_parser("approve")
    pap.add_argument("--factor", required=True)
    pap.add_argument("--weight", type=float, required=True)
    pap.add_argument("--force", action="store_true",
                     help="approve even if latest grade verdict is fail")
    _common(pap)

    pr = sub.add_parser("reject")
    pr.add_argument("--factor", required=True)
    _common(pr)

    pb = sub.add_parser("backtest-portfolio")
    pb.add_argument("--start", required=True)
    pb.add_argument("--end", required=True)
    _common(pb)

    prp = sub.add_parser("report")
    prp.add_argument("--factor", required=True)
    prp.add_argument("--report-dir", default=str(_DEFAULT_REPORT_DIR))
    _common(prp)

    return p


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd

    store = FactorStore(Path(args.db))
    store.init_schema()
    # Auto-upsert all registered factors so they show up in `list`
    for spec in list_factors():
        store.upsert_factor(
            id=spec.id, category=spec.category,
            direction=spec.direction, description=spec.description,
        )

    if cmd == "list":
        return _cmd_list(store)
    if cmd == "approve":
        return _cmd_approve(store, Path(args.yaml), args.factor, args.weight, args.force)
    if cmd == "reject":
        return _cmd_reject(store, Path(args.yaml), args.factor)
    if cmd == "report":
        return _cmd_report(store, Path(args.report_dir), args.factor)
    if cmd in ("fetch", "eval", "grade-all", "backtest-portfolio"):
        print(
            f"[error] '{cmd}' requires OKX REST + asyncio runtime;\n"
            f"        run it via the wrapper script: scripts/factor_research_smoke.sh\n"
            f"        or call okx_trade.research.cli helpers from a script with\n"
            f"        a constructed OKXRestClient.",
            file=sys.stderr,
        )
        return 2

    return 1


def _cmd_list(store: FactorStore) -> int:
    rows = store.list_factors()
    for r in rows:
        latest = store.latest_grade(r["id"])
        ic = f"{latest['ic_mean']:.4f}" if latest else "—"
        ir = f"{latest['ir']:.4f}" if latest else "—"
        ver = latest["verdict"] if latest else "—"
        flag = "*" if r["approved"] else " "
        w = f" w={r['approved_weight']:.2f}" if r["approved"] else ""
        print(f"{flag} {r['id']:<28} {r['category']:<10} IC={ic} IR={ir} {ver}{w}")
    return 0


def _cmd_approve(store: FactorStore, yaml_path: Path, factor: str, weight: float,
                 force: bool) -> int:
    try:
        spec = get_factor(factor)
    except KeyError as exc:
        print(f"[error] {exc}", file=sys.stderr); return 1

    latest = store.latest_grade(factor)
    if not force and (latest is None or latest["verdict"] != "pass"):
        print(f"[error] factor {factor!r} has no passing grade; use --force to override",
              file=sys.stderr)
        return 1

    cfg = _load_yaml(yaml_path)
    factors = [f for f in cfg.get("factors", []) if f["id"] != factor]
    factors.append({"id": factor, "weight": weight})
    factors.sort(key=lambda f: f["id"])
    cfg["factors"] = factors
    _write_yaml(yaml_path, cfg)
    # sqlite update happens AFTER yaml write to avoid divergence (spec §15.4)
    store.approve(factor, weight=weight, ts_ms=int(time.time() * 1000))
    print(f"approved {factor} weight={weight} → {yaml_path}")
    return 0


def _cmd_reject(store: FactorStore, yaml_path: Path, factor: str) -> int:
    cfg = _load_yaml(yaml_path)
    cfg["factors"] = [f for f in cfg.get("factors", []) if f["id"] != factor]
    _write_yaml(yaml_path, cfg)
    store.reject(factor)
    print(f"rejected {factor}")
    return 0


def _cmd_report(store: FactorStore, report_dir: Path, factor: str) -> int:
    from .report import render_grade_report
    from .grade import FactorGrade

    latest = store.latest_grade(factor)
    if latest is None:
        print(f"[error] no grade history for {factor!r}", file=sys.stderr); return 1
    # FactorGrade has ic_decay (list) but sqlite has scalars only — synthesize empty decay
    g = FactorGrade(
        factor_id=latest["factor_id"],
        panel_start_ms=latest["panel_start_ms"], panel_end_ms=latest["panel_end_ms"],
        horizon_bars=latest["horizon_bars"],
        ic_mean=latest["ic_mean"], ic_std=latest["ic_std"], ir=latest["ir"],
        ic_t_stat=latest["ic_t_stat"], ic_positive_rate=latest["ic_positive_rate"],
        ic_decay=[],
        turnover_avg=latest["turnover_avg"], autocorr_1=latest["autocorr_1"],
        long_short_spread=latest["long_short_spread"],
        net_ls_spread_after_fees=latest["net_ls_spread_after_fees"],
        n_periods=latest["n_periods"], n_instruments=latest["n_instruments"],
        verdict=latest["verdict"], graded_at_ms=latest["graded_at_ms"],
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"{factor}_{time.strftime('%Y-%m-%d')}.md"
    out.write_text(render_grade_report(g))
    print(f"wrote {out}")
    return 0


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {"factors": []}
    return yaml.safe_load(path.read_text()) or {"factors": []}


def _write_yaml(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
```

Create `src/okx_trade/research/__main__.py`:

```python
"""Allow ``python -m okx_trade.research.factor`` (resolved via __main__.py)."""
from __future__ import annotations

import sys

from .cli import run


if __name__ == "__main__":
    sys.exit(run())
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/research/test_cli.py -v`
Expected: 4 passed.

- [ ] **Step 5: Smoke the CLI manually**

```bash
python -m okx_trade.research list --db /tmp/test_zoo.db --yaml /tmp/test_p.yaml
```

Expected: 15 factor rows printed, all unapproved (no `*`).

- [ ] **Step 6: Commit**

```bash
git add src/okx_trade/research/cli.py src/okx_trade/research/__main__.py \
        tests/unit/research/test_cli.py
git commit -m "add(research): CLI with list/approve/reject/report subcommands"
```

---

## Task 17: `strategies/factor_portfolio.py` — pure synthesis functions

This task only ships the **pure** functions (z-score, weighted synthesis, top-K selection)
without any NT dependency, so they can be tested cheaply. The NT `Strategy` wrapper comes
in Task 18.

**Files:**
- Create: `src/okx_trade/strategies/factor_portfolio.py`
- Create: `tests/unit/strategies/test_strategy_factor_portfolio.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/strategies/test_strategy_factor_portfolio.py`:

```python
"""Tests for FactorPortfolioStrategy pure synthesis."""
from __future__ import annotations

import importlib

import numpy as np
import pytest

from okx_trade.research.panel import FactorPanel
from okx_trade.research.registry import clear_registry, register_factor
from okx_trade.strategies.factor_portfolio import (
    FactorWeight,
    cross_section_zscore,
    select_top_bot,
    synthesize_score,
)


@pytest.fixture(autouse=True)
def _isolate():
    clear_registry()
    import okx_trade.research.factors.momentum as m
    importlib.reload(m)  # bring back built-ins for these tests
    yield
    clear_registry()


def _panel(close: np.ndarray) -> FactorPanel:
    T, N = close.shape
    return FactorPanel(
        inst_ids=tuple(f"I{i}" for i in range(N)),
        timestamps_ms=tuple(range(T)),
        close=close, volume_usdt=np.ones((T, N)),
        funding_rate=None, open_interest=None, basis_apr=None,
    )


def test_cross_section_zscore_zero_mean_unit_var() -> None:
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    z = cross_section_zscore(vals)
    assert z.mean() == pytest.approx(0.0)
    assert z.std(ddof=0) == pytest.approx(1.0)


def test_cross_section_zscore_returns_nan_when_no_variance() -> None:
    vals = np.array([3.0, 3.0, 3.0])
    z = cross_section_zscore(vals)
    assert np.all(np.isnan(z))


def test_synthesize_score_combines_factors_by_weight() -> None:
    closes = np.linspace(100, 124, 25).reshape(25, 1).repeat(3, axis=1)
    closes[:, 1] *= 0.5  # second instrument has worse close progression
    panel = _panel(closes)
    weights = [
        FactorWeight(id="momentum_1d", weight=1.0),
    ]
    score, missing = synthesize_score(panel, weights)
    assert score.shape == (panel.n,)
    assert missing == []


def test_synthesize_score_skips_unregistered_with_warning() -> None:
    panel = _panel(np.ones((30, 3)) * 100.0)
    weights = [FactorWeight(id="nonexistent", weight=1.0)]
    score, missing = synthesize_score(panel, weights)
    assert "nonexistent" in missing
    # Score is all-NaN when all weights are missing
    assert np.all(np.isnan(score))


def test_select_top_bot_returns_indices_by_score() -> None:
    score = np.array([0.5, -1.2, 0.8, -0.3, 1.5])
    longs, shorts = select_top_bot(score, top_k_long=2, top_k_short=2)
    assert longs == [4, 2]   # 1.5, 0.8 — descending
    assert shorts == [1, 3]  # -1.2, -0.3 — ascending


def test_select_top_bot_skips_nan_scores() -> None:
    score = np.array([0.5, np.nan, 0.8, -0.3, 1.5])
    longs, shorts = select_top_bot(score, top_k_long=3, top_k_short=3)
    assert 1 not in longs and 1 not in shorts
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/strategies/test_strategy_factor_portfolio.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement pure functions in `src/okx_trade/strategies/factor_portfolio.py`**

```python
"""FactorPortfolioStrategy — generic factor synthesizer (linear weighted z-score).

Pure-function layer (NT-independent, used by tests + the NT Strategy class below).
The NT Strategy class is implemented in a follow-up section that imports NT lazily.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ..research.compute import compute_factor
from ..research.panel import FactorPanel
from ..research.registry import get_factor


@dataclass(frozen=True, slots=True)
class FactorWeight:
    id: str
    weight: float


def cross_section_zscore(vals: np.ndarray) -> np.ndarray:
    """Per-row z-score across instruments. NaN-safe. Returns all-NaN if std=0."""
    mu = np.nanmean(vals)
    sd = np.nanstd(vals)
    if not np.isfinite(sd) or sd == 0:
        return np.full_like(vals, np.nan, dtype=float)
    return (vals - mu) / sd


def synthesize_score(
    panel: FactorPanel,
    weights: list[FactorWeight],
) -> tuple[np.ndarray, list[str]]:
    """Compute each factor's last-row values, z-score, weight, and sum.

    Direction handling: ``long_low`` factors are negated so high score → long.

    Returns:
        (score, missing_ids): score is shape (panel.n,); missing_ids lists weight
        entries whose factor id isn't registered (skipped gracefully).
    """
    accumulated = np.zeros(panel.n, dtype=float)
    used_weight = 0.0
    missing: list[str] = []
    for w in weights:
        try:
            spec = get_factor(w.id)
        except KeyError:
            missing.append(w.id)
            continue
        try:
            arr = compute_factor(w.id, panel)
        except ValueError:
            missing.append(w.id)
            continue
        last = arr[-1].astype(float)
        if spec.direction == "long_low":
            last = -last
        z = cross_section_zscore(last)
        # If a row is all-NaN, skip it (don't pollute accumulated)
        if np.all(np.isnan(z)):
            missing.append(w.id)
            continue
        # NaN entries pass through as 0 contribution; finite entries contribute
        contrib = np.where(np.isnan(z), 0.0, z * w.weight)
        accumulated = accumulated + contrib
        used_weight += w.weight
    if used_weight == 0:
        return np.full(panel.n, np.nan, dtype=float), missing
    return accumulated, missing


def select_top_bot(
    score: np.ndarray, *, top_k_long: int, top_k_short: int,
) -> tuple[list[int], list[int]]:
    """Pick top-K (long) and bot-K (short) indices, skipping NaN scores.

    Returns indices into the panel's ``inst_ids`` array. Longs sorted descending,
    shorts sorted ascending (most-negative first).
    """
    valid = np.where(np.isfinite(score))[0]
    if len(valid) == 0:
        return [], []
    order = valid[np.argsort(score[valid])]  # ascending
    longs = order[-top_k_long:][::-1].tolist() if top_k_long > 0 else []
    shorts = order[:top_k_short].tolist() if top_k_short > 0 else []
    return longs, shorts


__all__ = [
    "FactorWeight",
    "cross_section_zscore",
    "select_top_bot",
    "synthesize_score",
]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/strategies/test_strategy_factor_portfolio.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/factor_portfolio.py \
        tests/unit/strategies/test_strategy_factor_portfolio.py
git commit -m "add(strategies): FactorPortfolio pure synthesis (z-score + top-K)"
```

---

## Task 18: `FactorPortfolioStrategy` NT Strategy class

**Files:**
- Modify: `src/okx_trade/strategies/factor_portfolio.py` (append NT class)
- Modify: `tests/unit/strategies/test_strategy_factor_portfolio.py` (append NT tests)

- [ ] **Step 1: Write failing tests (skipped without NT)**

Append to `tests/unit/strategies/test_strategy_factor_portfolio.py`:

```python
nt = pytest.importorskip("nautilus_trader")


def test_factor_portfolio_config_loads_from_yaml() -> None:
    """Verify the dataclass-mode StrategyConfig accepts our yaml shape."""
    from okx_trade.strategies.factor_portfolio import FactorPortfolioConfig
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX", "ETH-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=2, top_k_short=2,
        risk_pct=0.002,
        account_equity_usdt=10_000.0,
        factor_weights=[("momentum_7d", 0.5), ("funding_z_30d", 0.5)],
    )
    assert cfg.rebalance_hours == 4
    assert len(cfg.factor_weights) == 2


def test_factor_portfolio_strategy_initializes_without_error() -> None:
    from okx_trade.strategies.factor_portfolio import (
        FactorPortfolioConfig, FactorPortfolioStrategy,
    )
    cfg = FactorPortfolioConfig(
        instrument_ids=["BTC-USDT-SWAP.OKX"],
        bar_type_template="{inst}-1-HOUR-LAST-EXTERNAL",
        rebalance_hours=4,
        top_k_long=1, top_k_short=1,
        risk_pct=0.002, account_equity_usdt=10_000.0,
        factor_weights=[("momentum_7d", 1.0)],
    )
    strategy = FactorPortfolioStrategy(cfg)
    assert strategy.config.rebalance_hours == 4
```

- [ ] **Step 2: Run, verify fail**

Run: `pytest tests/unit/strategies/test_strategy_factor_portfolio.py -v`
Expected: 2 new failures: `ImportError: cannot import name 'FactorPortfolioConfig'`.

- [ ] **Step 3: Append NT Strategy class to `src/okx_trade/strategies/factor_portfolio.py`**

Append below the existing pure functions:

```python
# ---------------------------------------------------------------------------
# NT Strategy (lazy-loaded — only available if nautilus_trader is installed)
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from nautilus_trader.model.data import Bar


try:
    from nautilus_trader.model.data import BarType
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import InstrumentId
    from nautilus_trader.trading.config import StrategyConfig
    from nautilus_trader.trading.strategy import Strategy

    _NT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NT_AVAILABLE = False
    StrategyConfig = object  # type: ignore[assignment,misc]
    Strategy = object        # type: ignore[assignment,misc]


if _NT_AVAILABLE:

    from collections import deque
    import time

    from ..research import factors as _trigger_factor_registration  # noqa: F401
    from ..risk import RiskConfig, RiskIntent, apply_risk_manager, build_risk_manager
    from .base import effective_equity_usdt, position_contracts
    from .pnl_hook import record_strategy_trade
    from .qty import safe_make_qty


    class FactorPortfolioConfig(StrategyConfig, frozen=True):  # type: ignore[misc]
        """Configuration mirror of ``configs/factor_portfolio.yaml``.

        ``factor_weights`` is a list of (id, weight) tuples (StrategyConfig requires
        hashable/frozen types — list-of-tuples avoids dict mutation while staying
        round-trippable to/from yaml).
        """
        instrument_ids: list[str]
        bar_type_template: str
        rebalance_hours: int = 4
        top_k_long: int = 5
        top_k_short: int = 5
        risk_pct: float = 0.002
        account_equity_usdt: float = 10_000.0
        factor_weights: list[tuple[str, float]] = []
        risk_config: RiskConfig | None = None


    class FactorPortfolioStrategy(Strategy):  # type: ignore[misc]
        """Generic factor portfolio: read approved factors → synthesize → top-K trade.

        Compatible with the existing risk / pnl / portfolio infrastructure (matches
        the patterns in xs_momentum + ml_fusion).
        """

        def __init__(self, config: FactorPortfolioConfig) -> None:
            super().__init__(config)
            self._inst_ids = [InstrumentId.from_str(s) for s in config.instrument_ids]
            self._bar_types = {
                iid: BarType.from_str(config.bar_type_template.format(inst=iid.value))
                for iid in self._inst_ids
            }
            # Buffer enough bars for the slowest factor (vol_of_vol_30d = 60d * 24 + slack)
            self._closes: dict[str, deque[float]] = {
                iid.value: deque(maxlen=60 * 24 + 50) for iid in self._inst_ids
            }
            self._volumes: dict[str, deque[float]] = {
                iid.value: deque(maxlen=60 * 24 + 50) for iid in self._inst_ids
            }
            self._last_rebalance_ms: int = 0
            self._positions: dict[str, tuple[str, float, int]] = {}
            self._allocated_equity_usdt: float | None = None
            self._weights = [FactorWeight(id=fid, weight=w)
                             for fid, w in config.factor_weights]
            self._risk_manager, self._risk_handles = build_risk_manager(config.risk_config)
            self._pnl_tracker = None  # type: ignore[var-annotated]

        def on_start(self) -> None:
            for bar_type in self._bar_types.values():
                self.subscribe_bars(bar_type)
            self.log.info(
                f"factor_portfolio start: factors={[w.id for w in self._weights]} "
                f"top_k={self.config.top_k_long}/{self.config.top_k_short}"
            )
            if not self._weights:
                self.log.warning("factor_portfolio: no factor_weights configured; idle")

        def on_stop(self) -> None:
            self.log.info(f"factor_portfolio stop; open_legs={len(self._positions)}")

        def on_bar(self, bar: Bar) -> None:
            inst_value = bar.bar_type.instrument_id.value
            if inst_value not in self._closes:
                return
            self._closes[inst_value].append(bar.close.as_double())
            self._volumes[inst_value].append(bar.volume.as_double() * bar.close.as_double())

            now_ms = int(bar.ts_event // 1_000_000)
            if (self._weights
                    and now_ms - self._last_rebalance_ms
                    >= self.config.rebalance_hours * 3_600_000):
                self._rebalance(now_ms)
                self._last_rebalance_ms = now_ms

        def _build_panel(self) -> FactorPanel | None:
            inst_ids = tuple(iid.value for iid in self._inst_ids)
            T_min = min(len(self._closes[i]) for i in inst_ids)
            if T_min < 24:
                return None
            T = T_min
            close = np.column_stack([
                np.asarray(list(self._closes[i])[-T:], dtype=float) for i in inst_ids
            ])
            volume = np.column_stack([
                np.asarray(list(self._volumes[i])[-T:], dtype=float) for i in inst_ids
            ])
            ts = tuple(range(T))  # synthetic ts; factor functions don't use ts
            return FactorPanel(
                inst_ids=inst_ids, timestamps_ms=ts,
                close=close, volume_usdt=volume,
                funding_rate=None, open_interest=None, basis_apr=None,
            )

        def _rebalance(self, ts_ms: int) -> None:
            panel = self._build_panel()
            if panel is None:
                return
            score, missing = synthesize_score(panel, self._weights)
            if missing:
                self.log.warning(f"factor_portfolio: skipped factors {missing}")
            if not np.any(np.isfinite(score)):
                self.log.warning("factor_portfolio: all-NaN score; no trades this round")
                return
            longs, shorts = select_top_bot(
                score,
                top_k_long=self.config.top_k_long,
                top_k_short=self.config.top_k_short,
            )
            target = {panel.inst_ids[i]: "long" for i in longs}
            target.update({panel.inst_ids[i]: "short" for i in shorts})

            # Close legs no longer in target
            for inst_v, (cur_dir, _, _) in list(self._positions.items()):
                if inst_v not in target or target[inst_v] != cur_dir:
                    self._close_leg(inst_v, reason="REBALANCE", exit_ts_ms=ts_ms)

            for inst_v, direction in target.items():
                if inst_v not in self._positions:
                    self._open_leg(inst_v, direction, ts_ms=ts_ms)

            self.log.info(
                f"factor_portfolio rebalance: longs={longs} shorts={shorts} "
                f"open_legs={len(self._positions)}"
            )

        def _open_leg(self, inst_value: str, direction: str, *, ts_ms: int) -> None:
            cfg: FactorPortfolioConfig = self.config  # type: ignore[assignment]
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            closes = list(self._closes[inst_value])
            if not closes:
                return
            entry_px = closes[-1]
            ct_val = float(inst.multiplier) if float(inst.multiplier) > 0 else 1.0
            lot = float(inst.size_increment) if float(inst.size_increment) > 0 else 1.0
            sl_distance = entry_px * 0.01  # conservative 1% SL fallback
            stop = entry_px - sl_distance if direction == "long" else entry_px + sl_distance
            equity = effective_equity_usdt(self._allocated_equity_usdt, cfg.account_equity_usdt)
            contracts = position_contracts(
                account_equity_usdt=equity, risk_pct=cfg.risk_pct,
                entry_price=entry_px, stop_price=stop,
                ct_val=ct_val, min_sz=lot, lot_sz=lot,
            )
            if contracts <= 0:
                return
            intent = RiskIntent(
                strategy_id=str(self.id), instrument_id=inst_value,
                direction=direction,  # type: ignore[arg-type]
                size=contracts, entry_price=entry_px, stop_price=stop,
                account_equity_usdt=equity,
            )
            adjusted = apply_risk_manager(self, self._risk_manager, intent)
            if adjusted is None or adjusted <= 0:
                return
            qty_obj = safe_make_qty(inst, adjusted, self.log, ctx=f"open {inst_value}")
            if qty_obj is None:
                return
            side = OrderSide.BUY if direction == "long" else OrderSide.SELL
            self.submit_order(self.order_factory.market(
                instrument_id=inst_id, order_side=side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
            ))
            self._positions[inst_value] = (direction, adjusted, ts_ms)
            self.log.info(f"OPEN {direction} {inst_value} qty={adjusted}")

        def _close_leg(self, inst_value: str, *, reason: str, exit_ts_ms: int) -> None:
            pos = self._positions.get(inst_value)
            if pos is None:
                return
            direction, contracts, _entry_ts = pos
            inst_id = InstrumentId.from_str(inst_value)
            inst = self.cache.instrument(inst_id)
            if inst is None:
                return
            qty_obj = safe_make_qty(inst, contracts, ctx=f"close {inst_value}")
            if qty_obj is None:
                return
            close_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
            self.submit_order(self.order_factory.market(
                instrument_id=inst_id, order_side=close_side,
                quantity=qty_obj, time_in_force=TimeInForce.IOC,
                reduce_only=True,
            ))
            self.log.info(f"CLOSE {direction} {inst_value} reason={reason}")
            self._positions.pop(inst_value, None)


else:  # pragma: no cover

    class FactorPortfolioConfig:  # type: ignore[no-redef]
        pass

    class FactorPortfolioStrategy:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise ImportError(
                "nautilus_trader not installed; run pip install -e '.[strategy]'"
            )
```

Also extend `__all__` at module bottom:

```python
__all__ = [
    "FactorPortfolioConfig", "FactorPortfolioStrategy",
    "FactorWeight",
    "cross_section_zscore", "select_top_bot", "synthesize_score",
]
```

- [ ] **Step 4: Run, verify pass**

Run: `pytest tests/unit/strategies/test_strategy_factor_portfolio.py -v`
Expected: 8 passed (6 pure + 2 NT — NT tests skipped if NT not installed).

- [ ] **Step 5: Commit**

```bash
git add src/okx_trade/strategies/factor_portfolio.py \
        tests/unit/strategies/test_strategy_factor_portfolio.py
git commit -m "add(strategies): FactorPortfolioStrategy NT class with rebalance loop"
```

---

## Task 19: `configs/factor_portfolio.yaml` initial config + wire into runtime registry

**Files:**
- Create: `configs/factor_portfolio.yaml`
- Modify: `configs/live.yaml` (add `factor_portfolio` block, disabled)
- Modify: `src/okx_trade/runtime/live_node.py` (add factor_portfolio to strategy registry)

The strategy registry lives in `src/okx_trade/runtime/live_node.py` in a function that
returns `dict[str, tuple[Config, Strategy]]` — `scripts/live.py` does not register
strategies directly. The pattern is "lazy import inside a try/except ImportError" so the
module loads even without NT.

- [ ] **Step 1: Locate the registry**

Run: `grep -n '"ml_fusion"' src/okx_trade/runtime/live_node.py`
Expected: one match (around line 99), inside the M6+ batch try/except block.

- [ ] **Step 2: Create `configs/factor_portfolio.yaml`** with empty approved factors:

```yaml
# Factor Portfolio Strategy config (managed by `okx_trade.research.cli`)
# `factors` is populated by `python -m okx_trade.research approve --factor ... --weight ...`
# (DO NOT hand-edit — use the CLI to keep sqlite + yaml in sync.)

bar: "1H"
rebalance_hours: 4
top_k_long: 5
top_k_short: 5
risk_pct_per_leg: 0.002
account_equity_usdt: 10000.0

universe:
  size: 30
  settle_ccy: USDT
  sort_by: vol24h

factors: []   # populated by CLI
```

- [ ] **Step 3: Add a config entry to `configs/live.yaml`**

Insert after the existing `ob_imbalance` strategy block:

```yaml
  factor_portfolio:
    # New M6+ strategy (factor research lab output). Default disabled —
    # only enable after `python -m okx_trade.research approve ...` populates
    # configs/factor_portfolio.yaml with ≥ 3 factors that passed grade.
    enabled: false
    config: configs/factor_portfolio.yaml
```

- [ ] **Step 4: Wire the strategy class into `runtime/live_node.py`**

Find the existing `ml_fusion` try/except block (around line 97-101):

```python
    try:
        from ..strategies.ml_fusion import MLFusionConfig, MLFusionStrategy
        registry["ml_fusion"] = (MLFusionConfig, MLFusionStrategy)
    except ImportError:
        pass
    return registry
```

Append a new identical block **before** `return registry`:

```python
    try:
        from ..strategies.factor_portfolio import (
            FactorPortfolioConfig, FactorPortfolioStrategy,
        )
        registry["factor_portfolio"] = (FactorPortfolioConfig, FactorPortfolioStrategy)
    except ImportError:
        pass
    return registry
```

The (Config, Strategy) **order matters** — match the tuple ordering used by every other
strategy in this file (Config first, Strategy second). Verify with: `grep -A1 'registry\["' src/okx_trade/runtime/live_node.py`.

- [ ] **Step 5: Run all strategy tests to confirm no regression**

Run: `pytest tests/unit/strategies/ -v --no-header`
Expected: All existing strategy tests pass + new factor_portfolio tests pass.

- [ ] **Step 6: Commit**

```bash
git add configs/factor_portfolio.yaml configs/live.yaml scripts/live.py
git commit -m "enable(factor_portfolio): wire FactorPortfolioStrategy into live runner"
```

---

## Task 20: `scripts/factor_research_smoke.sh` — end-to-end smoke

**Files:**
- Create: `scripts/factor_research_smoke.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Factor research lab end-to-end smoke.
# Exercises: CLI list → factor registration → store init → report rendering.
# Does NOT hit OKX REST (offline-safe; meant for CI + new-machine sanity).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

TMPDIR="$(mktemp -d)"
DB="$TMPDIR/zoo.db"
YML="$TMPDIR/factor_portfolio.yaml"

echo "[1/4] list — should print 15 factors"
python -m okx_trade.research list --db "$DB" --yaml "$YML" | tee "$TMPDIR/list.txt"
test "$(wc -l < "$TMPDIR/list.txt")" -ge 15

echo "[2/4] approve momentum_7d (--force, no grade yet)"
python -m okx_trade.research approve --factor momentum_7d --weight 0.3 \
       --force --db "$DB" --yaml "$YML"

echo "[3/4] verify yaml has momentum_7d entry"
grep -q "momentum_7d" "$YML"

echo "[4/4] reject momentum_7d"
python -m okx_trade.research reject --factor momentum_7d --db "$DB" --yaml "$YML"
# After reject the yaml should still parse and not contain momentum_7d
python -c "import yaml; cfg = yaml.safe_load(open('$YML')); \
    assert all(f['id'] != 'momentum_7d' for f in cfg.get('factors', []))"

rm -rf "$TMPDIR"
echo "factor_research_smoke OK"
```

- [ ] **Step 2: Make executable + run**

```bash
chmod +x scripts/factor_research_smoke.sh
./scripts/factor_research_smoke.sh
```

Expected: 4 step lines + `factor_research_smoke OK`.

- [ ] **Step 3: Commit**

```bash
git add scripts/factor_research_smoke.sh
git commit -m "add(scripts): factor_research_smoke.sh end-to-end regression"
```

---

## Task 21: Update `docs/strategy_roadmap.md` + `README.md`

**Files:**
- Modify: `docs/strategy_roadmap.md` (add FactorPortfolioStrategy row + research lab note)
- Modify: `README.md` (mention research lab in 3-layer diagram comment)

- [ ] **Step 1: Read current `docs/strategy_roadmap.md`**

Run: `head -60 docs/strategy_roadmap.md`

- [ ] **Step 2: Append a new row + section**

Add to the strategy table (after `MLFusionStrategy` row):

```markdown
| [`FactorPortfolioStrategy`](../src/okx_trade/strategies/factor_portfolio.py) | meta | bar-driven (4h default) | **P1** | ❌ false | Generic factor synthesizer; reads configs/factor_portfolio.yaml populated by research lab |
```

Add a new section after "## M6+ 启用清单":

```markdown
---

## Factor Research Lab (P1, 2026-05-19)

新模块 `okx_trade.research`：CLI-driven 因子评估 pipeline + 通用 FactorPortfolio 策略。

- 设计文档: [`docs/superpowers/specs/2026-05-19-factor-research-lab-design.md`](superpowers/specs/2026-05-19-factor-research-lab-design.md)
- 实施计划: [`docs/superpowers/plans/2026-05-19-factor-research-lab.md`](superpowers/plans/2026-05-19-factor-research-lab.md)
- 入口: `python -m okx_trade.research <list|fetch|eval|approve|reject|backtest-portfolio|report>`
- 因子库 (v1, 15 个): momentum × 4, funding/OI × 4, basis × 2, volatility × 3, flow × 2

启用步骤：

1. `python -m okx_trade.research fetch --start 2025-11-01 --end 2026-05-15 --universe top30`
2. `python -m okx_trade.research grade-all --horizon 1d`
3. 看 `var/factor_research/reports/*.md` 选 3-5 个 verdict=pass 的因子
4. `python -m okx_trade.research approve --factor <id> --weight <0.1-0.4>` 逐个加
5. 改 `configs/live.yaml`: `factor_portfolio.enabled: true`
6. paper 跑 7 天看与 xs_momentum 相关系数（< 0.7 OK，否则砍权重）
```

- [ ] **Step 3: Append note to `README.md`**

Find the section listing strategies / modules in `README.md` and add:

```markdown
新增 (2026-05-19): **因子研究 lab** — `okx_trade.research` 模块 + `FactorPortfolioStrategy`。
CLI 评估任意因子的 IC/IR/decay/turnover，通过 grade 的因子直接喂 yaml 上线。
详见 [strategy_roadmap.md](docs/strategy_roadmap.md#factor-research-lab-p1-2026-05-19)。
```

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/unit -v --no-header 2>&1 | tail -30`
Expected: All tests pass; total count should be approximately 449 + ~85 ≈ 534 tests.

- [ ] **Step 5: Commit**

```bash
git add docs/strategy_roadmap.md README.md
git commit -m "docs: add factor research lab + FactorPortfolioStrategy to roadmap"
```

---

## Final Verification Checklist

After all 21 tasks complete, run:

- [ ] `pytest tests/unit -v --no-header 2>&1 | tail -5` — confirm ≥ 530 passing tests
- [ ] `python -m okx_trade.research list` — prints 15 factors
- [ ] `./scripts/factor_research_smoke.sh` — exits OK
- [ ] `git log --oneline | head -25` — 21 well-scoped commits
- [ ] `git diff main --stat` — no unexpected file changes
- [ ] Re-read spec §15 risks; confirm code addresses each (esp. yaml/sqlite atomic order in `_cmd_approve`)

When all green, this branch is ready for the live-data validation phase (per spec §14
Phase 1 verdict). Live validation is NOT part of this plan — it is a separate plan that
follows successful paper-trading observation.

---

## Open Items Deferred to Future Plans

Per spec §14, the following are explicitly **not** in this plan:

- **P2**: Replacing `ml_fusion._features.FeatureRow` with `research.registry` consumption; high-frequency tick persistence + microstructure factors
- **P3**: External (on-chain, macro) data sources; genetic / LLM factor search

Each will get its own spec → plan when the team is ready.
