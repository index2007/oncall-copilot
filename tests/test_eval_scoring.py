"""M10 评测契约层的离线单测：不读语料、不连网、不需要密钥。

钉住的是"打分不能算错"：关键词/根因/引用判定、judge 的调用时机、summarize 的分母规则
（error 不进分母、误拒率、拒答准确率）与 percentile 的边界。

run_eval.py 里要真索引、真模型的部分（Harness / JudgeRunner 的网络调用）不在这里测，
由 eval 冒烟跑覆盖；这里只测它依赖的纯函数契约。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "eval"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
import scoring  # noqa: E402  eval/ 是脚本目录，不在包路径里
from app.obs.stats import percentile  # noqa: E402

GOLD = "https://kubernetes.io/zh-cn/docs/concepts/cluster-administration/logging/"
OTHER = "https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/assign-pod-node/"


def make_row(rid: str = "e001", **overrides) -> scoring.EvalRow:
    """一条正样本：默认关键词 OOMKilled、gold 指向 logging 那篇。"""
    data = {
        "id": rid,
        "symptom": "pod 反复重启，容器退出码 137，节点内存压力大",
        "resource_type": "Pod",
        "gold_root_cause": "容器内存超限，被内核 OOMKilled",
        "gold_doc_urls": (GOLD,),
        "gold_keywords": ("OOMKilled",),
        "negative": False,
    }
    data.update(overrides)
    return scoring.EvalRow(**data)


def make_negative(rid: str = "n001") -> scoring.EvalRow:
    return scoring.EvalRow(
        id=rid,
        symptom="用户说昨天页面有点慢，没有报错，也没有关键词",
        resource_type=None,
        gold_root_cause="语料里没有依据，应拒答",
        negative=True,
    )


# ---------------------------------------------------------------- 关键词判定


def test_keyword_hit_is_case_insensitive_and_normalizes_whitespace() -> None:
    assert scoring.keyword_hit(["OOMKilled"], "Pod 被 oomkilled 杀掉")
    assert scoring.keyword_hit(["oomkilled"], "pod was OOMKilled by kernel")
    # 多个空格/换行折叠成一个空格后再比
    assert scoring.keyword_hit(["exit code 137"], "container  exit\ncode   137")
    assert not scoring.keyword_hit(["OOMKilled"], "CrashLoopBackOff")
    assert not scoring.keyword_hit([], "OOMKilled")
    assert not scoring.keyword_hit(["OOMKilled"], "   ")


# ---------------------------------------------------------------- 根因 Top-1/Top-3


def test_root_cause_hit_top1_and_top3() -> None:
    row = make_row()

    assert scoring.root_cause_hit(["Pod 被 OOMKilled"], row) == (True, True, False)

    # 第二、第三位命中：算 Top-3 不算 Top-1
    assert scoring.root_cause_hit(["无关的镜像拉取失败", "OOMKilled"], row) == (False, True, False)
    assert scoring.root_cause_hit(["a", "b", "被 OOMKilled"], row) == (False, True, False)

    # 只看前三条：第四条命中也不算
    assert scoring.root_cause_hit(["a", "b", "c", "OOMKilled"], row) == (False, False, False)
    assert scoring.root_cause_hit([], row) == (False, False, False)


# ---------------------------------------------------------------- judge 调用时机


def test_judge_called_only_when_keywords_miss() -> None:
    row = make_row()
    seen: list[str] = []

    def judge(candidate: str, _row: scoring.EvalRow) -> bool:
        seen.append(candidate)
        return True

    # 关键词命中 -> 不调 judge
    assert scoring.cause_hit("Pod 被 OOMKilled", row, judge) == (True, False)
    assert seen == []

    # 关键词漏判 -> 调一次，judged=True
    assert scoring.cause_hit("容器内存用量超过 limit 被内核杀掉", row, judge) == (True, True)
    assert len(seen) == 1

    # 没有 judge、或 judge 返回 None（判不了）-> 都算未命中，且不标记 judged
    assert scoring.cause_hit("节点磁盘写满", row) == (False, False)
    assert scoring.cause_hit("节点磁盘写满", row, lambda c, r: None) == (False, False)


def test_root_cause_hit_marks_judge_used_once() -> None:
    row = make_row()
    calls: list[str] = []

    def judge(candidate: str, _row: scoring.EvalRow) -> bool:
        calls.append(candidate)
        return "内存" in candidate

    top1, top3, used = scoring.root_cause_hit(
        ["无关", "容器内存超限被杀", "OOMKilled"], row, judge
    )
    assert top1 is False and top3 is True and used is True
    # 前两条关键词都没命中，各调一次 judge（第一条判否）；第三条关键词直接命中，不再调
    assert len(calls) == 2


# ---------------------------------------------------------------- 引用命中


def test_citation_hit_normalizes_case_and_trailing_slash() -> None:
    row = make_row()  # gold 带尾斜杠

    assert scoring.citation_hit(["https://kubernetes.io/zh-cn/docs/concepts/cluster-administration/logging"], row)
    assert scoring.citation_hit([
        "HTTPS://Kubernetes.IO/zh-cn/docs/concepts/cluster-administration/logging/"
    ], row)
    assert scoring.citation_hit([OTHER, GOLD], row)
    assert not scoring.citation_hit([OTHER], row)
    assert not scoring.citation_hit([], row)
    # 只归一化尾斜杠，query 不同仍是不同页
    assert not scoring.citation_hit([GOLD + "?v=2"], row)


# ---------------------------------------------------------------- 聚合口径


def test_summarize_keeps_error_samples_out_of_denominators() -> None:
    rows = [make_row("e001"), make_row("e002"), make_row("e003")]
    results = [
        scoring.EvalResult(
            row_id="e001",
            variant="full",
            abstained=False,
            causes=("Pod 被 OOMKilled",),
            citations=(GOLD,),
            recall_urls=(GOLD, OTHER),
            ttft_ms=300.0,
            latency_ms=100.0,
            cost_cny=0.01,
        ),
        scoring.EvalResult(
            row_id="e002",
            variant="full",
            abstained=False,
            causes=("镜像拉取失败",),
            citations=(OTHER,),
            recall_urls=(OTHER,),
            ttft_ms=None,
            latency_ms=200.0,
            cost_cny=0.02,
        ),
        scoring.EvalResult(
            row_id="e003",
            variant="full",
            abstained=False,
            latency_ms=999.0,
            cost_cny=9.0,
            error="TimeoutError: 生成超时",
        ),
    ]

    m = scoring.summarize(results, rows, variant="full")

    assert m.positives == 2  # error 那条不算正样本分母
    assert m.errors == 1
    assert m.top1 == 0.5
    assert m.top3 == 0.5
    assert m.recall20 == 0.5
    assert m.citation_hit == 0.5
    assert m.false_abstain == 0.0
    # 延迟/成本也不含 error 那条
    assert m.latency_p50_ms == 100.0
    assert m.latency_p95_ms == 200.0
    assert m.cost_avg_cny == 0.015
    # 只有一条有 TTFT（非流式那条为 None），不应被当成 0 摊进 P50
    assert m.ttft_p50_ms == 300.0

def test_summarize_rejects_unknown_row_id() -> None:
    """数据集里没有的 row_id 必须报错，而不是静默算错（结果与数据集不匹配）。"""
    bad = [scoring.EvalResult(row_id="e999", variant="full", abstained=False)]
    with pytest.raises(ValueError):
        scoring.summarize(bad, [make_row("e001")], variant="full")


def test_summarize_counts_false_abstain_and_negative_accuracy() -> None:
    rows = [make_row("e010"), make_negative("n001"), make_negative("n002")]
    results = [
        # 正样本被拒答：计入误拒率，且不再算 Top-1/recall（causes 为空）
        scoring.EvalResult(
            row_id="e010",
            variant="hybrid_rerank",
            abstained=True,
            recall_urls=(GOLD,),
            latency_ms=50.0,
            cost_cny=0.001,
        ),
        # 负样本正确拒答
        scoring.EvalResult(row_id="n001", variant="hybrid_rerank", abstained=True, latency_ms=60.0),
        # 负样本没有拒答
        scoring.EvalResult(
            row_id="n002",
            variant="hybrid_rerank",
            abstained=False,
            causes=("内存不足",),
            citations=(OTHER,),
            latency_ms=70.0,
        ),
    ]

    m = scoring.summarize(results, rows, variant="hybrid_rerank")

    assert m.positives == 1 and m.negatives == 2
    assert m.false_abstain == 1.0
    assert m.abstain_accuracy == 0.5
    # 拒答的样本不进 Top-1/引用的分子；但召回发生在生成之前，recall@20 必须照算
    assert m.top1 == 0.0
    assert m.recall20_pool == 1.0
    assert m.recall20 == 0.0
    assert m.citation_hit == 0.0
    assert m.citation_hit_answered == 0.0  # 分母 0 条作答样本，不给假分数
    assert m.errors == 0


def test_table_row_has_eleven_columns_and_dash_for_missing_ttft() -> None:
    rows = [make_row("e001")]
    results = [
        scoring.EvalResult(
            row_id="e001",
            variant="full",
            abstained=False,
            causes=("OOMKilled",),
            citations=(GOLD,),
            latency_ms=120.0,
            cost_cny=0.0043,
        )
    ]
    m = scoring.summarize(results, rows, variant="full")
    line = m.table_row()

    assert m.ttft_p50_ms is None
    assert line.startswith("| + Query 理解（full） |")
    assert "—" in line  # 非流式没有 TTFT -> 显示 — 而不是 0
    assert line.count("|") == 12  # 11 列
    assert "¥0.0043" in line
    assert scoring.render_table([m]).count("\n") == 2


# ---------------------------------------------------------------- percentile


def test_percentile_boundaries() -> None:
    assert percentile([], 50) is None
    assert percentile([7.0], 50) == 7.0
    assert percentile([7.0], 95) == 7.0

    assert percentile([10.0, 20.0], 0) == 10.0
    assert percentile([10.0, 20.0], 50) == 10.0  # 最近秩：ceil(0.5*2)-1 = 0
    assert percentile([10.0, 20.0], 95) == 20.0  # ceil(1.9)-1 = 1
    assert percentile([10.0, 20.0], 100) == 20.0

    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0  # ceil(2)-1 = 1
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0
    # 越界百分位夹到两端，不抛 IndexError
    assert percentile([5.0, 9.0], -10) == 5.0
    assert percentile([5.0, 9.0], 250) == 9.0


# ---------------------------------------------------------------- judge 重算指标的接法


class FakeJudge:
    """run_eval.apply_judge 只依赖 .enabled 与 .verdict，不需要真模型与密钥。"""

    enabled = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def verdict(self, candidate: str, _row: scoring.EvalRow) -> bool:
        self.calls.append(candidate)
        return "内存" in candidate


def test_apply_judge_only_pays_for_keyword_misses() -> None:
    import run_eval  # 只测纯逻辑：apply_judge 不碰网络、不建索引

    rows = [make_row("e001"), make_row("e002"), make_row("e003")]
    hit = scoring.EvalResult(
        row_id="e001", variant="full", abstained=False, causes=("Pod 被 OOMKilled",),
        citations=(GOLD,), recall_urls=(GOLD,), latency_ms=100.0, cost_cny=0.004,
    )
    rescued = scoring.EvalResult(
        row_id="e002", variant="full", abstained=False,
        causes=("容器内存超过 limit 被内核杀掉",), citations=(GOLD,),
        recall_urls=(GOLD,), latency_ms=120.0, cost_cny=0.004,
    )
    broken = scoring.EvalResult(
        row_id="e003", variant="full", abstained=False, latency_ms=999.0, error="TimeoutError: 超时",
    )
    judge = FakeJudge()

    m = run_eval.apply_judge(
        scoring.summarize([hit, rescued, broken], rows, variant="full"),
        [hit, rescued, broken],
        rows,
        judge,
    )

    # 分母 2（error 不算）；关键词命中的那条不走 judge，第二条靠 judge 救回
    assert m.top1 == 1.0 and m.top3 == 1.0
    assert m.errors == 1
    assert judge.calls == ["容器内存超过 limit 被内核杀掉"]
    assert m.judge_used == 1


def test_dump_results_writes_rerank_top1_jsonl(tmp_path) -> None:
    """阈值标定吃的是逐条 rerank_top1，不是聚合表；这里钉住落盘字段与行序。"""
    import run_eval  # 只测纯落盘：_dump_results 不碰网络、不建索引

    rows = [make_row("e001"), make_negative("n001")]
    pos = scoring.EvalResult(
        row_id="e001", variant="full", abstained=False, causes=("Pod 被 OOMKilled",),
        citations=(GOLD,), recall_urls=(GOLD,), rerank_top1=0.5587,
        ttft_ms=900.0, latency_ms=2400.0, cost_cny=0.004,
    )
    neg = scoring.EvalResult(
        row_id="n001", variant="full", abstained=True, rerank_top1=0.0002, latency_ms=300.0,
    )
    run = run_eval.VariantRun(
        variant="full",
        results=[pos, neg],
        metrics=scoring.summarize([pos, neg], rows, variant="full"),
    )

    out = tmp_path / "nested" / "results.jsonl"
    run_eval._dump_results(out, [run], rows)

    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [x["row_id"] for x in lines] == ["e001", "n001"]
    assert lines[0]["rerank_top1"] == 0.5587
    assert lines[1]["negative"] is True and lines[1]["abstained"] is True
    assert lines[0]["citations"] == [GOLD]
    assert {"variant", "ttft_ms", "cost_cny", "causes", "recall_urls"} <= set(lines[0])
