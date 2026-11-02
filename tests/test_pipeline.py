"""pipeline 编排层单测：闸门、降级、重生成、异常兜底。

全用假依赖（假 retriever + 假生成函数），不联网、不读密钥、不依赖索引。
真实链路验证放在 scripts/d4_e2e.py。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.core.pipeline import Diagnoser, StageTiming
from app.core.schemas import Cause, Citation, DiagnosisCard, Evidence, ScoredChunk


def make_chunk(cid: str, *, score: float = 0.016, rerank: float | None = None, degraded: bool = False) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=cid,
        doc_id="k8s/" + cid,
        text="容器内存超过 limits 被 cgroup 杀掉，Reason: OOMKilled，exit code 137。",
        heading_path="Troubleshooting Pods > OOMKilled",
        url="https://kubernetes.io/zh-cn/docs/tasks/debug/",
        score=score,
        rerank_score=rerank,
        degraded=degraded,
    )


class FakeRetriever:
    """只实现 pipeline 用到的那一个方法。"""

    def __init__(self, chunks: list[ScoredChunk]):
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, q, *, limit=None, use_sub_index=None):  # noqa: ANN001
        self.calls.append({"query": getattr(q, "search_text", ""), "limit": limit})
        return list(self.chunks)


def fake_generate_factory(cards: list[DiagnosisCard]):
    calls: list[int] = []

    def _gen(query: str, chunks: list[ScoredChunk]) -> DiagnosisCard:
        calls.append(len(chunks))
        return cards[len(calls) - 1]

    _gen.calls = calls  # type: ignore[attr-defined]
    return _gen


def settings(**kw: Any) -> Settings:
    """测试用配置：默认关掉 Query 理解里的 LLM 兜底，保证不联网。"""
    base = Settings(_env_file=None)  # type: ignore[call-arg]
    return base.model_copy(update={"query_llm_enabled": False, **kw})


def fake_rerank(chunks_are_ctx: bool = True):
    """假重排：直接返回传入的候选（分数已在构造时写好），不联网。"""

    def _rerank(query: str, chunks: list[ScoredChunk], top_n: int = 5) -> list[ScoredChunk]:
        return list(chunks)[:top_n]

    return _rerank


GOOD_CARD = DiagnosisCard(
    summary="容器内存超过 limits，被 cgroup OOM 杀掉",
    causes=[Cause(title="内存超 limits", confidence=0.9, evidence=[Evidence(chunk_id="c_1")])],
    citations=[Citation(chunk_id="c_1", title="Troubleshooting Pods")],
)
BAD_CARD = DiagnosisCard(
    summary="节点磁盘满了（资料里没有这条）",
    causes=[Cause(title="磁盘满", confidence=0.4, evidence=[Evidence(chunk_id="c_fake")])],
)


def test_gate_abstains_on_empty_context():
    gen = fake_generate_factory([GOOD_CARD])
    d = Diagnoser(FakeRetriever([]), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("随便问一句")
    assert res.card.abstained is True
    assert res.abstained_at == "gate"
    assert res.card.causes == []
    assert gen.calls == []  # 不调 LLM
    assert res.card.latency_ms >= 0


def test_gate_abstains_below_threshold():
    ctx = [make_chunk("c_1", rerank=0.10), make_chunk("c_2", rerank=0.05)]
    gen = fake_generate_factory([GOOD_CARD])
    d = Diagnoser(FakeRetriever(ctx), settings(abstain_threshold=0.35), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("服务器很慢")
    assert res.card.abstained is True
    assert "0.100" in res.card.summary
    assert gen.calls == []


def test_threshold_is_strict_less_than():
    """等于阈值必须放行（否则边界样本会被误拒）。"""
    ctx = [make_chunk("c_1", rerank=0.35)]
    gen = fake_generate_factory([GOOD_CARD])
    d = Diagnoser(FakeRetriever(ctx), settings(abstain_threshold=0.35), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 起不来")
    assert res.card.abstained is False
    assert gen.calls == [1]


def test_degraded_rerank_skips_gate():
    """重排降级时分数退回 RRF 量级，闸门必须跳过，否则 100% 误拒。"""
    ctx = [make_chunk("c_1", score=0.0164, rerank=None, degraded=True)]
    gen = fake_generate_factory([GOOD_CARD])
    d = Diagnoser(FakeRetriever(ctx), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 起不来")
    assert res.abstained is False
    assert gen.calls == [1]
    assert res.card.degraded is True  # 降级要标在卡上


def test_verify_failure_regenerates_once_then_degrades():
    ctx = [make_chunk("c_1", rerank=0.9)]
    gen = fake_generate_factory([BAD_CARD, BAD_CARD])
    d = Diagnoser(FakeRetriever(ctx), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 起不来")
    assert len(gen.calls) == 2  # 重生成一次
    assert res.regenerated is True
    assert res.card.degraded is True
    assert res.card.causes == []
    assert "c_fake" in res.card.summary
    assert res.report is not None and res.report.ok is False


def test_verify_failure_then_success():
    ctx = [make_chunk("c_1", rerank=0.9)]
    gen = fake_generate_factory([BAD_CARD, GOOD_CARD])
    d = Diagnoser(FakeRetriever(ctx), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 起不来")
    assert len(gen.calls) == 2
    assert res.card.degraded is False
    assert res.report is not None and res.report.ok is True


def test_generate_exception_becomes_degraded_card():
    ctx = [make_chunk("c_1", rerank=0.9)]

    def boom(query: str, chunks: list[ScoredChunk]) -> DiagnosisCard:
        raise RuntimeError("boom")

    d = Diagnoser(FakeRetriever(ctx), settings(), rerank_fn=fake_rerank(), generate_fn=boom)
    res = d.run("Pod 起不来")
    assert res.card.degraded is True
    assert "boom" in res.card.summary


def test_timings_and_summary_row():
    ctx = [make_chunk("c_1", rerank=0.9)]
    gen = fake_generate_factory([GOOD_CARD])
    d = Diagnoser(FakeRetriever(ctx), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 起不来")
    row = res.summary_row()
    for key in ("t_retrieve_ms", "t_rerank_ms", "t_generate_ms", "t_total_ms"):
        assert key in row
        assert row[key] >= 0
    assert row["citations"] == 1


def test_stage_timing_defaults_zero():
    t = StageTiming()
    assert t.as_dict()["total_ms"] == 0.0


def test_llm_abstain_marks_generator_stage():
    """模型自己判断证据不足而拒答时，abstained_at 要能区分出是生成侧，不是闸门。"""
    card = DiagnosisCard(abstained=True, summary="证据不足", causes=[], abort_reason="insufficient_evidence")
    gen = fake_generate_factory([card])
    d = Diagnoser(FakeRetriever([make_chunk("c_1", rerank=0.9)]), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 一直 Pending")
    assert res.card.abstained is True
    assert res.abstained_at == "generator"
    assert len(gen.calls) == 1


def test_citations_invalid_stays_abstained_and_marks_degraded():
    """ADR-5②：引用全落空是拒答；同时置 degraded，才能与"检索不到"分开统计。"""
    card = DiagnosisCard(abstained=True, summary="引用无效", causes=[], abort_reason="citations_invalid")
    gen = fake_generate_factory([card])
    d = Diagnoser(FakeRetriever([make_chunk("c_1", rerank=0.9)]), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 一直 Pending")
    assert res.card.abstained is True
    assert res.card.degraded is True
    assert res.abstained_at == "generator"


def test_guard_failure_abstains_after_one_regeneration():
    """两次校验都不过 -> 拒答 + degraded，原因记 citations_invalid（ADR-4 最后一档）。"""
    bad = DiagnosisCard(
        summary="编了引用",
        causes=[Cause(title="磁盘满", confidence=0.4, evidence=[Evidence(chunk_id="c_fake")])],
    )
    gen = fake_generate_factory([bad, bad])
    d = Diagnoser(FakeRetriever([make_chunk("c_1", rerank=0.9)]), settings(), rerank_fn=fake_rerank(), generate_fn=gen)
    res = d.run("Pod 一直 Pending")
    assert len(gen.calls) == 2
    assert res.regenerated is True
    assert res.card.abstained is True
    assert res.card.degraded is True
    assert res.card.abort_reason == "citations_invalid"
    assert res.card.causes == []

