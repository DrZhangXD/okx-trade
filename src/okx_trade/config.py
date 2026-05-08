"""Settings loaded from environment / .env via pydantic-settings.

所有字段以 ``OKX_`` 前缀命名，例如 ``OKX_API_KEY``、``OKX_HTTP_PROXY``。

URL 字段留空时按 ``is_demo`` 自动选择官方 demo / live 端点。代理字段同时透传给
httpx（REST）和 websockets（WS），覆盖国内访问场景。
"""
from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# OKX 官方 v5 端点
_REST_LIVE = "https://www.okx.com"
_REST_DEMO = "https://www.okx.com"  # demo 与 live 同域名，靠 header x-simulated-trading=1 区分
_WS_PUBLIC_LIVE = "wss://ws.okx.com:8443/ws/v5/public"
_WS_PUBLIC_DEMO = "wss://wspap.okx.com:8443/ws/v5/public"
_WS_PRIVATE_LIVE = "wss://ws.okx.com:8443/ws/v5/private"
_WS_PRIVATE_DEMO = "wss://wspap.okx.com:8443/ws/v5/private"
_WS_BUSINESS_LIVE = "wss://ws.okx.com:8443/ws/v5/business"
_WS_BUSINESS_DEMO = "wss://wspap.okx.com:8443/ws/v5/business"


class OKXSettings(BaseSettings):
    """所有配置项。直接 ``OKXSettings()`` 会自动从环境/.env 加载。"""

    # ---- 凭证 ----
    api_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    passphrase: SecretStr = SecretStr("")

    # ---- 模式 ----
    is_demo: bool = True

    # ---- Endpoints（留空走默认值）----
    rest_base_url: str | None = None
    ws_public_url: str | None = None
    ws_private_url: str | None = None
    ws_business_url: str | None = None

    # ---- 网络（国内代理）----
    http_proxy: str | None = None
    https_proxy: str | None = None

    # ---- REST 行为 ----
    request_timeout_sec: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)

    # ---- 日志 ----
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_prefix="OKX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 派生属性：根据 is_demo 选 endpoint ----

    @property
    def effective_rest_base_url(self) -> str:
        if self.rest_base_url:
            return self.rest_base_url
        return _REST_DEMO if self.is_demo else _REST_LIVE

    @property
    def effective_ws_public_url(self) -> str:
        if self.ws_public_url:
            return self.ws_public_url
        return _WS_PUBLIC_DEMO if self.is_demo else _WS_PUBLIC_LIVE

    @property
    def effective_ws_private_url(self) -> str:
        if self.ws_private_url:
            return self.ws_private_url
        return _WS_PRIVATE_DEMO if self.is_demo else _WS_PRIVATE_LIVE

    @property
    def effective_ws_business_url(self) -> str:
        if self.ws_business_url:
            return self.ws_business_url
        return _WS_BUSINESS_DEMO if self.is_demo else _WS_BUSINESS_LIVE

    # ---- 解明文凭证（供 transport 使用，不要打日志）----

    def credentials(self) -> tuple[str, str, str]:
        """返回 (api_key, secret_key, passphrase) 明文三元组。"""
        return (
            self.api_key.get_secret_value(),
            self.secret_key.get_secret_value(),
            self.passphrase.get_secret_value(),
        )

    def has_credentials(self) -> bool:
        """是否配齐凭证；私有接口/WS 鉴权前用它做前置检查。"""
        return all(self.credentials())


__all__ = ["OKXSettings"]
