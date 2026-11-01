"""SQLite：索引状态与运行记录。

只存"哪些文档在哪一版被索引过"，不存正文与向量：

- documents：doc_id -> doc_hash（正文 sha256）、chunk_count、is_deleted。
  增量索引只比 doc_hash，正文改一个字节就重切这篇，没改的一篇都不碰。
- index_runs：每次 index_incremental 一行，供 M11 的 metrics/summary 取数，
  也让"二次运行 added=0"这类验收有据可查（不是靠 print 眼看的）。

为什么用 SQLite 而不是 JSON：需要事务化的 upsert + 并发读（服务进程读、
索引 CLI 写），且清单 §3 定的就是 SQLite，不引入外部数据库。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    source      TEXT,
    path        TEXT,
    title       TEXT,
    url         TEXT,
    doc_hash    TEXT NOT NULL,
    fetch_hash  TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    is_deleted  INTEGER NOT NULL DEFAULT 0,
    indexed_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_deleted ON documents(is_deleted);

CREATE TABLE IF NOT EXISTS index_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT,
    finished_at    TEXT,
    trigger        TEXT,
    full_rebuild   INTEGER NOT NULL DEFAULT 0,
    added          INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    deleted        INTEGER NOT NULL DEFAULT 0,
    skipped        INTEGER NOT NULL DEFAULT 0,
    chunks_total   INTEGER NOT NULL DEFAULT 0,
    chunks_written INTEGER NOT NULL DEFAULT 0,
    api_calls      INTEGER NOT NULL DEFAULT 0,
    cache_hits     INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0,
    cost_cny       REAL NOT NULL DEFAULT 0,
    elapsed_s      REAL NOT NULL DEFAULT 0,
    note           TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    """薄封装：每次操作开一个连接，索引器与 CLI 单线程用足够。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    # -- documents ---------------------------------------------
    def doc_rows(self, *, include_deleted: bool = False) -> dict[str, dict]:
        sql = "SELECT * FROM documents"
        if not include_deleted:
            sql += " WHERE is_deleted = 0"
        with self._conn() as c:
            return {r["doc_id"]: dict(r) for r in c.execute(sql)}

    def upsert_doc(
        self,
        *,
        doc_id: str,
        source: str,
        path: str,
        title: str,
        url: str,
        doc_hash: str,
        fetch_hash: str = "",
        chunk_count: int = 0,
    ) -> None:
        now = utc_now()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO documents (doc_id, source, path, title, url, doc_hash,
                                       fetch_hash, chunk_count, is_deleted, indexed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    source=excluded.source, path=excluded.path, title=excluded.title,
                    url=excluded.url, doc_hash=excluded.doc_hash, fetch_hash=excluded.fetch_hash,
                    chunk_count=excluded.chunk_count, is_deleted=0,
                    indexed_at=excluded.indexed_at, updated_at=excluded.updated_at
                """,
                (doc_id, source, path, title, url, doc_hash, fetch_hash, chunk_count, now, now),
            )

    def mark_deleted(self, doc_ids: Iterable[str]) -> int:
        ids = list(doc_ids)
        if not ids:
            return 0
        now = utc_now()
        with self._conn() as c:
            c.executemany(
                "UPDATE documents SET is_deleted = 1, chunk_count = 0, updated_at = ? WHERE doc_id = ?",
                [(now, d) for d in ids],
            )
        return len(ids)

    def clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM documents")

    def doc_stats(self) -> dict:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS docs, COALESCE(SUM(chunk_count), 0) AS chunks
                FROM documents WHERE is_deleted = 0
                """
            ).fetchone()
            runs = c.execute("SELECT COUNT(*) AS n FROM index_runs").fetchone()["n"]
        return {"docs": row["docs"], "chunks": row["chunks"], "runs": runs}

    # -- index_runs --------------------------------------------
    def record_run(self, report) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO index_runs (started_at, finished_at, trigger, full_rebuild,
                    added, updated, deleted, skipped, chunks_total, chunks_written,
                    api_calls, cache_hits, tokens, cost_cny, elapsed_s, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    getattr(report, "started_at", ""),
                    getattr(report, "finished_at", ""),
                    getattr(report, "trigger", "cli"),
                    int(bool(getattr(report, "full_rebuild", False))),
                    report.added,
                    report.updated,
                    report.deleted,
                    report.skipped,
                    report.chunks_total,
                    report.chunks_written,
                    report.api_calls,
                    report.cache_hits,
                    report.tokens,
                    report.cost_cny,
                    report.elapsed_s,
                    getattr(report, "note", ""),
                ),
            )
            return int(cur.lastrowid)

    def last_runs(self, limit: int = 5) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM index_runs ORDER BY run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
