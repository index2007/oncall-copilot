"""query_log 的可观测字段：分段耗时（stages）与拒答来源（abstain_sources）。

两列是 D6 审核补的（README 承诺 /v1/metrics/summary 能给分段 P50/P95，原来落不了库），
这里锁三件事：老库能就地加列、log_query 能把两列写进去、metrics_summary 能读出来。
"""

from __future__ import annotations

import json
import sqlite3

from app.obs.db import DB


OLD_SCHEMA = """
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
"""


def _columns(path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(query_log)")}
    finally:
        conn.close()


def test_init_schema_adds_new_columns_to_old_db(tmp_path):
    """老库（D5 时期）没有 timings_json / abstained_at，init_schema 要就地 ALTER。"""
    path = tmp_path / "oncall.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()
    assert "timings_json" not in _columns(path)

    DB(path).init_schema()

    cols = _columns(path)
    assert {"timings_json", "abstained_at"} <= cols
    # 再跑一次必须幂等（服务每次启动都会 init_schema）
    DB(path).init_schema()
    assert _columns(path) == cols


def test_log_query_stores_timings_and_abstained_at(tmp_path):
    db = DB(tmp_path / "oncall.db")
    db.init_schema()
    db.log_query(
        request_id="rid-1",
        endpoint="/v1/ask",
        query="OOMKilled",
        timings_json=json.dumps({"retrieve_ms": 12.0, "generate_ms": 1800.0}),
        abstained_at="gate",
    )
    row = db.query_log_rows(limit=1)[0]
    assert json.loads(row["timings_json"])["retrieve_ms"] == 12.0
    assert row["abstained_at"] == "gate"


def test_metrics_summary_reports_stage_percentiles_and_abstain_sources(tmp_path):
    db = DB(tmp_path / "oncall.db")
    db.init_schema()
    for i, (retrieve, generate, at) in enumerate(
        [(10.0, 1000.0, "gate"), (20.0, 2000.0, "generator"), (30.0, 3000.0, None)]
    ):
        db.log_query(
            request_id="rid-" + str(i),
            endpoint="/v1/ask",
            query="q" + str(i),
            timings_json=json.dumps({"retrieve_ms": retrieve, "generate_ms": generate}),
            abstained=at is not None,
            abstained_at=at,
        )
    summary = db.metrics_summary(window=10)
    assert summary["requests"] == 3
    # 最近秩：3 条取 50% -> 第 2 小，95% -> 最大
    assert summary["stages"]["retrieve_ms"] == {"p50": 20.0, "p95": 30.0, "count": 3}
    assert summary["stages"]["generate_ms"]["p95"] == 3000.0
    assert summary["abstain_sources"] == {"gate": 1, "generator": 1, "none": 1}


def test_metrics_summary_survives_garbage_timings(tmp_path):
    """手工塞进来的脏 JSON 不能让整个 /v1/metrics/summary 500。"""
    db = DB(tmp_path / "oncall.db")
    db.init_schema()
    db.log_query(request_id="r1", endpoint="/v1/ask", query="q", timings_json="{not json")
    db.log_query(request_id="r2", endpoint="/v1/ask", query="q", timings_json="")
    summary = db.metrics_summary(window=10)
    assert summary["requests"] == 2
    assert summary["stages"] == {}
