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
    def __init__(self,
                 pending_orders: list[dict[str, Any]] | None = None,
                 pending_raises: Exception | None = None,
                 close_fail_inst_ids: set[str] | None = None,
                 cancel_fail_ord_ids: set[str] | None = None) -> None:
        self.close_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self._close_fail_inst_ids = close_fail_inst_ids or set()
        self._cancel_fail_ord_ids = cancel_fail_ord_ids or set()
        self._pending_orders = pending_orders or []
        self._pending_raises = pending_raises

    async def get_pending_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self._pending_raises:
            raise self._pending_raises
        return list(self._pending_orders)

    async def cancel_order(self, *, inst_id: str, ord_id: str,
                            **kwargs: Any) -> dict[str, Any]:
        self.cancel_calls.append({"inst_id": inst_id, "ord_id": ord_id})
        if ord_id in self._cancel_fail_ord_ids:
            raise RuntimeError(f"simulated cancel failure for {ord_id}")
        return {"ordId": ord_id, "sCode": "0"}

    async def close_position(self, inst_id: str, *, pos_side: Any,
                              mgn_mode: Any, **kwargs: Any) -> dict[str, Any]:
        self.close_calls.append(
            {"inst_id": inst_id, "pos_side": pos_side, "mgn_mode": mgn_mode}
        )
        if inst_id in self._close_fail_inst_ids:
            raise RuntimeError(f"simulated close failure for {inst_id}")
        return {"clOrdId": f"closed-{inst_id}"}


class _FakeClient:
    def __init__(self, positions: list[Position],
                 get_positions_raises: Exception | None = None,
                 pending_orders: list[dict[str, Any]] | None = None,
                 pending_raises: Exception | None = None,
                 close_fail_inst_ids: set[str] | None = None,
                 cancel_fail_ord_ids: set[str] | None = None) -> None:
        self.account = _FakeAccount(positions, raises=get_positions_raises)
        self.trade = _FakeTrade(
            pending_orders=pending_orders,
            pending_raises=pending_raises,
            close_fail_inst_ids=close_fail_inst_ids,
            cancel_fail_ord_ids=cancel_fail_ord_ids,
        )

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
    assert client.trade.close_calls == []


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
    assert len(client.trade.close_calls) == 2
    assert {c["inst_id"] for c in client.trade.close_calls} == {
        "BTC-USDT-SWAP", "ETH-USDT-SWAP",
    }


@pytest.mark.asyncio
async def test_partial_failure_returns_partial_code(fake_settings_demo: None) -> None:
    client = _FakeClient(
        positions=[
            _mk_position("BTC-USDT-SWAP", pos=0.5, pos_side="long"),
            _mk_position("ETH-USDT-SWAP", pos=-1.0, pos_side="short"),
        ],
        close_fail_inst_ids={"ETH-USDT-SWAP"},
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_PARTIAL_FAIL
    assert len(client.trade.close_calls) == 2  # 都试过


@pytest.mark.asyncio
async def test_dry_run_does_not_close(fake_settings_demo: None) -> None:
    client = _FakeClient(positions=[
        _mk_position("BTC-USDT-SWAP", pos=0.5, pos_side="long"),
    ])
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=True, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert client.trade.close_calls == []


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


# ---------------------------------------------------------------------------
# 阶段 1: 取消 pending 单 —— 2026-05-15 加入
# 回归 sCode=51169 故障:OKX 端遗留的 ACCEPTED 但未成交单会被 NT 启动时识别为
# 幻仓 → 下次 rebalance 发 reduce_only → OKX 返"position not exist"=51169
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancels_pending_orders_before_closing(fake_settings_demo: None) -> None:
    """有 pending 单 + 持仓 → 先取消所有单,再平所有仓。"""
    client = _FakeClient(
        positions=[
            _mk_position("BTC-USDT-SWAP", pos=0.5, pos_side="long"),
        ],
        pending_orders=[
            {"instId": "ETH-USDT-SWAP", "ordId": "111", "side": "buy",
             "sz": "0.05", "state": "live"},
            {"instId": "DOGE-USDT-SWAP", "ordId": "222", "side": "sell",
             "sz": "100", "state": "live"},
        ],
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert len(client.trade.cancel_calls) == 2
    assert {c["ord_id"] for c in client.trade.cancel_calls} == {"111", "222"}
    assert len(client.trade.close_calls) == 1
    assert client.trade.close_calls[0]["inst_id"] == "BTC-USDT-SWAP"


@pytest.mark.asyncio
async def test_no_pending_no_positions(fake_settings_demo: None) -> None:
    """完全干净 → 啥都不动,exit OK。"""
    client = _FakeClient(positions=[], pending_orders=[])
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert client.trade.cancel_calls == []
    assert client.trade.close_calls == []


@pytest.mark.asyncio
async def test_pending_only_no_positions(fake_settings_demo: None) -> None:
    """有 pending 单但无持仓 → 取消单子,无平仓动作。"""
    client = _FakeClient(
        positions=[],
        pending_orders=[
            {"instId": "ETH-USDT-SWAP", "ordId": "111", "side": "buy",
             "sz": "0.05", "state": "live"},
        ],
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert len(client.trade.cancel_calls) == 1
    assert client.trade.close_calls == []


@pytest.mark.asyncio
async def test_cancel_failure_returns_partial(fake_settings_demo: None) -> None:
    """部分 cancel 失败(已 fill 等) → 仍继续到 close 阶段,但 exit 1。"""
    client = _FakeClient(
        positions=[],
        pending_orders=[
            {"instId": "A", "ordId": "111", "state": "live"},
            {"instId": "B", "ordId": "222", "state": "live"},
        ],
        cancel_fail_ord_ids={"222"},
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_PARTIAL_FAIL
    assert len(client.trade.cancel_calls) == 2


@pytest.mark.asyncio
async def test_pending_query_fail_returns_connect(fake_settings_demo: None) -> None:
    """连不上 OKX(查 pending 时挂)→ EXIT_CONNECT_FAIL,不动任何状态。"""
    client = _FakeClient(
        positions=[],
        pending_raises=RuntimeError("network down"),
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=False, require_demo=True)
    assert exit_code == rc.EXIT_CONNECT_FAIL
    assert client.trade.cancel_calls == []
    assert client.trade.close_calls == []


@pytest.mark.asyncio
async def test_dry_run_does_not_cancel(fake_settings_demo: None) -> None:
    """--dry-run 在 cancel 阶段也只列不动。"""
    client = _FakeClient(
        positions=[_mk_position("BTC-USDT-SWAP", pos=0.5, pos_side="long")],
        pending_orders=[
            {"instId": "ETH", "ordId": "111", "state": "live"},
        ],
    )
    with patch.object(rc, "OKXRestClient", return_value=client):
        exit_code = await rc.reconcile(dry_run=True, require_demo=True)
    assert exit_code == rc.EXIT_OK
    assert client.trade.cancel_calls == []
    assert client.trade.close_calls == []
