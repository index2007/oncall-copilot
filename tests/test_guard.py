"""M8 guard 单测：should_abstain（检索侧拒答）与 verify_citations（引用后置校验）。

三条要守住的纪律，各自有专门用例：
1. should_abstain 只看**最高** rerank 分 —— 不被条数或平均分稀释，等于阈值算通过；
2. verify_citations 只认 allowed 集合 —— 就算某 chunk_id 恰好是本次检索到的块，
   只要没进模型上下文（不在 allowed 里）就算幻觉引用；
3. 两者都是纯函数：不读 settings、不发网络，调用后原卡片不被改动。
"""

from __future__ import annotations

import pytest

from app.core.guard import best_score, should_abstain, verify_citations
from app.core.schemas import Cause, Citation, CitationReport, DiagnosisCard, Evidence, ScoredChunk


def hit(chunk_id="c1", *, score=0.01, rerank=None, source="k8s"):
    return ScoredChunk(
        chunk_id=chunk_id,
        doc_id="d-" + chunk_id,
        text="正文 " + chunk_id,
        score=score,
        rrf_score=score,
        rerank_score=rerank,
        source=source,
    )


def card(*causes, citations=()):
    return DiagnosisCard(causes=list(causes), citations=list(citations))


def cause(title="原因", *chunk_ids):
    return Cause(title=title, confidence=0.6, evidence=[Evidence(chunk_id=cid) for cid in chunk_ids])


# ------------------------------------------------------------------ best_score
def test_best_score_empty_is_zero():
    assert best_score([]) == 0.0


def test_best_score_prefers_rerank_score():
    hits = [hit("c1", score=0.5, rerank=0.2), hit("c2", score=0.01, rerank=0.9)]
    assert best_score(hits) == pytest.approx(0.9)


def test_best_score_falls_back_to_rrf_score():
    # 降级时 rerank_score 为 None，只能退回 RRF 分
    hits = [hit("c1", score=0.0164), hit("c2", score=0.0159)]
    assert best_score(hits) == pytest.approx(0.0164)


def test_best_score_mixes_scored_and_unscored():
    # 同一批里既有打了分的也有没打分的（理论上不该出现），取全局最大而不要求口径一致
    hits = [hit("c1", score=0.0164), hit("c2", score=0.01, rerank=0.4)]
    assert best_score(hits) == pytest.approx(0.4)


# --------------------------------------------------------------- should_abstain
def test_abstain_on_empty_hits():
    assert should_abstain([], 0.35) is True


def test_abstain_when_top1_below_threshold():
    assert should_abstain([hit("c1", rerank=0.10), hit("c2", rerank=0.05)], 0.35) is True


def test_abstain_at_exact_threshold_is_not_abstain():
    # 阈值是及格线：0.35 >= 0.35 就该答，严格小于才拒
    assert should_abstain([hit("c1", rerank=0.35)], 0.35) is False


def test_abstain_above_threshold_is_false():
    assert should_abstain([hit("c1", rerank=0.91)], 0.35) is False


def test_abstain_only_looks_at_max_not_average():
    # 1 条高相关 + 19 条 0 分：平均分 0.0455 < 0.35，但最高分够，不能拒答
    hits = [hit("c0", rerank=0.9)] + [hit("c%d" % i, rerank=0.0) for i in range(1, 20)]
    assert should_abstain(hits, 0.35) is False


def test_abstain_ignores_count():
    # 条数少不代表不能答，条数多也不代表能答：只看最高分
    assert should_abstain([hit("c1", rerank=0.0)] * 50, 0.35) is True


def test_abstain_uses_rerank_score_not_rrf():
    # RRF 分 0.0164 远低于阈值，但重排分 0.8 —— 必须以重排分为准
    assert should_abstain([hit("c1", score=0.0164, rerank=0.8)], 0.35) is False


def test_degraded_hits_abstain_because_rrf_is_tiny():
    # 记录已知后果：重排降级时没有 rerank_score，退回 RRF 分必然低于阈值。
    # 所以 API 层必须先看 degraded，再决定要不要走这一步（否则重排一挂就全量拒答）。
    hits = [hit("c1", score=0.0164), hit("c2", score=0.0159)]
    assert should_abstain(hits, 0.35) is True


def test_threshold_zero_never_abstains_on_non_empty():
    assert should_abstain([hit("c1", rerank=0.0)], 0.0) is False


