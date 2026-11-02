"""core/streaming.py 单测：帧序、TTFT、拒答不调 LLM、引用校验重生成、异常收口。

全部离线：假 retriever + 假 rerank + 假流式生成器，不联网、不开索引、不花钱。
真实链路（DeepSeek 真流）在 scripts/ 里单独验，不进单测。
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.config import Settings
from app.core import guard as guard_mod
from app.core.generator import StreamFailed
from app.core.pipeline import Diagnoser
from app.core.schemas import DiagnosisCard, ScoredChunk
from app.core.streaming import stream_diagnose


def make_settings(**kw: Any) -> Settings:
    """测试配置：默认关掉 Query 理解的 LLM 兜底，保证不联网。"""
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


class FakeRetriever:
    def __init__(self, chunks: list[ScoredChunk]):
        self.chunks = chunks
        self.calls: list[Any] = []

    def retrieve(self, q, *, limit=None, use_sub_index=None):  # noqa: ANN001
        self.calls.append(limit)
        return list(self.chunks)


def fake_rerank(query: str, chunks: list[ScoredChunk], top_n: int = 5) -> list[ScoredChunk]:
    return list(chunks)[:top_n]


def make_diagnoser(chunks: list[ScoredChunk]) -> Diagnoser:
    return Diagnoser(
        retriever=FakeRetriever(chunks),
        settings=make_settings(),
        rerank_fn=fake_rerank,
        generate_fn=lambda query, ctx: DiagnosisCard(),  # 流式路径不走它，只为防止意外联网
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


def split(text: str, n: int = 3) -> list[str]:
    size = max(1, len(text) // n)
    parts = [text[i * size : (i + 1) * size] for i in range(n - 1)]
    parts.append(text[(n - 1) * size :])
    return [p for p in parts if p]


def make_factory(pieces: list[str], calls: list[int], *, gap: float = 0.0):
    """假流式生成器：返回迭代器，记录被调次数；gap 用来制造可测的 TTFT 间隔。"""

    def _factory(query: str, chunks: list[ScoredChunk], *, settings: Settings | None = None):
        calls.append(len(chunks))

        def gen():
            for i, piece in enumerate(pieces):
                if i == 1 and gap:
                    time.sleep(gap)
                yield piece

        return gen()

    return _factory


def events(diagnoser: Diagnoser, text: str, factory: Any, **kw: Any) -> list[tuple[str, dict]]:
    return list(
        stream_diagnose(
            diagnoser,
            text,
            stream_factory=factory,
            request_id="abc12345",
            **kw,
        )
    )


def by_event(collected: list[tuple[str, dict]], name: str) -> list[dict]:
    return [data for event, data in collected if event == name]


# ---------------------------------------------------------------------------


def test_frame_order_and_ttft():
    """帧序 meta → citations → delta* → done；delta 拼接 == 原 JSON；ttft < latency。"""
    chunks = [make_chunk("c1"), make_chunk("c2", rerank=0.6)]
    diagnoser = make_diagnoser(chunks)
    calls: list[int] = []
    pieces = split(GOOD_JSON, 3)

    collected = events(diagnoser, "Pod 一直 OOMKilled 重启", make_factory(pieces, calls, gap=0.02))

    names = [event for event, _ in collected]
    assert names[0] == "meta"
    assert names[1] == "citations"
    assert names[-1] == "done"
    # 除首尾两帧与 delta 外，不允许有别的帧名
    assert set(names) == {"meta", "citations", "delta", "done"}

    deltas = by_event(collected, "delta")
    assert "".join(d["text"] for d in deltas) == GOOD_JSON
    assert calls == [2]  # 只开了一次流，且上下文是 2 块

    done = by_event(collected, "done")[0]
    assert done["ttft_ms"] is not None
    assert 0 <= done["ttft_ms"] < done["latency_ms"]
    assert done["regenerated"] is False
    assert done["card"]["abstained"] is False
    assert done["report"]["total"] >= 1
    assert done["report"]["invalid_ids"] == []

    # done 里的引用必须全部落在 citations 帧给出的白名单里
    allowed = {c["chunk_id"] for c in by_event(collected, "citations")[0]["context"]}
    assert {c["chunk_id"] for c in done["card"]["citations"]} <= allowed
    for cause in done["card"]["causes"]:
        assert {e["chunk_id"] for e in cause["evidence"]} <= allowed


def test_meta_carries_plan_and_request_id():
    diagnoser = make_diagnoser([make_chunk()])
    collected = events(diagnoser, "OOMKilled exit code 137", make_factory([GOOD_JSON], []))
    meta = by_event(collected, "meta")[0]
    assert meta["request_id"] == "abc12345"
    assert meta["plan"]["error_codes"] == ["exit code 137"]
    assert meta["plan"]["raw"] == "OOMKilled exit code 137"


def test_gate_abstain_skips_llm():
    """闸门一拒答：只有 meta → citations → done，一次 LLM 都没调。"""
    diagnoser = make_diagnoser([make_chunk("c1", rerank=0.1)])
    calls: list[int] = []
    factory = make_factory([GOOD_JSON], calls)

    collected = events(diagnoser, "今天天气怎么样", factory)

    assert [event for event, _ in collected] == ["meta", "citations", "done"]
    assert calls == []  # 一个 delta 都没生成，更没调 LLM

    done = by_event(collected, "done")[0]
    assert done["ttft_ms"] is None
    assert done["card"]["abstained"] is True
    assert done["card"]["causes"] == []
    assert done["card"]["abort_reason"] == "low_confidence"
    assert done["cost_cny"] == 0.0


def test_regenerate_once_then_degraded():
    """引用全错 → 重生成一次 → 仍错则 degraded=true（且只有一帧 done）。"""
    ghost = json.dumps(
        {
            "abstained": False,
            "summary": "编造的结论",
            "causes": [
                {
                    "title": "编造的根因",
                    "confidence": 0.9,
                    "explanation": "引用了一个不存在的块",
                    "evidence": [{"chunk_id": "ghost-1"}],
                }
            ],
            "citations": [{"chunk_id": "ghost-1"}],
        },
        ensure_ascii=False,
    )
    diagnoser = make_diagnoser([make_chunk("c1")])
    calls: list[int] = []

    collected = events(diagnoser, "OOMKilled", make_factory([ghost], calls))

    assert len(calls) == 2  # 第一次 + 重生成一次
    dones = by_event(collected, "done")
    assert len(dones) == 1
    done = dones[0]
    assert done["regenerated"] is True
    assert done["card"]["degraded"] is True
    assert done["card"]["causes"] == []
    assert "引用全部未通过校验" in done["card"]["summary"]
    assert done["card"]["abstained"] is True
    assert done["card"]["abort_reason"] == "citations_invalid"
    # 帧名仍然只有约定内的五种
    assert {event for event, _ in collected} <= {"meta", "citations", "delta", "done", "error"}


def test_valid_second_attempt_not_degraded():
    """第一次引用全错、第二次合法：regenerated=true 且卡片正常。"""
    ghost = json.dumps(
        {
            "summary": "编造的结论",
            "causes": [{"title": "编造", "evidence": [{"chunk_id": "ghost-1"}]}],
            "citations": [{"chunk_id": "ghost-1"}],
        },
        ensure_ascii=False,
    )
    diagnoser = make_diagnoser([make_chunk("c1")])
    calls: list[int] = []

    def factory(query: str, chunks: list[ScoredChunk], *, settings: Settings | None = None):
        calls.append(1)
        return iter([ghost] if len(calls) == 1 else [GOOD_JSON])

    collected = events(diagnoser, "OOMKilled", factory)

    done = by_event(collected, "done")[0]
    assert len(calls) == 2
    assert done["regenerated"] is True
    assert done["card"]["degraded"] is False
    assert done["card"]["causes"]


def test_exception_mid_stream_yields_error_without_done():
    """流到一半抛异常：收到 error 帧、done 不出现、不留半截。"""
    diagnoser = make_diagnoser([make_chunk("c1")])

    def factory(query: str, chunks: list[ScoredChunk], *, settings: Settings | None = None):
        def gen():
            yield '{"summary": "半截'
            raise StreamFailed("连接被重置")

        return gen()

    collected = events(diagnoser, "OOMKilled", factory)

    names = [event for event, _ in collected]
    assert names == ["meta", "citations", "delta", "error"]
    error = by_event(collected, "error")[0]
    assert error["stage"] == "generate"
    assert "StreamFailed" in error["message"]
    assert "done" not in names


def test_exception_before_first_token_yields_error():
    """开流阶段就失败（没配密钥 / 连接失败）：只有 meta → citations → error。"""
    diagnoser = make_diagnoser([make_chunk("c1")])

    def factory(query: str, chunks: list[ScoredChunk], *, settings: Settings | None = None):
        raise StreamFailed("DEEPSEEK_API_KEY 未配置，无法流式生成")

    collected = events(diagnoser, "OOMKilled", factory)

    assert [event for event, _ in collected] == ["meta", "citations", "error"]
    assert by_event(collected, "error")[0]["stage"] == "generate"


def test_json_invalid_degrades_instead_of_error():
    """模型流里吐的不是 JSON：done 里给 degraded 卡，abort_reason 写明 json_invalid。"""
    diagnoser = make_diagnoser([make_chunk("c1")])

    collected = events(diagnoser, "OOMKilled", make_factory(["好的，我分析一下：这不是 JSON。"], []))

    assert [event for event, _ in collected] == ["meta", "citations", "delta", "done"]
    done = by_event(collected, "done")[0]
    assert done["card"]["degraded"] is True
    assert str(done["card"]["abort_reason"]).startswith("json_invalid")
    assert done["card"]["citations"]  # 降级卡用原文片段兜底


def test_retrieve_failure_yields_error():
    """召回抛异常：error(stage=retrieve)，没有 citations/done。"""
    class BoomRetriever:
        def retrieve(self, q, *, limit=None, use_sub_index=None):  # noqa: ANN001
            raise RuntimeError("lancedb 打不开")

    diagnoser = Diagnoser(
        retriever=BoomRetriever(),
        settings=make_settings(),
        rerank_fn=fake_rerank,
        generate_fn=lambda query, ctx: DiagnosisCard(),
    )
    collected = events(diagnoser, "OOMKilled", make_factory([GOOD_JSON], []))

    assert [event for event, _ in collected] == ["meta", "error"]
    assert by_event(collected, "error")[0]["stage"] == "retrieve"


def test_done_report_can_be_reverified():
    """done 里的 report 与 guard.verify_citations 的结论一致（不是装饰字段）。"""
    diagnoser = make_diagnoser([make_chunk("c1")])
    collected = events(diagnoser, "OOMKilled", make_factory([GOOD_JSON], []))
    done = by_event(collected, "done")[0]
    card = DiagnosisCard.model_validate(done["card"])
    report = guard_mod.verify_citations(card, {"c1"})
    assert report.ok
    assert report.total == done["report"]["total"]
