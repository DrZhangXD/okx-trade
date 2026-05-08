"""Shared pytest fixtures for okx-trade tests."""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture
def fake_credentials() -> dict[str, str]:
    """Dummy OKX credentials for unit tests (never hit network)."""
    return {
        "api_key": "test-api-key",
        "secret_key": "test-secret-key",
        "passphrase": "test-passphrase",
    }


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove all OKX_* env vars so config tests start from a known baseline."""
    for k in list(os.environ):
        if k.startswith("OKX_"):
            monkeypatch.delenv(k, raising=False)
    yield
