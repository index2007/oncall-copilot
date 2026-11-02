"""辅助接口的离线单测：health / metrics / sources / admin / webhook。

全部离线：不连 LLM、不调 embedding / rerank、不开真 LanceDB 表。
外部依赖一律走两档注入——
- FastAPI 的 app.dependency_overrides 换掉 get_app_settings / get_db / get_diagnoser
  以及 sources 自己的 get_vector_store；
- 模块属性 monkeypatch 换掉 admin 的 index_incremental 与 run_in_threadpool。

数据目录用 tmp_path 现造（lancedb 目录 + bm25.pkl + content/*.md + 初始化过的 SQLite），
不读仓库里 data/ 的真实索引，测试可以在任何机器上重复。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin as admin_mod
from app.api import health as health_mod
from app.api import metrics as metrics_mod
from app.api import sources as sources_mod
from app.api import webhook as webhook_mod
from app.api.deps import get_app_settings, get_db, get_diagnoser
from app.config import Settings
from app.core.schemas import Cause, DiagnosisCard
from app.index.indexer import IndexReport
from app.obs.db import DB

# ---------------------------------------------------------------------------
# 公共脚手架
# ---------------------------------------------------------------------------
ALL_MODULES = (webhook_mod, sources_mod, admin_mod, metrics_mod, health_mod)


def make_settings(tmp_path, *, keys: bool = True) -> Settings:
    """临时 data/content 目录 + 假的 provider key（不读仓库 .env 的真实密钥）。"""
    return Settings(
        data_dir=str(tmp_path / "data"),
        content_dir=str(tmp_path / "content"),
        deepseek_api_key="test-deepseek-key" if keys else "",
        sf_api_key="test-sf-key" if keys else "",
    )


def make_app() -> FastAPI:
    """只挂本任务负责的 5 个 router，不 import app.main（避免被别人的模块牵连）。"""
    app = FastAPI()
    for module in ALL_MODULES:
        app.include_router(module.router)
    return app


def make_client(*, settings: Settings | None = None, db: DB | None = None, diagnoser=None) -> TestClient:
    app = make_app()
    if settings is not None:
        app.dependency_overrides[get_app_settings] = lambda: settings
    if db is not None:
        app.dependency_overrides[get_db] = lambda: db
    if diagnoser is not None:
        app.dependency_overrides[get_diagnoser] = lambda: diagnoser
    return TestClient(app)


def prepare_index(settings: Settings, *, pages: int = 2, chunks: int = 7) -> DB:
    """造一套"索引跑过一遍"的现场：lancedb 目录、bm25.pkl、语料、SQLite 行与 run。"""
    data = settings.data_path
    data.mkdir(parents=True, exist_ok=True)
    lance = data / "lancedb" / "chunks.lance"
    lance.mkdir(parents=True)
    (lance / "fragment.json").write_text("{}", encoding="utf-8")
    (data / "bm25.pkl").write_bytes(b"fake-bm25-index")
    for i in range(pages):
        path = settings.content_path / "k8s" / ("doc%d.md" % i)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 标题\n\n正文", encoding="utf-8")

    db = DB(data / "oncall.db")
    db.init_schema()
    db.upsert_doc(
        doc_id="d1",
        source="k8s",
        path="k8s/doc0.md",
        title="Pod 调度",
        url="https://example.com/pod",
        doc_hash="h1",
        chunk_count=chunks,
    )
    db.record_run(
        IndexReport(
            added=1,
            chunks_total=chunks,
            chunks_written=chunks,
            elapsed_s=9.5,
            finished_at="2025-11-01T10:00:00Z",
            trigger="cli",
        )
    )
    return db


# ---------------------------------------------------------------------------
# 1) health
# ---------------------------------------------------------------------------
def test_health_ok_reports_checks_and_index(tmp_path):
    settings = make_settings(tmp_path)
    db = prepare_index(settings)
    resp = make_client(settings=settings, db=db).get("/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    checks = body["checks"]
    assert checks["db"] == {"ok": True, "docs": 1, "chunks": 7}
    assert checks["vector_store"]["ok"] is True and checks["vector_store"]["entries"] >= 1
    assert checks["bm25"]["ok"] is True and checks["bm25"]["bytes"] > 0
    assert checks["content"]["ok"] is True and checks["content"]["pages"] == 2
    # 只报"配没配"，值绝不能出现在响应体里
    assert checks["provider_keys"] == {"ok": True, "deepseek": True, "sf": True}
    assert "test-deepseek-key" not in resp.text and "test-sf-key" not in resp.text

    index = body["index"]
    assert (index["docs"], index["chunks"], index["runs"]) == (1, 7, 1)
    assert index["last_run_at"] == "2025-11-01T10:00:00Z"
    assert index["last_run"]["elapsed_s"] == 9.5
    assert index["last_run"]["added"] == 1


def test_health_degraded_still_http_200(tmp_path):
    """关键项失败只改 status，不改状态码：探针不该因为降级把实例摘掉。"""
    settings = make_settings(tmp_path)
    db = prepare_index(settings)
    (settings.data_path / "bm25.pkl").unlink()

    resp = make_client(settings=settings, db=db).get("/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["bm25"]["ok"] is False
    assert body["checks"]["db"]["ok"] is True  # 其余项照常报


def test_health_db_not_initialized_is_degraded(tmp_path):
    """没跑过索引：documents 表不存在 → db 项 fail，但仍是 200 + 可读的错误信息。"""
    settings = make_settings(tmp_path, keys=False)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    db = DB(settings.data_path / "oncall.db")

    resp = make_client(settings=settings, db=db).get("/v1/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"]["ok"] is False
    assert "error" in body["checks"]["db"]
    assert body["index"] == {}
    assert body["checks"]["provider_keys"] == {"ok": False, "deepseek": False, "sf": False}


# ---------------------------------------------------------------------------
# 2) metrics
# ---------------------------------------------------------------------------
def test_metrics_summary_percentiles_and_index(tmp_path):
    settings = make_settings(tmp_path)
    db = prepare_index(settings)
    # 最近秩口径：3 条样本下 P50 = 第 2 条、P95 = 第 3 条（不插值）
    for latency, ttft, abstained in ((100.0, 11.0, False), (200.0, 22.0, False), (300.0, 33.0, True)):
        db.log_query(
            request_id="rid",
            endpoint="/v1/ask",
            query="Pod OOMKilled",
            latency_ms=latency,
            ttft_ms=ttft,
            abstained=abstained,
            completion_tokens=10,
            cost_cny=0.001,
        )

    resp = make_client(settings=settings, db=db).get("/v1/metrics/summary")
    assert resp.status_code == 200
    body = resp.json()

    assert body["window"] == 200
    assert body["requests"] == 3
    assert body["latency_p50_ms"] == 200.0
    assert body["latency_p95_ms"] == 300.0
    assert body["ttft_p50_ms"] == 22.0
    assert body["ttft_p95_ms"] == 33.0
    assert body["abstain_rate"] == pytest.approx(0.3333)
    assert body["by_endpoint"] == {"/v1/ask": 3}
    assert body["index"]["docs"] == 1
    assert body["index"]["last_run"]["elapsed_s"] == 9.5
    assert body["index"]["last_run"]["added"] == 1


def test_metrics_window_bounds(tmp_path):
    settings = make_settings(tmp_path)
    db = prepare_index(settings)
    db.log_query(request_id="rid", endpoint="/v1/ask", query="q", latency_ms=111.0)
    client = make_client(settings=settings, db=db)

    assert client.get("/v1/metrics/summary", params={"window": 1}).json()["requests"] == 1
    assert client.get("/v1/metrics/summary", params={"window": 2000}).status_code == 200
    # 越界交给 Query(ge=1, le=2000) → FastAPI 统一 422
    assert client.get("/v1/metrics/summary", params={"window": 0}).status_code == 422
    assert client.get("/v1/metrics/summary", params={"window": 2001}).status_code == 422
    assert client.get("/v1/metrics/summary", params={"window": -5}).status_code == 422


# ---------------------------------------------------------------------------
# 3) sources
# ---------------------------------------------------------------------------
class _FakeQuery:
    def __init__(self, row):
        self._row = row
        self.where_expr = ""

    def where(self, expr):
        self.where_expr = expr
        return self

    def limit(self, n):
        return self

    def to_list(self):
        return [dict(self._row)] if self._row else []


class _FakeTable:
    def __init__(self, row):
        self._row = row
        self.query = None

    def search(self):
        self.query = _FakeQuery(self._row)
        return self.query


class _FakeStore:
    """假的向量库：只实现 sources.py 用到的 table() → search().where().limit().to_list()。"""

    def __init__(self, row):
        self.table_obj = _FakeTable(row)

    def table(self):
        return self.table_obj


CHUNK = {
    "chunk_id": "c_abc1234567",
    "doc_id": "d1",
    "doc_hash": "h1",
    "text": "容器内存超过 limits 被 OOMKilled。" * 200,  # 5000+ 字符，验证不截断
    "heading_path": "Troubleshooting Pods > Pod 被 OOMKilled",
    "url": "https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/",
    "anchor": "pod-oomkilled",
    "order": 3,
    "token_count": 799,
    "source": "k8s",
    "title": "为容器管理资源",
    "vector": [0.0] * 8,
}


def _sources_client(settings: Settings, store) -> TestClient:
    app = make_app()
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[sources_mod.get_vector_store] = lambda: store
    return TestClient(app)


def test_sources_returns_full_text_and_metadata(tmp_path):
    settings = make_settings(tmp_path)
    store = _FakeStore(CHUNK)
    resp = _sources_client(settings, store).get("/v1/sources/c_abc1234567")

    assert resp.status_code == 200
    body = resp.json()
    assert body["chunk_id"] == "c_abc1234567"
    assert body["text"] == CHUNK["text"] and len(body["text"]) > 5000  # 原样返回，不截断
    assert body["url"] == CHUNK["url"]
    assert body["heading_path"] == CHUNK["heading_path"]
    assert body["anchor"] == "pod-oomkilled"
    assert (body["order"], body["token_count"]) == (3, 799)
    assert body["title"] == "为容器管理资源"
    assert "vector" not in body  # 向量不回给客户端
    assert "chunk_id = 'c_abc1234567'" in store.table_obj.query.where_expr


def test_sources_404_when_chunk_missing(tmp_path):
    settings = make_settings(tmp_path)
    resp = _sources_client(settings, _FakeStore(None)).get("/v1/sources/c_not_exist")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "chunk not found"}


def test_sources_404_when_vector_store_absent(tmp_path):
    settings = make_settings(tmp_path)
    resp = _sources_client(settings, None).get("/v1/sources/c_abc1234567")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "chunk not found"


# ---------------------------------------------------------------------------
# 4) admin
# ---------------------------------------------------------------------------
FAKE_REPORT = IndexReport(
    trigger="api",
    full_rebuild=True,
    added=2,
    updated=1,
    deleted=0,
    skipped=3,
    chunks_total=10,
    chunks_written=10,
    api_calls=1,
    cache_hits=4,
    tokens=500,
    cost_cny=0.1234,
    elapsed_s=9.876,
    note="dry-run：未写入任何存储",
)


def test_admin_index_maps_report_and_uses_threadpool(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    calls: dict = {}

    def fake_index_incremental(**kwargs):
        calls.update(kwargs)
        return FAKE_REPORT

    async def fake_threadpool(fn, *args, **kwargs):
        calls["threadpool"] = True
        return fn(*args, **kwargs)

    monkeypatch.setattr(admin_mod, "index_incremental", fake_index_incremental)
    monkeypatch.setattr(admin_mod, "run_in_threadpool", fake_threadpool)

    resp = make_client(settings=settings).post(
        "/v1/admin/index", json={"full": True, "dry_run": True, "limit": 3}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["added"] == 2 and body["updated"] == 1 and body["deleted"] == 0
    assert body["skipped"] == 3
    assert (body["chunks_total"], body["chunks_written"]) == (10, 10)
    assert (body["api_calls"], body["cache_hits"], body["tokens"]) == (1, 4, 500)
    assert body["cost_cny"] == pytest.approx(0.1234)
    assert body["elapsed_s"] == pytest.approx(9.88)
    # 同步执行的口径随响应一起返回（README 里也写了这条）
    assert "同步执行" in body["note"] and "dry-run" in body["note"]
    # 入参原样透传 + trigger 固定 api + 必须过线程池
    assert calls["threadpool"] is True
    assert calls["full"] is True and calls["dry_run"] is True and calls["limit"] == 3
    assert calls["trigger"] == "api"
    assert calls["settings"] is settings


def test_admin_index_failure_returns_500(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    def boom(**kwargs):
        raise RuntimeError("embedding 失败，未写入")

    monkeypatch.setattr(admin_mod, "index_incremental", boom)

    resp = make_client(settings=settings).post("/v1/admin/index", json={})

    assert resp.status_code == 500
    # 内部异常原文只进日志，不回给调用方（与 main.py 的 500 口径一致）
    assert resp.json()["detail"] == "internal error"
    assert "embedding 失败" not in resp.text


# ---------------------------------------------------------------------------
# 5) webhook
# ---------------------------------------------------------------------------
class _FakeDiagnoser:
    """假诊断器：记录收到的文本；fail_marker 命中时抛异常，用来验证单条隔离。"""

    def __init__(self, fail_marker: str = ""):
        self.texts: list[str] = []
        self.fail_marker = fail_marker

    def run(self, text, **_kwargs):
        self.texts.append(text)
        if self.fail_marker and self.fail_marker in text:
            raise RuntimeError("LLM 超时")
        card = DiagnosisCard(
            diagnosis_id="dg_test",
            summary="根因：容器内存 limit 过小",
            causes=[Cause(title="内存 limit 过小", confidence=0.8)],
        )
        return SimpleNamespace(card=card)


def _v4_payload(*alerts: dict) -> dict:
    return {
        "version": "4",
        "groupKey": "{alertname=\"KubePodOOMKilled\"}",
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "oncall-copilot",
        "groupLabels": {"alertname": "KubePodOOMKilled"},
        "commonLabels": {"severity": "critical"},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": list(alerts),
    }


def _firing_alert(**labels) -> dict:
    base = {
        "alertname": "KubePodOOMKilled",
        "namespace": "default",
        "pod": "app-1",
        "node": "node-1",
        "instance": "10.0.0.1:9100",
        "severity": "critical",
    }
    base.update(labels)
    return {
        "status": "firing",
        "labels": base,
        "annotations": {
            "summary": "Pod 因 OOM 被杀",
            "description": "容器内存超过 limit，exit code 137",
        },
        "startsAt": "2025-11-01T10:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph",
        "fingerprint": "fp-" + base["alertname"],
    }


def test_webhook_processes_firing_and_skips_resolved(tmp_path):
    settings = make_settings(tmp_path)
    db = prepare_index(settings)
    fake = _FakeDiagnoser(fail_marker="这条会炸")

    payload = _v4_payload(
        _firing_alert(),
        {
            "status": "firing",
            "labels": {"alertname": "BadAlert", "namespace": "kube-system", "severity": "warning"},
            "annotations": {"summary": "这条会炸", "description": "用来验证单条异常被隔离"},
            "fingerprint": "fp-bad",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "KubePodOOMKilled", "namespace": "default"},
            "annotations": {"summary": "已恢复"},
            "fingerprint": "fp-resolved",
        },
    )

    resp = make_client(settings=settings, db=db, diagnoser=fake).post("/webhook/alertmanager", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 1  # 只有成功那条算 accepted
    # skipped 只统计"没资格/没轮到"的：这里是 1 条 resolved；失败条已经尝试过诊断，不算 skipped
    assert body["skipped"] == 1
    assert len(body["results"]) == 2

    ok = body["results"][0]
    assert ok["status"] == "firing"
    assert ok["alertname"] == "KubePodOOMKilled" and ok["severity"] == "critical"
    assert ok["diagnosis_id"] == "dg_test"
    assert ok["abstained"] is False
    assert ok["causes"] == ["内存 limit 过小"]
    assert ok["summary"] == "根因：容器内存 limit 过小"
    assert ok["error"] == "" and ok["latency_ms"] >= 0

    bad = body["results"][1]
    assert bad["status"] == "error"
    assert bad["diagnosis_id"] == ""
    assert "RuntimeError" in bad["error"] and "LLM 超时" in bad["error"]

    # resolved 不调诊断；只有两条 firing 进了诊断器
    assert len(fake.texts) == 2
    assert all("已恢复" not in text for text in fake.texts)
    assert not any(r["fingerprint"] == "fp-resolved" for r in body["results"])

    # 诊断文本是自然语言，不是 JSON：拼进了 summary / description / 关键标签
    first = fake.texts[0]
    assert "告警 KubePodOOMKilled（级别 critical）：Pod 因 OOM 被杀" in first
    assert "现象与影响：容器内存超过 limit，exit code 137" in first
    assert "命名空间 default" in first
    assert "Pod app-1" in first
    assert "节点 node-1" in first
    assert "实例 10.0.0.1:9100" in first
    assert "{" not in first and '"' not in first

    # 两条都落了 query_log：成功 status=ok，失败 status=error（error_rate 有分母）
    rows = db.query_log_rows(10)
    assert len(rows) == 2
    assert {row["endpoint"] for row in rows} == {"/webhook/alertmanager"}
    assert {row["status"] for row in rows} == {"ok", "error"}


def test_webhook_caps_alerts_per_request(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    db = prepare_index(settings)
    fake = _FakeDiagnoser()
    monkeypatch.setattr(webhook_mod, "MAX_ALERTS", 1)

    payload = _v4_payload(
        _firing_alert(),
        _firing_alert(alertname="KubeNodeNotReady", pod=""),
    )

    body = make_client(settings=settings, db=db, diagnoser=fake).post(
        "/webhook/alertmanager", json=payload
    ).json()

    assert body["accepted"] == 1
    assert body["skipped"] == 1  # 第 2 条 firing 超过上限 → skipped
    assert len(body["results"]) == 1
    assert len(fake.texts) == 1  # 超上限的那条根本没调诊断
