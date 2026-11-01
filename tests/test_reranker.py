"""M6 重排器单测：假 HTTP client，不发真实请求。

覆盖四件事：
1. 请求契约：/v1/rerank 路径、四个 body 字段、top_n、Authorization 头、分批上限；
2. 成功路径：按 relevance_score 重排、写回 rerank_score / score / rank、保留上游元数据；
3. 降级路径：超时 / 5xx / 401 / 网络错误 / 空密钥 / 畸形响应 —— 不抛异常、保留 RRF 顺序、
   degraded=True、不伪造 rerank_score；
4. 成本与统计：token 累积、cost_cny、last_stats 字段。

不联网：全部走注入的 FakeClient；重试等待换成 wait=lambda _: 0，否则一个 5xx 用例要睡 3 秒。
"""

from __future__ import annotations

import httpx
import pytest

import app.core.reranker as reranker_module
from app.core.reranker import Reranker
from app.core.schemas import ScoredChunk

BASE_URL = "https://api.siliconflow.cn/v1"


# ------------------------------------------------------------------ 假 HTTP
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("" if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.handler(json, len(self.calls))


def make_reranker(client, **overrides):
    base = dict(
        api_key="sk-test-000000000000",
        base_url=BASE_URL,
        model="BAAI/bge-reranker-v2-m3",
        timeout=5.0,
        max_docs=20,
        price_cny_per_mtok=0.0,
        client=client,
        retry_attempts=3,
        retry_wait=lambda _: 0,
    )
    base.update(overrides)
    return Reranker(**base)


def chunk(chunk_id, text=None, score=0.01, **kw):
    data = dict(
        chunk_id=chunk_id,
        doc_id="d-" + chunk_id,
        text="正文 " + chunk_id if text is None else text,
        score=score,
        rrf_score=score,
    )
    data.update(kw)
    return ScoredChunk(**data)


def scoring_handler(rules, usage_tokens=42):
    """rules: [(子串, 分数)]，按文档文本匹配第一条；返回体按分数降序（真实 API 的行为）。"""

    def score_of(doc):
        for needle, value in rules:
            if needle in doc:
                return value
        return 0.0

    def handler(payload, call_index):
        results = [
            {"index": i, "relevance_score": score_of(doc)}
            for i, doc in enumerate(payload["documents"])
        ]
        results.sort(key=lambda r: r["relevance_score"], reverse=True)
        return FakeResponse(200, {"id": "r-%d" % call_index, "results": results,
                                  "usage": {"total_tokens": usage_tokens}})

    return handler


def failing_handler(exc):
    def handler(payload, call_index):
        raise exc

    return handler


# ---------------------------------------------------------------- 请求契约
def test_request_shape():
    seen = {}

    def handler(payload, call_index):
        seen.update(payload)
        return FakeResponse(200, {"results": [{"index": 0, "relevance_score": 0.5}]})

    client = FakeClient(handler)
    reranker = make_reranker(client)
    reranker.rerank("Pod Pending", [chunk("c1", "唯一候选正文")], top_n=5)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == BASE_URL + "/rerank"
    assert call["headers"]["Authorization"] == "Bearer sk-test-000000000000"
    assert call["timeout"] == 5.0
    assert set(seen) == {"model", "query", "documents", "top_n"}
    assert seen["model"] == "BAAI/bge-reranker-v2-m3"
    assert seen["query"] == "Pod Pending"
    assert seen["documents"] == ["唯一候选正文"]
    assert seen["top_n"] == 1


def test_reorders_by_relevance_score():
    # RRF 顺序里 c_low 排最后，但它才是真相关的
    chunks = [chunk("c_high", "无关甲", score=0.0164), chunk("c_mid", "无关乙", score=0.0159),
              chunk("c_low", "命中 OOMKilled", score=0.0154)]
    client = FakeClient(scoring_handler([("命中", 0.93)]))
    out = make_reranker(client).rerank("容器被杀", chunks, top_n=3)

    assert [c.chunk_id for c in out] == ["c_low", "c_high", "c_mid"]
    assert out[0].rerank_score == pytest.approx(0.93)
    assert out[0].score == pytest.approx(0.93)  # score 同步成重排分，下游只看 score
    assert [c.rank for c in out] == [1, 2, 3]
    assert all(c.degraded is False for c in out)


def test_top_n_truncates():
    chunks = [chunk("c%d" % i, "文档%d" % i, score=0.01 * (5 - i)) for i in range(5)]
    out = make_reranker(FakeClient(scoring_handler([]))).rerank("q", chunks, top_n=2)
    assert len(out) == 2
    assert [c.rank for c in out] == [1, 2]


def test_default_top_n_is_five():
    chunks = [chunk("c%d" % i, "文档%d" % i) for i in range(8)]
    out = make_reranker(FakeClient(scoring_handler([]))).rerank("q", chunks)
    assert len(out) == 5


def test_empty_chunks_skip_network():
    client = FakeClient(scoring_handler([]))
    reranker = make_reranker(client)
    assert reranker.rerank("q", []) == []
    assert client.calls == []
    assert reranker.last_stats["reason"] == "no_candidates"


def test_top_n_zero():
    client = FakeClient(scoring_handler([]))
    reranker = make_reranker(client)
    assert reranker.rerank("q", [chunk("c1")], top_n=0) == []
    assert client.calls == []
    assert reranker.last_stats["reason"] == "top_n=0"


def test_single_chunk_still_scored():
    # 只有 1 条也要打分：guard.should_abstain 靠 rerank_score 判定，跳过就没有分
    client = FakeClient(scoring_handler([("命中", 0.8)]))
    out = make_reranker(client).rerank("q", [chunk("c1", "命中")])
    assert len(client.calls) == 1
    assert out[0].rerank_score == pytest.approx(0.8)


def test_empty_query_degrades_without_call():
    client = FakeClient(scoring_handler([]))
    reranker = make_reranker(client)
    out = reranker.rerank("   ", [chunk("c1")])
    assert client.calls == []
    assert out[0].degraded is True
    assert reranker.last_stats["reason"] == "empty_query"


# ---------------------------------------------------------------- 降级路径
def test_5xx_retries_then_degrades():
    client = FakeClient(lambda payload, n: FakeResponse(503, text="upstream busy"))
    reranker = make_reranker(client)
    chunks = [chunk("c1", score=0.02), chunk("c2", score=0.01)]
    out = reranker.rerank("q", chunks)

    assert len(client.calls) == 3  # tenacity 指数退避重试 3 次
    assert [c.chunk_id for c in out] == ["c1", "c2"]  # 保留 RRF 顺序
    assert all(c.degraded is True for c in out)
    assert all(c.rerank_score is None for c in out)  # 不伪造重排分
    assert [c.rank for c in out] == [1, 2]
    assert reranker.last_stats["degraded"] is True
    assert "503" in reranker.last_stats["reason"]


def test_timeout_degrades():
    client = FakeClient(failing_handler(httpx.ReadTimeout("read timeout")))
    reranker = make_reranker(client)
    out = reranker.rerank("q", [chunk("c1")])
    assert len(client.calls) == 3
    assert out[0].degraded is True


def test_network_error_degrades():
    client = FakeClient(failing_handler(httpx.ConnectError("connection refused")))
    reranker = make_reranker(client)
    chunks = [chunk("c1", score=0.03), chunk("c2", score=0.02)]
    out = reranker.rerank("q", chunks)
    assert len(client.calls) == 3
    assert [c.chunk_id for c in out] == ["c1", "c2"]
    assert "ConnectError" in reranker.last_stats["reason"]


def test_401_is_fatal_no_retry():
    client = FakeClient(lambda payload, n: FakeResponse(401, text='{"message":"invalid api key"}'))
    reranker = make_reranker(client)
    out = reranker.rerank("q", [chunk("c1")])
    assert len(client.calls) == 1  # 重试只会多烧额度
    assert out[0].degraded is True
    assert "401" in reranker.last_stats["reason"]


def test_missing_api_key_no_network():
    client = FakeClient(scoring_handler([]))
    reranker = make_reranker(client, api_key="")
    out = reranker.rerank("q", [chunk("c1")])
    assert client.calls == []
    assert out[0].degraded is True
    assert reranker.last_stats["reason"].startswith("fatal")
    assert "SF_API_KEY" in reranker.last_stats["reason"]


def test_empty_results_degrades():
    client = FakeClient(lambda payload, n: FakeResponse(200, {"results": []}))
    reranker = make_reranker(client)
    out = reranker.rerank("q", [chunk("c1")])
    assert len(client.calls) == 3
    assert out[0].degraded is True


def test_missing_index_degrades():
    # 2 条候选只回 1 条：少一条就没法解释 rank，按失败处理
    client = FakeClient(lambda payload, n: FakeResponse(200, {"results": [{"index": 0, "relevance_score": 0.9}]}))
    reranker = make_reranker(client)
    out = reranker.rerank("q", [chunk("c1"), chunk("c2")])
    assert out == [c.model_copy(update={"degraded": True, "rank": i}) for i, c in enumerate(out, start=1)]


def test_non_json_body_degrades():
    client = FakeClient(lambda payload, n: FakeResponse(200, None))
    out = make_reranker(client).rerank("q", [chunk("c1")])
    assert out[0].degraded is True


def test_degraded_respects_top_n_and_ranks():
    client = FakeClient(lambda payload, n: FakeResponse(500, text="boom"))
    chunks = [chunk("c%d" % i, score=0.01 * (5 - i)) for i in range(5)]
    out = make_reranker(client).rerank("q", chunks, top_n=2)
    assert [c.chunk_id for c in out] == ["c0", "c1"]
    assert [c.rank for c in out] == [1, 2]


# ---------------------------------------------------------------- 分批
def test_batching_splits_requests():
    client = FakeClient(scoring_handler([("命中", 0.9)]))
    chunks = [chunk("c%d" % i) for i in range(5)]
    make_reranker(client, max_docs=2).rerank("q", chunks, top_n=5)

    assert len(client.calls) == 3
    assert [len(c["json"]["documents"]) for c in client.calls] == [2, 2, 1]
    assert [c["json"]["top_n"] for c in client.calls] == [2, 2, 1]


def test_batches_merge_into_global_order():
    # 最相关的那条落在第 3 批，也要能排到全局第一
    chunks = [chunk("c%d" % i, "无关%d" % i, score=0.01 * (6 - i)) for i in range(5)]
    chunks[4] = chunk("c4", "命中 关键文档", score=0.005)
    client = FakeClient(scoring_handler([("命中", 0.95)]))
    out = make_reranker(client, max_docs=2).rerank("q", chunks, top_n=5)
    assert out[0].chunk_id == "c4"
    assert out[0].rerank_score == pytest.approx(0.95)
    assert len(client.calls) == 3


def test_batch_failure_degrades_whole_call():
    # 第 2 批挂掉：不混用两次调用口径的分数，整次降级
    def handler(payload, call_index):
        if call_index == 1:
            return FakeResponse(200, {"results": [
                {"index": i, "relevance_score": 0.9} for i in range(len(payload["documents"]))
            ]})
        raise httpx.ConnectError("second batch down")

    client = FakeClient(handler)
    chunks = [chunk("c%d" % i, "文档%d" % i) for i in range(4)]
    out = make_reranker(client, max_docs=2).rerank("q", chunks, top_n=4)
    assert all(c.degraded is True for c in out)
    assert all(c.rerank_score is None for c in out)
    assert [c.chunk_id for c in out] == ["c0", "c1", "c2", "c3"]


# ---------------------------------------------------------------- 细节与成本
def test_metadata_preserved():
    chunks = [chunk("c1", "命中", score=0.02, vector_score=0.83, vector_rank=2, bm25_score=21.5,
                    bm25_rank=4, heading_path="A > B", url="https://example.com/x", source="k8s")]
    out = make_reranker(FakeClient(scoring_handler([("命中", 0.77)]))).rerank("q", chunks)
    got = out[0]
    assert got.vector_score == pytest.approx(0.83)
    assert got.bm25_score == pytest.approx(21.5)
    assert got.vector_rank == 2
    assert got.bm25_rank == 4
    assert got.heading_path == "A > B"
    assert got.url == "https://example.com/x"
    assert got.source == "k8s"


def test_tie_break_is_deterministic():
    handler = lambda payload, n: FakeResponse(200, {"results": [
        {"index": i, "relevance_score": 0.5} for i in range(len(payload["documents"]))
    ]})
    out = make_reranker(FakeClient(handler)).rerank("q", [chunk("c_b"), chunk("c_a", score=0.009), chunk("c_c")])
    assert [c.chunk_id for c in out] == ["c_a", "c_b", "c_c"]


def test_alternative_score_field():
    handler = lambda payload, n: FakeResponse(200, {"results": [{"index": 0, "score": 0.66}]})
    out = make_reranker(FakeClient(handler)).rerank("q", [chunk("c1")])
    assert out[0].rerank_score == pytest.approx(0.66)


def test_cost_from_tokens():
    client = FakeClient(scoring_handler([], usage_tokens=1000))
    reranker = make_reranker(client, price_cny_per_mtok=2.0)
    reranker.rerank("q", [chunk("c1")])
    assert reranker.tokens == 1000
    assert reranker.cost_cny == pytest.approx(0.002)
    assert reranker.last_stats["tokens"] == 1000
    assert reranker.last_stats["cost_cny"] == pytest.approx(0.002)


def test_stats_keys():
    client = FakeClient(scoring_handler([]))
    reranker = make_reranker(client)
    reranker.rerank("q", [chunk("c1"), chunk("c2")])
    stats = reranker.last_stats
    for key in ("model", "degraded", "reason", "candidates", "returned", "batches",
                "api_calls", "tokens", "cost_cny", "request_ms", "total_ms"):
        assert key in stats
    assert stats["candidates"] == 2
    assert stats["returned"] == 2
    assert stats["batches"] == 1
    assert stats["degraded"] is False


def test_module_level_rerank_uses_reranker(monkeypatch):
    calls = {}

    class FakeReranker:
        def rerank(self, query, chunks, top_n=5):
            calls.update({"query": query, "chunks": chunks, "top_n": top_n})
            return ["sentinel"]

    monkeypatch.setattr(reranker_module, "get_reranker", lambda settings=None: FakeReranker())
    out = reranker_module.rerank("q", [chunk("c1")], top_n=2)
    assert out == ["sentinel"]
    assert calls["query"] == "q"
    assert calls["top_n"] == 2
