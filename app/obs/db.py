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

from app.obs.stats import percentile

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

CREATE TABLE IF NOT EXISTS query_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT,
    endpoint          TEXT,
    query             TEXT,
    plan_json         TEXT,
    hit_chunk_ids     TEXT,
    context_chunk_ids TEXT,
    answer_len        INTEGER NOT NULL DEFAULT 0,
    abstained         INTEGER NOT NULL DEFAULT 0,
    degraded          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'ok',
    ttft_ms           REAL,
    latency_ms        REAL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cny          REAL NOT NULL DEFAULT 0,
    created_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_query_log_created ON query_log(created_at);
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

    # -- query_log（M11 可观测） --------------------------------
    def log_query(
        self,
        *,
        request_id: str,
        endpoint: str,
        query: str,
        plan_json: str = "",
        hit_chunk_ids: str = "",
        context_chunk_ids: str = "",
        answer_len: int = 0,
        abstained: bool = False,
        degraded: bool = False,
        status: str = "ok",
        ttft_ms: float | None = None,
        latency_ms: float | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_cny: float = 0.0,
    ) -> int:
        """落一行请求日志。

        为什么失败也要落（status != ok）：metrics 里的 error_rate 要有分母，
        排查偶发 500 时最需要的就是那条失败记录。
        写日志本身绝不能把请求带崩——调用方负责 try/except。
        """
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO query_log (request_id, endpoint, query, plan_json, hit_chunk_ids,
                    context_chunk_ids, answer_len, abstained, degraded, status, ttft_ms,
                    latency_ms, prompt_tokens, completion_tokens, cost_cny, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    endpoint,
                    (query or "")[:2000],
                    plan_json or "",
                    hit_chunk_ids or "",
                    context_chunk_ids or "",
                    int(answer_len or 0),
                    1 if abstained else 0,
                    1 if degraded else 0,
                    status,
                    ttft_ms,
                    latency_ms,
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    float(cost_cny or 0.0),
                    utc_now(),
                ),
            )
            return int(cur.lastrowid)

    def query_log_rows(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM query_log ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    def metrics_summary(self, window: int = 200) -> dict:
        """近 window 次请求的汇总；百分位用最近秩（nearest-rank），不插值。

        为什么不用插值：样本量只有几十条时，插值会给出一个没出现过的耗时，
        被追问「这个 P95 是哪条请求」就答不上来；最近秩永远对应一条真实请求。
        """
        rows = self.query_log_rows(limit=window)
        total = len(rows)
        lat = [float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
        ttft = [float(r["ttft_ms"]) for r in rows if r.get("ttft_ms") is not None]
        costs = [float(r.get("cost_cny") or 0.0) for r in rows]
        comps = [int(r.get("completion_tokens") or 0) for r in rows]
        endpoints: dict[str, int] = {}
        for r in rows:
            key = str(r.get("endpoint") or "?")
            endpoints[key] = endpoints.get(key, 0) + 1
        abstained = sum(1 for r in rows if r.get("abstained"))
        degraded = sum(1 for r in rows if r.get("degraded"))
        errors = sum(1 for r in rows if str(r.get("status") or "ok") != "ok")
        return {
            "window": int(window),
            "requests": total,
            "ttft_p50_ms": percentile(ttft, 50),
            "ttft_p95_ms": percentile(ttft, 95),
            "latency_p50_ms": percentile(lat, 50),
            "latency_p95_ms": percentile(lat, 95),
            "abstain_rate": round(abstained / total, 4) if total else 0.0,
            "degraded_rate": round(degraded / total, 4) if total else 0.0,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "avg_cost_cny": round(sum(costs) / total, 6) if total else 0.0,
            "total_cost_cny": round(sum(costs), 6),
            "avg_completion_tokens": round(sum(comps) / total, 1) if total else 0.0,
            "by_endpoint": endpoints,
        }