# ------------------------------------------------------------ verify_citations
def test_all_valid_is_ok():
    c = card(cause("OOM", "c1"), cause("限额", "c2"), citations=[Citation(chunk_id="c3")])
    report = verify_citations(c, {"c1", "c2", "c3", "c4"})
    assert isinstance(report, CitationReport)
    assert (report.total, report.valid) == (3, 3)
    assert report.invalid_ids == []
    assert report.orphan_causes == []
    assert report.coverage == pytest.approx(1.0)
    assert report.ok is True


def test_hallucinated_evidence_is_invalid_and_orphans_cause():
    c = card(cause("编的", "c_fake"))
    report = verify_citations(c, {"c1"})
    assert report.total == 1
    assert report.valid == 0
    assert report.invalid_ids == ["c_fake"]
    assert report.orphan_causes == [0]
    assert report.ok is False


def test_only_allowed_set_decides_not_retrieved_chunks():
    # c99 是上一轮会话里真实存在的块，但没进这次上下文 -> 仍是幻觉引用
    c = card(cause("跨轮张冠李戴", "c99"))
    assert verify_citations(c, {"c1", "c2"}).invalid_ids == ["c99"]


def test_partial_invalid_ids_keep_first_seen_order_and_dedupe():
    c = card(cause("甲", "c_fake_b", "c_fake_a"), cause("乙", "c_fake_b"), citations=[Citation(chunk_id="c_fake_c")])
    report = verify_citations(c, {"c1"})
    assert report.invalid_ids == ["c_fake_b", "c_fake_a", "c_fake_c"]
    assert report.total == 4
    assert report.valid == 0


def test_duplicate_valid_citations_count_each_occurrence():
    c = card(cause("重复引用", "c1", "c1"), citations=[Citation(chunk_id="c1")])
    report = verify_citations(c, {"c1"})
    assert (report.total, report.valid) == (3, 3)


def test_partially_supported_cause_is_not_orphan():
    # 一条 evidence 真、一条假：cause 有据可依，但 ok=False 因为存在幻觉引用
    c = card(cause("半真", "c1", "c_fake"))
    report = verify_citations(c, {"c1"})
    assert report.total == 2
    assert report.valid == 1
    assert report.invalid_ids == ["c_fake"]
    assert report.orphan_causes == []
    assert report.ok is False


def test_cause_without_evidence_is_orphan():
    c = card(cause("没证据"), cause("有证据", "c1"))
    report = verify_citations(c, {"c1"})
    assert report.orphan_causes == [0]
    assert report.coverage == pytest.approx(0.5)
    assert report.ok is False


def test_empty_chunk_id_counts_as_invalid():
    c = card(cause("没写 id", ""), citations=[Citation(chunk_id="")])
    report = verify_citations(c, {"c1"})
    assert report.invalid_ids == [""]  # 去重后只记一次
    assert report.total == 2
    assert report.valid == 0


def test_citations_valid_but_no_causes_is_ok():
    report = verify_citations(card(citations=[Citation(chunk_id="c1")]), {"c1"})
    assert report.ok is True
    assert report.coverage == 0.0  # 没有 cause，分母为 0


def test_empty_card_is_ok_with_zero_coverage():
    report = verify_citations(DiagnosisCard(), {"c1"})
    assert (report.total, report.valid) == (0, 0)
    assert report.invalid_ids == []
    assert report.orphan_causes == []
    assert report.coverage == 0.0
    assert report.ok is True


def test_abstained_empty_card_is_ok():
    # 拒答卡片没有任何引用，不该被判成幻觉
    report = verify_citations(DiagnosisCard(abstained=True, summary="检索结果与问题无关"), set())
    assert report.ok is True


def test_empty_allowed_rejects_everything():
    c = card(cause("甲", "c1"), citations=[Citation(chunk_id="c2")])
    report = verify_citations(c, set())
    assert report.valid == 0
    assert report.invalid_ids == ["c1", "c2"]
    assert report.orphan_causes == [0]
    assert report.ok is False


def test_extra_allowed_ids_do_not_matter():
    # allowed 是上下文白名单，允许有多余条目（没被引用的块不影响结论）
    report = verify_citations(card(cause("甲", "c1")), {"c1", "c2", "c3", "c4"})
    assert report.ok is True


def test_verify_does_not_mutate_card():
    c = card(cause("甲", "c1", "c_fake"), citations=[Citation(chunk_id="c2")])
    before = c.model_dump()
    verify_citations(c, {"c1"})
    assert c.model_dump() == before


def test_none_card_returns_default_report():
    assert verify_citations(None, {"c1"}) == CitationReport()


def test_coverage_rounds_to_four_places():
    # 1/3 的 cause 有据可依
    c = card(cause("甲", "c1"), cause("乙"), cause("丙"))
    assert verify_citations(c, {"c1"}).coverage == pytest.approx(0.3333)
