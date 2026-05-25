"""OKX adapter 配置（msgspec Struct）。

NT TradingNode 用 msgspec.Struct 当配置 schema —— 必须是 immutable / typed / 无副作用。
我们扩展 ``LiveDataClientConfig`` / ``LiveExecClientConfig`` 加入 OKX 特有字段。

凭证支持两种来源：
1. 直接在 config 里传（适合从 NodeConfig 静态拼装）；
2. 留空 → 由 ``OKXSettings`` 从 ``.env`` 读取（运行时由 factory 处理）。
"""
from __future__ import annotations

from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig


class OKXDataClientConfig(LiveDataClientConfig):
    """OKX 行情客户端配置。

    Attributes:
        api_key: OKX API key（公共行情可不填；查 my-account 频道时必填）。
        api_secret: OKX API secret。
        passphrase: OKX API passphrase。
        is_demo: ``True`` 走 demo 端点；``False`` 走生产。
        http_proxy: HTTP 代理 URL（国内访问通常需要）。
        load_instruments: 启动时是否预加载所有 instruments 到 cache（默认 True）。
    """

    api_key: str | None = None
    api_secret: str | None = None
    passphrase: str | None = None
    is_demo: bool = True
    http_proxy: str | None = None
    load_instruments: bool = True
    option_ulys: list[str] | None = None
    """Restrict OPTION loading to these underlyings (e.g. ["BTC-USD"]).
    None = load all (~500 contracts; causes long startup)."""


class OKXExecClientConfig(LiveExecClientConfig):
    """OKX 执行客户端配置。

    Attributes:
        api_key / api_secret / passphrase: 必填（执行需要鉴权）。
        is_demo / http_proxy: 同 data client。
        td_mode: 默认 trade mode：``cross`` / ``isolated`` / ``cash``。
        pos_side_mode: ``net`` (单向持仓) 或 ``long_short`` (双向持仓)。
            必须与 OKX 账户设置一致。
    """

    api_key: str | None = None
    api_secret: str | None = None
    passphrase: str | None = None
    is_demo: bool = True
    http_proxy: str | None = None
    td_mode: str = "cross"
    pos_side_mode: str = "net"


__all__ = ["OKXDataClientConfig", "OKXExecClientConfig"]
