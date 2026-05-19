"""Tests for the CLI: cover argparse routing + approve writes yaml + sqlite."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from okx_trade.research.cli import build_parser, run
from okx_trade.research.store import FactorStore


@pytest.fixture(autouse=True)
def _ensure_factors_registered():
    """Reload factor submodules so @register_factor side effects fire even after
    test_registry.py's clear_registry() teardown wiped the global registry."""
    import importlib

    from okx_trade.research.registry import clear_registry

    clear_registry()
    for sub in ("momentum", "funding_oi", "basis", "volatility", "flow"):
        mod = importlib.import_module(f"okx_trade.research.factors.{sub}")
        importlib.reload(mod)
    yield


def test_parser_recognizes_all_subcommands() -> None:
    p = build_parser()
    for cmd in ("list", "fetch", "eval", "grade-all", "approve", "reject",
                "backtest-portfolio", "report", "wf-grade", "corr-matrix"):
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
              "--force", "--db", str(db), "--yaml", str(yml)])
    assert rc == 0
    store = FactorStore(db)
    approved = store.list_approved()
    assert len(approved) == 1 and approved[0]["id"] == "momentum_7d"
    cfg = yaml.safe_load(yml.read_text())
    assert any(
        pair[0] == "momentum_7d" and pair[1] == 0.25 for pair in cfg["factor_weights"]
    )


def test_reject_removes_factor_from_yaml(tmp_path) -> None:
    db = tmp_path / "z.db"; yml = tmp_path / "p.yaml"
    run(["approve", "--factor", "momentum_7d", "--weight", "0.25",
         "--force", "--db", str(db), "--yaml", str(yml)])
    rc = run(["reject", "--factor", "momentum_7d",
              "--db", str(db), "--yaml", str(yml)])
    assert rc == 0
    cfg = yaml.safe_load(yml.read_text())
    assert all(pair[0] != "momentum_7d" for pair in cfg.get("factor_weights", []))
