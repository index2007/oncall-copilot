"""索引器的两条安全约束（2026-11 外部 review 复核项 A3）。

1. --full 与 --limit 互斥：否则一条调试命令就能把全库换成一个只含 N 篇的新库。
2. 全量重建的清空顺序：必须先清 documents 再动向量库，否则崩溃窗口会留下
   "documents 说全都最新、向量库却是空的"这种不可自愈的状态。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.index import indexer


def test_full_and_limit_are_mutually_exclusive():
    with pytest.raises(ValueError, match="limit"):
        indexer.index_incremental(full=True, limit=5)


def test_cli_rejects_full_with_limit(capsys):
    with pytest.raises(SystemExit) as exc:
        indexer.main(["--full", "--limit", "2"])
    assert exc.value.code == 2
    assert "--full 与 --limit 互斥" in capsys.readouterr().err


def test_reset_stores_clears_documents_before_dropping_vectors():
    calls: list[str] = []

    class FakeStore:
        def drop(self) -> None:
            calls.append("store.drop")

        def table(self, *, create: bool = False) -> Any:
            calls.append(f"store.table(create={create})")

    class FakeDB:
        def clear(self) -> None:
            calls.append("db.clear")

    indexer._reset_stores(FakeStore(), FakeDB())  # type: ignore[arg-type]

    assert calls == ["db.clear", "store.drop", "store.table(create=True)"]
