"""scripts/reconcile_okx_positions.py 单测——mock OKXRestClient,验证退出码 + 调用次数。"""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from okx_trade.models import Position

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "reconcile_okx_positions.py"


def _load_reconcile() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reconcile_mod", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load_reconcile()


def _mk_position(inst_id: str, pos: float, pos_side: str = "net",
                 mgn_mode: str = "cross") -> Position:
    return Position(
        instId=inst_id, instType="SWAP", mgnMode=mgn_mode, posSide=pos_side,
        pos=Decimal(str(pos)), avgPx=Decimal("0"),
    )


class _FakeAccount:
    def __init__(self, positions: list[Position],
                 raises: Exception | None = None) -> None:
        self._positions = positions
        self._raises = raises

    async def get_positions(self, **kwargs: Any) -> list[Position]:
        if self._raises:
            raise self._raises
        return list(self._positions)


class _FakeTrade:
    def __init__(self, fail_inst_ids: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_inst_ids = fail_inst_ids or set()

    async def close_position(self, inst_id: str, *, pos_side: Any,
                              mgn_mode: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"inst_id": inst_id, "pos_side": pos_side, "mgn_mode": mgn_mode})
        if inst_id in self._fail_inst_ids:
            raise RuntimeError(f"simulated close failure for {inst_id}")
        return {"clOrdId": f"closed-{inst_id}"}


class _FakeClient:
    def __init__(self, positions: list[Position],
                 get_positions_raises: Exception | None = None,
                 fail_inst_ids: set[str] | None = None) -> None:
        self.account = _FakeAccount(positions, raises=get_positions_raises)
        self.trade = _FakeTrade(fail_inst_ids=fail_inst_ids)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def fake_settings_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure OKXSettings(is_demo=True) regardless of host .env."""
    monkeypatch.setenv("OKX_IS_DEMO", "true")
    monkeypatch.setenv("OKX_API_KEY", "test")
    monkeypatch.setenv("OKX_API_SECRET", "test")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "test")


@pytest.fixture
def fake_settings_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKX_IS_DEMO", "false")
    monkeypatch.setenv("OKX_API_KEY", "test")
    monkeypatch.setenv("OKX_API_SECRET", "test")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "test")


@pytest.mark.asyncio
async def test_no_open_positions_exits_ok(fake_settings_demo: None) -> None:
    client = _FakeClient(positions=[
        _mk_position("BTC-USDT-SWAP", pos=0.0),
        _mk_position("ETH-USDT-SWAP", pos=0.0),
    ])
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert client.trade.calls == []


@pytest.mark.asyncio
async def test_open_positions_closed_all_success(fake_settings_demo: None) -> None:
    client = _FakeClient(positions=[
        _mk_position("BTC-USDT-SWAP", pos=0.5, pos_side="long"),
        _mk_position("ETH-USDT-SWAP", pos=-1.0, pos_side="short"),
        _mk_position("SOL-USDT-SWAP", pos=0.0),
    ])
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert len(client.trade.calls) == 2
    assert {c["inst_id"] for c in client.trade.calls} == {
        "BTC-USDT-SWAP", "ETH-USDT-SWAP",
    }


@pytest.mark.asyncio
async def test_partial_failure_returns_partial_code(fake_settings_demo: None) -> None:
    client = _FakeClient(
        positions=[
            _mk_position("BTC-USDT-SWAP", pos=0.5, pos_side="long"),
            _mk_position("ETH-USDT-SWAP", pos=-1.0, pos_side="short"),
        ],
        fail_inst_ids={"ETH-USDT-SWAP"},
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_PARTIAL_FAIL
    assert len(client.trade.calls) == 2  # 都试过


@pytest.mark.asyncio
async def test_dry_run_does_not_close(fake_settings_demo: None) -> None:
    client = _FakeClient(positions=[
        _mk_position("BTC-USDT-SWAP", pos=0.5, pos_side="long"),
    ])
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=True, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert client.trade.calls == []


@pytest.mark.asyncio
async def test_connect_failure_returns_connect_code(fake_settings_demo: None) -> None:
    client = _FakeClient(
        positions=[],
        get_positions_raises=RuntimeError("simulated network down"),
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_CONNECT_FAIL


@pytest.mark.asyncio
async def test_live_refused_when_require_demo(fake_settings_live: None) -> None:
    """在实盘环境 require_demo=True 应直接拒绝运行,不查 OKX 不动单。"""
    sentinel = MagicMock()
    with patch.object(rc, "OKXRestClient", return_value=sentinel):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_REFUSED_LIVE
    sentinel.assert_not_called()


@pytest.mark.asyncio
async def test_live_allowed_when_require_demo_off(fake_settings_live: None) -> None:
    """--no-require-demo 显式允许实盘 → 正常流程。"""
    client = _FakeClient(positions=[])
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=False)
    assert exit_code == rc.EXIT_OK
