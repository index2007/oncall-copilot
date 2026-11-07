"""日志可观测性：request_id 必须真的出现在每条日志里。

背景：2026-11 外部 code review 指出 setup_logging() 里
`logger.configure(extra={"request_id": "-"})` 预置了键，随后 filter 用 setdefault
永远不会覆盖它 → 日志里的 rid 恒为 "-"，同一次请求的日志串不起来。
这里锁住修复后的行为（D8 复核 §A1）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from app.obs.logging import (
    _inject_request_id,
    new_request_id,
    set_request_id,
    setup_logging,
)


@pytest.fixture()
def restore_logging():
    """每个用例自己改全局 sink，结束后恢复成默认配置，避免污染其他用例。"""
    yield
    logger.remove()
    set_request_id("-")
    setup_logging()


def test_console_filter_overrides_configured_default(restore_logging):
    logger.remove()
    setup_logging()
    lines: list[str] = []
    logger.add(lines.append, filter=_inject_request_id, format="{extra[request_id]}")

    rid = new_request_id()
    logger.info("boom")
    assert rid != "-"
    assert lines[-1].strip() == rid  # loguru 的 format 默认带换行


def test_file_sink_carries_request_id(restore_logging, tmp_path: Path):
    log_file = tmp_path / "app.log"
    logger.remove()
    setup_logging(log_file=log_file)

    rid = "cafe0123"
    set_request_id(rid)
    logger.info("扫描完成")

    text = log_file.read_text(encoding="utf-8")
    assert "扫描完成" in text
    assert f"| {rid} |" in text
    assert "| - |" not in text


def test_request_id_changes_per_request(restore_logging):
    logger.remove()
    setup_logging()
    lines: list[str] = []
    logger.add(lines.append, filter=_inject_request_id, format="{extra[request_id]}")

    first = new_request_id()
    logger.info("a")
    second = new_request_id()
    logger.info("b")

    assert [lines[0].strip(), lines[1].strip()] == [first, second]
    assert first != second
