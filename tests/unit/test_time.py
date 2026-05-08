"""utils.time 测试——只验证格式与时序，不假定具体时刻。"""
from __future__ import annotations

import re
import time

from okx_trade.utils.time import epoch_ms, iso_utc_now

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def test_iso_utc_now_format() -> None:
    s = iso_utc_now()
    assert _ISO_RE.match(s), f"format mismatch: {s!r}"


def test_epoch_ms_monotonic() -> None:
    a = epoch_ms()
    time.sleep(0.005)
    b = epoch_ms()
    assert b >= a
    assert isinstance(a, int)
