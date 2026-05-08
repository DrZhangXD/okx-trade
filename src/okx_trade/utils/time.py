"""时间工具：OKX 签名要求 ISO-8601 UTC 时间戳，毫秒精度。"""
from __future__ import annotations

import time
from datetime import datetime, timezone


def iso_utc_now() -> str:
    """返回当前 UTC 时间，格式：``2024-04-19T12:34:56.789Z``。

    OKX 签名 header ``OK-ACCESS-TIMESTAMP`` 要求这种格式。
    """
    now = datetime.now(timezone.utc)
    # %f 是 6 位微秒；OKX 文档要求 3 位毫秒
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def epoch_ms() -> int:
    """返回当前时间的毫秒级 Unix 时间戳。"""
    return int(time.time() * 1000)


__all__ = ["epoch_ms", "iso_utc_now"]
