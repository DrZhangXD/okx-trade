"""structlog 配置 helper.

调用 ``configure_logging("INFO")`` 一次后，再 ``structlog.get_logger("okx_trade")``
就能拿到统一格式的 logger。开发期用 console renderer 保持可读，生产环境可改 JSON。
"""
from __future__ import annotations

import logging
import sys
from typing import cast

import structlog


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """初始化 structlog + 标准 logging。幂等——多次调用以最后一次为准。"""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # 标准 logging 输出到 stderr
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=log_level,
    )

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "okx_trade") -> structlog.stdlib.BoundLogger:
    """返回带名字的 logger。未先调用 ``configure_logging`` 时使用 structlog 默认配置。"""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


__all__ = ["configure_logging", "get_logger"]
