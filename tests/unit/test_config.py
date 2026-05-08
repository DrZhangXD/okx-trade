"""OKXSettings 加载与派生属性测试。"""
from __future__ import annotations

import pytest

from okx_trade.config import OKXSettings


@pytest.mark.usefixtures("clean_env")
class TestOKXSettings:
    def test_defaults(self) -> None:
        s = OKXSettings(_env_file=None)
        assert s.is_demo is True
        assert s.api_key.get_secret_value() == ""
        assert s.has_credentials() is False
        assert s.request_timeout_sec == 10.0
        assert s.max_retries == 3
        assert s.log_level == "INFO"

    def test_load_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OKX_API_KEY", "k")
        monkeypatch.setenv("OKX_SECRET_KEY", "s")
        monkeypatch.setenv("OKX_PASSPHRASE", "p")
        monkeypatch.setenv("OKX_IS_DEMO", "false")
        monkeypatch.setenv("OKX_HTTP_PROXY", "http://127.0.0.1:7890")
        s = OKXSettings(_env_file=None)
        assert s.credentials() == ("k", "s", "p")
        assert s.is_demo is False
        assert s.http_proxy == "http://127.0.0.1:7890"
        assert s.has_credentials() is True

    def test_demo_endpoints(self) -> None:
        s = OKXSettings(_env_file=None, is_demo=True)
        assert "wspap" in s.effective_ws_public_url
        assert "wspap" in s.effective_ws_private_url
        assert "wspap" in s.effective_ws_business_url

    def test_live_endpoints(self) -> None:
        s = OKXSettings(_env_file=None, is_demo=False)
        assert "wspap" not in s.effective_ws_public_url
        assert s.effective_ws_public_url.startswith("wss://ws.okx.com")

    def test_custom_endpoint_overrides_default(self) -> None:
        custom = "wss://my-mock-server/ws/public"
        s = OKXSettings(_env_file=None, ws_public_url=custom)
        assert s.effective_ws_public_url == custom

    def test_credentials_use_secret_str(self) -> None:
        # 凭证不会出现在 repr / str 中（防止打日志泄漏）
        s = OKXSettings(_env_file=None, api_key="my-secret-key")  # type: ignore[arg-type]
        assert "my-secret-key" not in repr(s)
        assert "my-secret-key" not in str(s)
