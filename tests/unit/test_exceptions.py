"""异常体系与 classify_business_error 映射测试。"""
from __future__ import annotations

import pytest

from okx_trade.exceptions import (
    OKXAPIError,
    OKXAuthError,
    OKXError,
    OKXInsufficientBalance,
    OKXOrderNotFound,
    OKXRateLimitError,
    classify_business_error,
)


class TestHierarchy:
    def test_subclasses_share_root(self) -> None:
        for cls in (
            OKXAPIError,
            OKXAuthError,
            OKXInsufficientBalance,
            OKXOrderNotFound,
            OKXRateLimitError,
        ):
            assert issubclass(cls, OKXError)


class TestClassify:
    def test_auth_code(self) -> None:
        err = classify_business_error("50111", "Invalid sign")
        assert isinstance(err, OKXAuthError)
        assert err.code == "50111"

    def test_insufficient_balance(self) -> None:
        err = classify_business_error("51008", "insufficient")
        assert isinstance(err, OKXInsufficientBalance)

    def test_order_not_found(self) -> None:
        err = classify_business_error("51400", "order not found")
        assert isinstance(err, OKXOrderNotFound)

    def test_unknown_falls_back_to_generic(self) -> None:
        err = classify_business_error("99999", "weird")
        assert type(err) is OKXAPIError  # 不是子类
        assert err.code == "99999"

    def test_rate_limit_codes_raise_directly(self) -> None:
        # 限频码不返回，而是直接 raise（让 transport 走重试分支）
        with pytest.raises(OKXRateLimitError):
            classify_business_error("50011", "Too Many Requests")

    def test_endpoint_in_message(self) -> None:
        err = classify_business_error("51000", "msg", endpoint="POST /api/v5/trade/order")
        assert "POST /api/v5/trade/order" in str(err)


class TestRateLimitError:
    def test_retry_after_attached(self) -> None:
        err = OKXRateLimitError("rate limited", retry_after=2.5)
        assert err.retry_after == 2.5
