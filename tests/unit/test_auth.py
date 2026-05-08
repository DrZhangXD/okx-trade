"""签名测试。

OKX 官方文档没给出固定的"标准测试向量"，所以这里用：
1. 手工算出来的固定向量（便于回归——任何改动都会导致 hash 变化）；
2. Round-trip：相同输入产生相同输出，不同输入必产生不同输出；
3. Header 组装的形状测试。
"""
from __future__ import annotations

from okx_trade.auth import build_rest_headers, build_ws_login_args, sign

# ---------------------------------------------------------------------------
# 手工固定向量（防回归）
# ---------------------------------------------------------------------------

# 以下向量由 Python 标准库手工计算：
#   secret="testsecret"
#   ts="2024-01-01T00:00:00.000Z"
#   method="GET"
#   path="/api/v5/account/balance"
#   body=""
# 任何 sign() 的实现改动都应保持此值不变。
_FIXED_VECTOR_SIG = "P/3IJHHqzHqimtEh/8E6L/cgG0W3k0VkOeuwkh9qjzA="


class TestSign:
    def test_fixed_vector(self) -> None:
        result = sign(
            secret="testsecret",
            timestamp="2024-01-01T00:00:00.000Z",
            method="GET",
            request_path="/api/v5/account/balance",
        )
        assert result == _FIXED_VECTOR_SIG

    def test_method_case_insensitive(self) -> None:
        # method 内部统一转大写——传 "get" 应与 "GET" 相同
        a = sign("s", "ts", "get", "/p")
        b = sign("s", "ts", "GET", "/p")
        assert a == b

    def test_body_changes_signature(self) -> None:
        a = sign("s", "ts", "POST", "/p", body="")
        b = sign("s", "ts", "POST", "/p", body='{"x":1}')
        assert a != b

    def test_secret_changes_signature(self) -> None:
        a = sign("secret-1", "ts", "GET", "/p")
        b = sign("secret-2", "ts", "GET", "/p")
        assert a != b

    def test_timestamp_changes_signature(self) -> None:
        a = sign("s", "ts-1", "GET", "/p")
        b = sign("s", "ts-2", "GET", "/p")
        assert a != b


class TestBuildRestHeaders:
    def test_required_headers(self) -> None:
        h = build_rest_headers(
            api_key="key",
            secret="secret",
            passphrase="pass",
            method="GET",
            request_path="/api/v5/account/balance",
            timestamp="2024-01-01T00:00:00.000Z",
        )
        assert h["OK-ACCESS-KEY"] == "key"
        assert h["OK-ACCESS-PASSPHRASE"] == "pass"
        assert h["OK-ACCESS-TIMESTAMP"] == "2024-01-01T00:00:00.000Z"
        assert h["OK-ACCESS-SIGN"]  # 非空
        assert h["Content-Type"] == "application/json"

    def test_demo_header_added(self) -> None:
        h = build_rest_headers("k", "s", "p", "GET", "/p", is_demo=True, timestamp="ts")
        assert h["x-simulated-trading"] == "1"

    def test_live_no_demo_header(self) -> None:
        h = build_rest_headers("k", "s", "p", "GET", "/p", is_demo=False, timestamp="ts")
        assert "x-simulated-trading" not in h

    def test_auto_timestamp(self) -> None:
        h = build_rest_headers("k", "s", "p", "GET", "/p")
        ts = h["OK-ACCESS-TIMESTAMP"]
        # 形如 2026-04-19T12:34:56.789Z
        assert ts.endswith("Z")
        assert "T" in ts


class TestBuildWsLoginArgs:
    def test_shape(self) -> None:
        args = build_ws_login_args("k", "s", "p", timestamp="1700000000.000")
        assert set(args.keys()) == {"apiKey", "passphrase", "timestamp", "sign"}
        assert args["apiKey"] == "k"
        assert args["passphrase"] == "p"
        assert args["timestamp"] == "1700000000.000"
        # sign 是 base64 字符串，长度固定 44（HMAC-SHA256 的 32 字节 → 44 字符 base64）
        assert len(args["sign"]) == 44

    def test_auto_timestamp(self) -> None:
        args = build_ws_login_args("k", "s", "p")
        # 形如 "1713456789.123"——10 位整数 + 3 位小数
        ts = args["timestamp"]
        assert "." in ts
        assert float(ts) > 0
