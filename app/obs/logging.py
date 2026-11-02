"""loguru 日志与请求上下文。

一个服务里日志要解决三件事：① 每条日志都能追到某次请求（request_id）；
② 级别可配（生产 INFO、排查 DEBUG），不靠改代码；③ 控制台可读、文件可追。

为什么用 loguru 而不是 stdlib logging：本项目日志点不多，但每次都要带
request_id、stage、chunk 数这类上下文；loguru 的 bind + 结构化 kwargs 比
logging.Filter 少写一半代码。代价是它不在标准库（清单 §3 已列）。

request_id 用 contextvars 而不是传参：请求链路上有 pipeline、retriever、
generator 多层，层层传一个 id 会污染所有函数签名。
"""

from __future__ import annotations

import sys
import uuid
from contextvars import ContextVar
from pathlib import Path

from loguru import logger

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")

CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <7}</level> | "
    "<cyan>{extra[request_id]}</cyan> | {message}"
)


def new_request_id() -> str:
    """一次请求一个短 id（8 位十六进制），日志里能 grep 到同一次请求的全部行。"""
    rid = uuid.uuid4().hex[:8]
    _REQUEST_ID.set(rid)
    return rid


def current_request_id() -> str:
    return _REQUEST_ID.get()


def set_request_id(rid: str) -> None:
    _REQUEST_ID.set(rid)


def setup_logging(level: str = "INFO", log_file: Path | str | None = None) -> None:
    """幂等：重复调用只保留一个 handler，避免 uvicorn reload 后日志翻倍。"""
    logger.remove()
    logger.configure(extra={"request_id": "-"})
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=CONSOLE_FORMAT,
        filter=lambda record: record["extra"].setdefault("request_id", current_request_id()) or True,
        enqueue=False,
    )
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            path,
            level=level.upper(),
            rotation="10 MB",
            retention=5,
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {extra[request_id]} | {message}",
        )
