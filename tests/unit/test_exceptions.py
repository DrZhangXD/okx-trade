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


class TestInnerSCodeSurfacing:
    """OKXAPIError 应把 ``data[0].sCode/sMsg`` 拼进 str(err)。

    OKX "All operations failed"(outer code=1) 这类壳错误的真因藏在 data 里，
    上层只看 str(exc) 时必须能直接读到。
    """

    def test_no_data_no_suffix(self) -> None:
        # 向后兼容：无 data → 输出格式不变
        err = OKXAPIError("1", "All operations failed", endpoint="/api/v5/trade/order")
        assert str(err) == "[/api/v5/trade/order] OKX API error code=1: All operations failed"

    def test_data_list_with_scode(self) -> None:
        err = OKXAPIError(
            "1",
            "All operations failed",
            data=[{"sCode": "51008", "sMsg": "Order failed. Insufficient USDT"}],
            endpoint="/api/v5/trade/order",
        )
        assert "sCode=51008" in str(err)
        assert "Insufficient USDT" in str(err)

    def test_data_dict_with_scode(self) -> None:
        err = OKXAPIError(
            "1",
            "All operations failed",
            data={"sCode": "51020", "sMsg": "Order qty below min"},
        )
        assert "sCode=51020" in str(err)
        assert "Order qty below min" in str(err)

    def test_data_scode_zero_no_suffix(self) -> None:
        # data[0].sCode == "0" 是子项成功，不该当错误展示
        err = OKXAPIError(
            "1", "weird wrapper", data=[{"sCode": "0", "sMsg": "ok"}],
        )
        assert "sCode=" not in str(err)

    def test_subclass_inherits_surfacing(self) -> None:
        # OKXInsufficientBalance 继承 __init__，应自动获得同样的 sCode 拼接行为
        err = OKXInsufficientBalance(
            "51008",
            "insufficient",
            data=[{"sCode": "51008", "sMsg": "USDT balance 0"}],
        )
        assert "sCode=51008" in str(err)
        assert "USDT balance 0" in str(err)
