"""POST /v1/ask 与 /v1/diagnose 的接口层单测（全离线，不碰真索引/真 LLM）。

这里刻意不 import app.main：main 会把 admin / metrics / sources / webhook 等所有路由都装进来，
任何一个兄弟模块坏掉都会让本文件的失败看起来像流式的问题。测试只装自己关心的两个 router，
依赖用 dependency_overrides 注入假 Diagnoser 与假 DB。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ask as ask_mod
from app.api import diagnose as diagnose_mod
from app.api.deps import get_db, get_diagnoser
from app.config import Settings
from app.core.generator import StreamFailed
from app.core.pipeline import Diagnoser
from app.core.schemas import DiagnosisCard, ScoredChunk

QUERY = "Pod 一直 OOMKilled 重启，exit code 137"


def make_settings(**kw: Any) -> Settings:
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(update={"query_llm_enabled": False, **kw})


def make_chunk(cid: str = "c1", *, rerank: float | None = 0.9) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=cid,
        doc_id="k8s/" + cid,
        text="容器内存超过 limits 被 cgroup 杀掉，Reason: OOMKilled，exit code 137。",
        heading_path="Troubleshooting Pods > OOMKilled",
        url="https://kubernetes.io/zh-cn/docs/tasks/debug/",
        title="Pod OOMKilled",
        score=0.5,
        rerank_score=rerank,
    )


GOOD_PAYLOAD = {
    "abstained": False,
    "summary": "容器内存超过 limit，被 cgroup OOMKilled（exit 137）。",
    "causes": [
        {
            "title": "内存 limit 太小",
            "confidence": 0.82,
            "explanation": "JVM 堆 + 元空间总和超过 limit，被内核 OOM killer 杀掉。",
            "evidence": [{"chunk_id": "c1"}],
        }
    ],
    "verify_commands": ["kubectl describe pod app-1"],
    "actions": ["把 memory limit 调到 1Gi 以上"],
    "citations": [{"chunk_id": "c1", "title": "Pod OOMKilled"}],
}
GOOD_JSON = json.dumps(GOOD_PAYLOAD, ensure_ascii=False)


class FakeRetriever:
    def retrieve(self, q, *, limit=None, use_sub_index=None):  # noqa: ANN001
        return [make_chunk("c1"), make_chunk("c2", rerank=0.6)]


def fake_rerank(query: str, chunks: list[ScoredChunk], top_n: int = 5) -> list[ScoredChunk]:
    return list(chunks)[:top_n]


def make_diagnoser() -> Diagnoser:
    d = Diagnoser(
        retriever=FakeRetriever(),
        settings=make_settings(),
        rerank_fn=fake_rerank,
        generate_fn=lambda *a, **k: DiagnosisCard.model_validate(GOOD_PAYLOAD),
    )
    d.stream_fn = make_stream_fn([GOOD_JSON])  # type: ignore[attr-defined]
    return d


def make_stream_fn(pieces: list[str], *, gap: float = 0.0):
    def _factory(query: str, chunks: list[ScoredChunk], *, settings: Settings | None = None):
        def gen() -> Iterator[str]:
            for i, piece in enumerate(pieces):
                if i == 1 and gap:
                    time.sleep(gap)
                yield piece

        return gen()

    return _factory


class FakeDB:
    """只记录调用，不碰 SQLite：接口层关心的是"写了什么"，不是"怎么写的"。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_query(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


def make_client(diagnoser: Diagnoser, db: FakeDB, *, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(ask_mod.router)
    app.include_router(diagnose_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_diagnoser] = lambda: diagnoser
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def parse_sse(lines: Iterator[str]) -> list[tuple[str, dict]]:
    """按行解析 SSE：只要能拿到 event 名与 data，注释行/心跳行一律忽略。"""
    frames: list[tuple[str, dict]] = []
    event: str | None = None
    for raw in lines:
        line = raw.rstrip("\r")
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            frames.append((event or "", json.loads(line[len("data:") :].strip())))
        elif line == "":
            event = None
    return frames


# --------------------------------------------------------------------- 同步


def test_ask_sync_returns_card_and_logs():
    db = FakeDB()
    client = make_client(make_diagnoser(), db)

    resp = client.post("/v1/ask", json={"query": QUERY})

    assert resp.status_code == 200
    body = resp.json()
    assert body["card"]["summary"]
    assert body["card"]["abstained"] is False
    assert body["plan"]["error_codes"] == ["exit code 137"]
    assert "timings" in body

    assert len(db.rows) == 1
    row = db.rows[0]
    assert row["endpoint"] == "/v1/ask"
    assert row["status"] == "ok"
    assert row["query"] == QUERY
    assert row["context_chunk_ids"] == "c1,c2"
    assert row["hit_chunk_ids"] == "c1,c2"
    assert json.loads(row["plan_json"])["intent"] == "diagnose"


def test_diagnose_endpoint_sync_ok():
    db = FakeDB()
    client = make_client(make_diagnoser(), db)

    resp = client.post("/v1/diagnose", json={"query": QUERY})

    assert resp.status_code == 200
    assert db.rows[0]["endpoint"] == "/v1/diagnose"


def test_diagnose_rejects_stream_true():
    db = FakeDB()
    client = make_client(make_diagnoser(), db)

    resp = client.post("/v1/diagnose", json={"query": QUERY, "stream": True})

    assert resp.status_code == 400
    assert "/v1/ask" in resp.json()["detail"]
    assert db.rows == []  # 400 不该落一行"成功"日志


def test_sync_failure_returns_500_and_logs_error():
    db = FakeDB()
    diagnoser = make_diagnoser()

    def boom(*a: Any, **k: Any):
        raise RuntimeError("索引目录不存在")

    diagnoser.run = boom  # type: ignore[method-assign]
    client = make_client(diagnoser, db, raise_server_exceptions=False)

    resp = client.post("/v1/ask", json={"query": QUERY})

    assert resp.status_code == 500
    assert len(db.rows) == 1
    assert db.rows[0]["status"] == "error"
    assert db.rows[0]["endpoint"] == "/v1/ask"


# ------------------------------------------------------------------- 流式


def test_ask_stream_frames_and_headers():
    db = FakeDB()
    pieces = [GOOD_JSON[: len(GOOD_JSON) // 2], GOOD_JSON[len(GOOD_JSON) // 2 :]]
    diagnoser = make_diagnoser()
    diagnoser.stream_fn = make_stream_fn(pieces, gap=0.02)  # type: ignore[attr-defined]
    client = make_client(diagnoser, db)

    with client.stream("POST", "/v1/ask", json={"query": QUERY, "stream": True}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        frames = parse_sse(resp.iter_lines())

    names = [event for event, _ in frames]
    assert names[0] == "meta"
    assert names[1] == "citations"
    assert names[-1] == "done"

    deltas = [data["text"] for event, data in frames if event == "delta"]
    assert "".join(deltas) == GOOD_JSON

    done = [data for event, data in frames if event == "done"][0]
    assert done["ttft_ms"] is not None and done["ttft_ms"] < done["latency_ms"]
    assert done["regenerated"] is False
    assert done["card"]["citations"][0]["chunk_id"] == "c1"

    assert len(db.rows) == 1
    assert db.rows[0]["endpoint"] == "/v1/ask"
    assert db.rows[0]["status"] == "ok"
    assert db.rows[0]["context_chunk_ids"] == "c1,c2"
    assert db.rows[0]["ttft_ms"] is not None


def test_ask_stream_error_frame_and_log():
    db = FakeDB()
    diagnoser = make_diagnoser()

    def bad_factory(query: str, chunks: list[ScoredChunk], *, settings: Settings | None = None):
        raise StreamFailed("DEEPSEEK_API_KEY 未配置")

    diagnoser.stream_fn = bad_factory  # type: ignore[attr-defined]
    client = make_client(diagnoser, db)

    with client.stream("POST", "/v1/ask", json={"query": QUERY, "stream": True}) as resp:
        assert resp.status_code == 200  # 头已发出，错误只能走事件
        frames = parse_sse(resp.iter_lines())

    names = [event for event, _ in frames]
    assert names == ["meta", "citations", "error"]
    error = [data for event, data in frames if event == "error"][0]
    assert error["stage"] == "generate"
    assert "StreamFailed" in error["message"]

    assert db.rows[0]["status"] == "error"
    assert db.rows[0]["endpoint"] == "/v1/ask"


def test_ask_stream_abstain_has_no_delta():
    db = FakeDB()
    diagnoser = make_diagnoser()

    def low_rerank(query: str, chunks: list[ScoredChunk], top_n: int = 5) -> list[ScoredChunk]:
        return [make_chunk("c1", rerank=0.05)]

    diagnoser._rerank = low_rerank  # type: ignore[method-assign]
    client = make_client(diagnoser, db)

    with client.stream("POST", "/v1/ask", json={"query": "今天天气怎么样", "stream": True}) as resp:
        frames = parse_sse(resp.iter_lines())

    assert [event for event, _ in frames] == ["meta", "citations", "done"]
    done = [data for event, data in frames if event == "done"][0]
    assert done["ttft_ms"] is None
    assert done["card"]["abstained"] is True
    assert db.rows[0]["abstained"] is True

class BoomDB(FakeDB):
    """query_log 写失败（磁盘满 / SQLite locked）：旁路挂了不许影响响应。"""

    def log_query(self, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")


def test_query_log_failure_does_not_break_response():
    """D8/O1：落日志失败只警告，用户照常拿到卡。

    项目日志走 loguru（不是 stdlib logging），所以不能用 caplog，要自己挂 sink；
    断言的是"确实留了 warning"——光"没崩"不足以说明旁路失败被观测到。
    """
    from loguru import logger

    records: list[str] = []
    handle = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        db = BoomDB()
        client = make_client(make_diagnoser(), db)
        resp = client.post("/v1/ask", json={"query": QUERY})
    finally:
        logger.remove(handle)

    assert resp.status_code == 200
    assert resp.json()["card"]["summary"]
    assert any("query_log 写入失败" in text for text in records)
