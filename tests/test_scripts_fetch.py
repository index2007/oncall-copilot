"""抓取脚本的退出码（2026-11 外部 review 复核项 A6）。

幂等重跑时 0 新增 0 失败必须 exit 0；旧实现写成"没新增就 exit 1"，
于是"内容都在库里、全部跳过"会被编排脚本读成抓取失败。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_corpus.py"


@pytest.fixture(scope="module")
def fetch_corpus():
    spec = importlib.util.spec_from_file_location("fetch_corpus_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_corpus_under_test"] = module  # dataclass 需要能回查 __module__
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("results", "dry_run", "expected"),
    [
        ([{"saved": 0, "failed": 0}], False, 0),   # 幂等重跑：全部跳过 = 成功
        ([{"saved": 0, "failed": 3}], False, 1),   # 真的抓失败且一篇没落 = 失败
        ([{"saved": 2, "failed": 3}], False, 0),   # 有进展就算成功，失败数已打在报告里
        ([{"saved": 0, "failed": 5}], True, 0),    # dry-run 从不写盘，退出码只看能不能跑完
    ],
)
def test_exit_code(fetch_corpus, results, dry_run, expected):
    assert fetch_corpus._exit_code(results, dry_run=dry_run) == expected
