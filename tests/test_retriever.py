"""M5 检索器：RRF 数学与过滤兜底的单元测试（不碰网络、不碰 LanceDB）。

用假存储验证的是编排逻辑，不是 LanceDB/BM25 本身的检索质量——那部分在
真实索引上的 recall@20 实测里体现。
"""

from __future__ import annotations

import types

import pytest

from app.core.query_understanding import analyze
from app.core.retriever import RetrievalFailed, Retriever
from app.core.schemas import ScoredChunk
from app.index.vector_store import VectorStore


def make_settings(**overrides):
    base = dict(
        top_k_vec=20,
        top_k_bm25=20,
        top_k_fuse=20,
        rrf_k=60,
        sub_index_filter=True,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def row(chunk_id, source="k8s", score=1.0, text="正文"):
    return {
        "chunk_id": chunk_id,
        "doc_id": "d-" + chunk_id,
        "text": text,
        "heading_path": "H > H2",
        "url": "https://example.com",
        "anchor": "a",
        "order": 0,
        "token_count": 12,
        "source": source,
        "title": "标题",
        "doc_hash": "deadbeef",
        "score": score,
    }


def chunk(chunk_id, source="k8s", score=1.0, **kw):
    return ScoredChunk.from_row(row(chunk_id, source, score), **kw)


class FakeStore(VectorStore):
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def upsert(self, rows):
        return len(rows)

    def replace_doc(self, doc_id, rows):
        return len(rows)

    def delete_docs(self, doc_ids):
        return 0

    def count(self):
        return len(self.rows)

    def drop(self):
        return None

    def search(self, vector, k=20, where=None):
        self.calls.append({"k": k, "where": where})
        rows = self.rows
        if where:
            src = where.split("=", 1)[1].strip().strip("'")
            rows = [r for r in rows if r["source"] == src]
        return [dict(r) for r in rows[:k]]


class FakeBM25:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def search(self, query, k=20, source=None):
        self.calls.append({"query": query, "k": k, "source": source})
        rows = [r for r in self.rows if source is None or r["source"] == source]
        return [dict(r) for r in rows[:k]]


class FakeEmbedder:
    def __init__(self):
        self.texts = []

    def embed_one(self, text):
        self.texts.append(text)
        return [0.1, 0.2, 0.3]


def build(store_rows, bm25_rows, settings=None):
    store = FakeStore(store_rows)
    bm25 = FakeBM25(bm25_rows)
    emb = FakeEmbedder()
    return Retriever(store=store, bm25=bm25, embedder=emb, settings=settings or make_settings()), store, bm25, emb


# ---------------------------------------------------------------- RRF 数学
def test_rrf_scores_follow_rank():
    a = [chunk("c1"), chunk("c2")]
    b = [chunk("c2"), chunk("c3")]
    fused = Retriever.rrf_fuse(a, b, k=60, limit=10)
    scores = {c.chunk_id: c.score for c in fused}
    assert scores["c1"] == pytest.approx(1 / 61)
    assert scores["c2"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["c3"] == pytest.approx(1 / 62)
    assert [c.chunk_id for c in fused] == ["c2", "c1", "c3"]
    assert [c.rank for c in fused] == [1, 2, 3]


def test_rrf_ignores_duplicate_in_same_ranking():
    fused = Retriever.rrf_fuse([chunk("c1"), chunk("c1"), chunk("c2")], k=60, limit=10)
    scores = {c.chunk_id: c.score for c in fused}
    assert scores["c1"] == pytest.approx(1 / 61)  # 没白拿第二票
    assert scores["c2"] == pytest.approx(1 / 63)


def test_rrf_limit_and_rank_numbering():
    fused = Retriever.rrf_fuse([chunk("c1"), chunk("c2"), chunk("c3")], k=60, limit=2)
    assert len(fused) == 2
    assert [c.rank for c in fused] == [1, 2]


def test_rrf_merges_lane_scores():
    a = [chunk("c1", vector_score=0.83, vector_rank=1)]
    b = [chunk("c1", bm25_score=21.5, bm25_rank=1)]
    fused = Retriever.rrf_fuse(a, b, k=60, limit=5)
    assert fused[0].vector_score == pytest.approx(0.83)
    assert fused[0].bm25_score == pytest.approx(21.5)
    assert fused[0].vector_rank == 1
    assert fused[0].bm25_rank == 1
    assert fused[0].score == pytest.approx(2 / 61)


def test_rrf_empty():
    assert Retriever.rrf_fuse([], [], k=60, limit=5) == []


# ---------------------------------------------------------------- 召回编排
def test_search_text_includes_codes():
    retriever, store, bm25, emb = build([row("c1")], [row("c1")])
    plan = analyze("Pod OOMKilled exit code 137 一直重启", allow_llm=False)
    retriever.retrieve(plan)
    assert "exit code 137" in emb.texts[0]
    assert "OOMKilled" in emb.texts[0]
    assert "exit code 137" in bm25.calls[0]["query"]


def test_sub_index_filter_is_applied():
    retriever, store, bm25, _ = build([row("c1", "redis")], [row("c1", "redis")])
    plan = analyze("Redis 内存满了 maxmemory 报错", allow_llm=False)
    retriever.retrieve(plan)
    assert store.calls[0]["where"] == "source = 'redis'"
    assert bm25.calls[0]["source"] == "redis"


def test_no_filter_without_sub_index():
    retriever, store, bm25, _ = build([row("c1")], [row("c1")])
    plan = analyze("500 错误", allow_llm=False)
    retriever.retrieve(plan)
    assert store.calls[0]["where"] is None
    assert bm25.calls[0]["source"] is None


def test_fallback_fills_when_filtered_is_thin():
    # 子库只有 1 条，其它源有更多：不兜底就拿不到 20 条
    rows = [row("c1", "redis", score=0.9)] + [row("k%02d" % i, "k8s", score=0.5) for i in range(5)]
    retriever, store, bm25, _ = build(rows, [])
    plan = analyze("Redis 内存满了 maxmemory 报错", allow_llm=False)
    out = retriever.retrieve(plan)
    assert len(out) > 1
    assert retriever.last_stats["fallback"] is True
    assert store.calls[1]["where"] is None  # 第二次是不限源


def test_no_fallback_when_filtered_is_enough():
    rows = [row("r%02d" % i, "redis", score=1.0) for i in range(3)]
    retriever, store, bm25, _ = build(rows, [], settings=make_settings(top_k_fuse=3, top_k_vec=3, top_k_bm25=3))
    plan = analyze("Redis 内存满了 maxmemory 报错", allow_llm=False)
    out = retriever.retrieve(plan)
    assert len(out) == 3
    assert retriever.last_stats["fallback"] is False


def test_use_sub_index_false_disables_filter():
    retriever, store, _, _ = build([row("c1", "redis")], [])
    plan = analyze("Redis maxmemory 报错", allow_llm=False)
    retriever.retrieve(plan, use_sub_index=False)
    assert store.calls[0]["where"] is None


def test_results_sorted_and_ranked():
    rows = [row("c1", score=0.2), row("c2", score=0.9), row("c3", score=0.5)]
    retriever, _, _, _ = build(rows, [])
    plan = analyze("500 错误", allow_llm=False)
    out = retriever.retrieve(plan, limit=10)
    scores = [c.score for c in out]
    assert scores == sorted(scores, reverse=True)
    assert [c.rank for c in out] == list(range(1, len(out) + 1))


def test_stats_keys():
    retriever, _, _, _ = build([row("c1")], [row("c1")])
    plan = analyze("500 错误", allow_llm=False)
    retriever.retrieve(plan)
    stats = retriever.last_stats
    for key in ("query", "sub_index", "filtered", "fallback", "vector_candidates", "bm25_candidates", "fused", "vector_ms", "total_ms"):
        assert key in stats


def test_empty_index_returns_empty():
    retriever, _, _, _ = build([], [])
    plan = analyze("500 错误", allow_llm=False)
    assert retriever.retrieve(plan) == []
    # 空索引是"没命中"，不是"查不了"：不许降级、不许抛
    assert retriever.last_stats["degraded_legs"] == []
    assert retriever.last_stats["vector_error"] is None


# ---------------------------------------------------------------- 单路降级（D8/F1）
class BoomEmbedder:
    """向量路必挂：embedding 401（FatalEmbeddingError 的现实形态）。"""

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("embeddings HTTP 401")
        self.calls = 0

    def embed_one(self, text):
        self.calls += 1
        raise self.exc


class BoomBM25:
    def __init__(self, exc=None):
        self.exc = exc or FileNotFoundError("data/bm25.pkl 不存在")
        self.calls = 0

    def search(self, query, k=20, source=None):
        self.calls += 1
        raise self.exc


def build_faulty(*, vector=False, bm25=False, store_rows=None, bm25_rows=None):
    store = FakeStore(store_rows if store_rows is not None else [row("v1")])
    bm = BoomBM25() if bm25 else FakeBM25(bm25_rows if bm25_rows is not None else [row("b1")])
    emb = BoomEmbedder() if vector else FakeEmbedder()
    return Retriever(store=store, bm25=bm, embedder=emb, settings=make_settings()), bm


def test_vector_failure_falls_back_to_bm25():
    retriever, _ = build_faulty(vector=True)
    plan = analyze("Pod OOMKilled exit code 137", allow_llm=False)
    out = retriever.retrieve(plan)
    assert [c.chunk_id for c in out] == ["b1"]  # 另一路照常出候选
    assert retriever.last_stats["degraded_legs"] == ["vector"]
    assert retriever.last_stats["vector_error"].startswith("RuntimeError")
    assert retriever.last_stats["vector_candidates"] == 0
    # 降级标记走 last_stats，不往 chunk 上打（重排成功会把它冲掉）
    assert all(c.degraded is False for c in out)


def test_bm25_failure_falls_back_to_vector():
    retriever, _ = build_faulty(bm25=True)
    plan = analyze("Pod OOMKilled exit code 137", allow_llm=False)
    out = retriever.retrieve(plan)
    assert [c.chunk_id for c in out] == ["v1"]
    assert retriever.last_stats["degraded_legs"] == ["bm25"]
    assert retriever.last_stats["bm25_error"].startswith("FileNotFoundError")


def test_both_legs_failed_raises_retrieval_failed():
    retriever, _ = build_faulty(vector=True, bm25=True)
    plan = analyze("Pod OOMKilled exit code 137", allow_llm=False)
    with pytest.raises(RetrievalFailed) as exc_info:
        retriever.retrieve(plan)
    assert "向量路" in str(exc_info.value)
    assert "BM25 路" in str(exc_info.value)
    # 抛异常前也要有现场，否则调用方读不到哪一路挂了
    assert retriever.last_stats["degraded_legs"] == ["vector", "bm25"]
    assert retriever.last_stats["fused"] == 0


def test_empty_bm25_index_loaded_from_disk_marks_partial():
    """bm25.pkl 缺失时 BM25Store.load 只告警、返回空索引，search 静默返回 []。
    装配期标记 + 查询期留痕，才能把"没索引"与"没命中"分开（D8/F1）。"""
    retriever, _ = build_faulty(vector=False, bm25_rows=[])
    retriever.bm25_loaded_empty = True
    plan = analyze("Pod OOMKilled exit code 137", allow_llm=False)
    out = retriever.retrieve(plan)
    assert [c.chunk_id for c in out] == ["v1"]
    assert retriever.last_stats["degraded_legs"] == ["bm25"]
    assert retriever.last_stats["bm25_error"].startswith("EmptyBM25Index")


def test_hand_built_empty_bm25_is_not_degraded():
    """手工构造的空 BM25（单测常态）不算坏掉，否则每个空集用例都会被标降级。"""
    retriever, _store, _bm25, _emb = build([row("c1")], [])
    plan = analyze("500 错误", allow_llm=False)
    retriever.retrieve(plan)
    assert retriever.last_stats["degraded_legs"] == []


def test_leg_errors_reset_between_calls():
    """上一次失败的记录不能泄漏到下一次（否则会被误判成两路全挂）。"""
    retriever, _ = build_faulty(bm25=True)
    retriever.embedder = FakeEmbedder()
    plan = analyze("Pod OOMKilled exit code 137", allow_llm=False)
    retriever.retrieve(plan)
    assert retriever.last_stats["vector_error"] is None
    retriever.bm25 = FakeBM25([row("b1")])
    retriever.retrieve(plan)
    assert retriever.last_stats["degraded_legs"] == []
