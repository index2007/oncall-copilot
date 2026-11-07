#!/usr/bin/env python
"""M10 评测 harness：五档检索变体 -> 设计清单 §6.3 对比表 + 失败明细 + 成本。

用法（Windows 控制台先设 PYTHONIOENCODING=utf-8，否则中文表头会乱码）：

    .venv/Scripts/python.exe eval/run_eval.py --variant hybrid_rerank
    .venv/Scripts/python.exe eval/run_eval.py --variant all --stream --out eval/report.md
    .venv/Scripts/python.exe eval/run_eval.py --variant bm25 --limit 5 --judge keyword
    .venv/Scripts/python.exe eval/run_eval.py --variant full --json > eval/report.json
    .venv/Scripts/python.exe eval/run_eval.py --variant all --limit 1 --dry-run

逐条链路：plan -> 召回 ->（重排）->（闸门）-> 生成 -> 记 EvalResult。

== 五档怎么接线（改之前先读，否则表不可比）==
- bm25：只调 retriever.bm25_search，召回池 20 条，取前 top_k_final(默认 8) 条喂生成；不做向量、不做 RRF、
  不做重排、不做 Query 理解。
- vector：只调 retriever.vector_search，同上。
- hybrid：retriever.retrieve(use_sub_index=False)：两路各 20 -> RRF -> 20 条候选，
  取前 top_k_final 条喂生成；不重排、不过滤子库。
- hybrid_rerank：hybrid 的 20 条候选 -> reranker.rerank -> top_k_final 条喂生成。
- full：query_understanding.analyze 得到 plan（子库过滤 + 查询扩展）->
  retrieve(use_sub_index=True) -> rerank -> top_k_final 条（选块再做每文档≤2 配额）。
生成侧五档共用 app.core.generator（同一套 prompt、同一个模型），否则 Top-1 无从对比。

== 口径与取舍（都是刻意的，不是没接完）==
1. Retriever.retrieve() 内含子库过滤与"不足就补跑"的 fallback，表达不了"纯 BM25 / 纯向量"，
   所以这两档直接调底层 bm25_search / vector_search；hybrid 用 retrieve(use_sub_index=False)
   才是真正的"两路各 20 + RRF"，而不是拿 full 的结果再截前 top_k_final 条。
2. 非 full 变体不调 analyze()：用 QueryPlan(raw=symptom, clean=symptom)，search_text 就是
   原始报错文本。否则规则抽出的错误码/事件词会混进基线，"Query 理解"的增量被摊平。
3. bm25/vector 的召回池同样取 20 条再截前 top_k_final 条：这样 recall@20 这一列五档同口径；只取 5 条的话
   这一列对这档退化成 recall@5，和其他档没法比。
4. 闸门 should_abstain 只在重排后套用（与 pipeline 一致，重排降级时跳过）。无重排的三档
   没有与 abstain_threshold(0.35) 同量纲的分数——BM25 无上界、余弦 0~1、RRF 约 1/60——
   套上去是拿苹果比橘子，所以不套，只靠生成侧兜底。代价：这三档误拒率天然偏低、
   负样本拒答准确率也偏低，读表时以 full 的行为为准。
5. --stream 走 generator.open_stream + finalize_stream（与 /v1/ask?stream=true 同一条链路），
   TTFT 从"请求开始"（含检索与重排）量到第一个 delta；非 --stream 走 generate()。
   非流式没有 TTFT，EvalResult.ttft_ms=None，表里显示 "—"。
6. scoring.summarize 的签名没有 judge 参数（该文件已冻结），所以 judge 在 summarize 之后用
   scoring.root_cause_hit(causes, row, judge) 重算 Top-1/Top-3 覆盖指标；failure_details
   不带 judge，失败明细仍按关键词判定，报告里已注明这个差异。
7. full 的 Query 理解若走 LLM，其 token 不计入 card.cost_cny（analyze 不返回 usage），
   所以 full 的成本略低估；要严格对齐就设 QUERY_LLM_ENABLED=0 再跑。
8. 异常/超时只写进该条的 EvalResult.error，整轮继续；error 样本不进任何分母（scoring 口径）。

judge 缓存落 eval/.judge_cache.json（key = sha256(gold + 候选根因)），可复用，避免重复付费；
该文件已在 .gitignore 里。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

# 直接 python eval/run_eval.py 时 sys.path[0] 是 eval/，仓库根不在里面；
# 两个都插上，脚本与 python -m eval.run_eval 都能跑。
_EVAL_DIR = Path(__file__).resolve().parent
_ROOT = _EVAL_DIR.parent
for _p in (str(_ROOT), str(_EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scoring  # noqa: E402  eval/ 已在 sys.path 上，scoring.py 是冻结的契约层

from loguru import logger  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.core import generator as generator_mod  # noqa: E402
from app.core import guard, prompts, query_understanding  # noqa: E402
from app.core.reranker import get_reranker  # noqa: E402
from app.core.retriever import Retriever  # noqa: E402
from app.core.schemas import DiagnosisCard, QueryPlan, ScoredChunk  # noqa: E402

JUDGE_CACHE_NAME = ".judge_cache.json"
DEFAULT_DATASET = "dataset.jsonl"
DEFAULT_OUT = "report.md"
FAILURE_LIMIT = 10
_TICK = chr(96)  # 反引号：源码里不埋转义，也方便在 Windows 上读

# 每档"差在哪"的唯一说明来源：--dry-run 打印它，报告的口径段也用它。
VARIANT_CHAINS: dict[str, str] = {
    "bm25": "bm25_search(k=20) -> 前 top_k_final 条 -> generate（无向量、无 RRF、无重排、无 Query 理解）",
    "vector": "vector_search(k=20) -> 前 top_k_final 条 -> generate（无 BM25、无 RRF、无重排、无 Query 理解）",
    "hybrid": "retrieve(use_sub_index=False)：向量 20 + BM25 20 -> RRF -> 20 -> 前 top_k_final 条 -> generate",
    "hybrid_rerank": "hybrid 的 20 条 -> rerank -> top_k_final -> generate（不做子库过滤）",
    "full": "analyze（子库过滤 + 查询扩展）-> retrieve(use_sub_index=True) -> rerank -> top_k_final -> generate",
}

JUDGE_SYSTEM_PROMPT = (
    "你是 RAG 评测的判分员。判断「候选根因」与「标准根因」是否在语义上一致："
    "只要指向同一个故障机制就算一致，允许措辞、粒度、语言不同；"
    "方向相反、主体不同、或候选只是复述现象而没给出机制，都算不一致。"
    "只输出 JSON：{\"agree\": true} 或 {\"agree\": false}。"
)
JUDGE_USER_PROMPT = "标准根因：{gold}\n\n候选根因：{candidate}\n\n两者是否语义一致？"

USAGE_EXAMPLES = """示例：
  # 一档一跑（最常用）
  .venv/Scripts/python.exe eval/run_eval.py --variant hybrid_rerank

  # 五档全跑 + 流式量 TTFT + 写报告
  .venv/Scripts/python.exe eval/run_eval.py --variant all --stream --out eval/report.md

  # 省钱调试：前 5 条样本 + 关键词判定，不调 judge
  .venv/Scripts/python.exe eval/run_eval.py --variant all --limit 5 --judge keyword

  # 只看计划不花钱（不建索引、不调模型）
  .venv/Scripts/python.exe eval/run_eval.py --variant all --limit 1 --dry-run

注意：--judge llm（默认）会真调 DeepSeek 判定关键词漏判的样本，结果缓存在
eval/.judge_cache.json；跑一轮 40 条 x 5 档前先用 --limit 5 估一下花费。
"""


# --------------------------------------------------------------------- 小工具


def force_utf8() -> None:
    """Windows 控制台是 GBK：中文表与 ¥ 会直接 UnicodeEncodeError，这里兜底。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 被重定向成非文本流时忽略
            pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cause_text(cause: Any) -> str:
    """只取 title：设计清单 §6.2 把"根因命中"定义为结论标题命中。

    早先这里是 title + explanation 拼接，explanation 里提到 gold 关键词就算命中，
    等于用模型的长解释给自己加分；改成只喂 title 后这个数字是可信的。
    """
    return (getattr(cause, "title", "") or "").strip()


def _loads_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠一个 JSON 对象（容忍围栏与前后废话）。"""
    raw = (text or "").strip()
    fence = _TICK * 3
    if raw.startswith(fence):
        raw = raw[len(fence):].strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------------ judge


class JudgeRunner:
    """LLM-as-judge：候选根因与 gold_root_cause 是否语义一致，结果落盘复用。

    只判"关键词没命中"的样本（scoring.cause_hit 先走关键词），所以正常一轮下来调用量
    远小于样本数 x 3。缓存 key = sha256(判分环境指纹 + gold + candidate)：指纹含模型名与
    两段 prompt 原文，换模型或改 prompt 后旧缓存自动失效，不会拿上一版口径的结果给这一版打分。
    """

    def __init__(self, settings: Settings, cache_path: Path, *, enabled: bool = True):
        self.settings = settings
        self.path = Path(cache_path)
        self.enabled = bool(enabled)
        self.calls = 0
        self.cache_hits = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._call = generator_mod.default_llm_callable(settings) if self.enabled else None
        self._entries = self._load()
        self._fingerprint = self._judge_fingerprint()

    # -- 缓存 --------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
            return dict(raw["entries"])
        return {}

    def save(self) -> None:
        if not self.enabled:
            return
        payload = {"version": 1, "updated_at": _now(), "entries": self._entries}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:  # 缓存写不了不该毁掉整轮结果
            logger.warning("judge 缓存写入失败：{}", exc)

    def _judge_fingerprint(self) -> str:
        """判分口径指纹：模型 + 两段 prompt 原文。

        以前 key 只有 (gold, candidate)，换了 judge 模型或改了 prompt 会静默复用旧判定，
        指标就不可复现了——评测里的"缓存命中"必须建立在同一口径上。
        """
        payload = "\x00".join(
            [self.settings.llm_model, JUDGE_SYSTEM_PROMPT, JUDGE_USER_PROMPT]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _key(self, gold: str, candidate: str) -> str:
        raw = self._fingerprint + "\n" + gold + "\n" + candidate
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # -- 判定（给 scoring.Judge 用）----------------------------
    def verdict(self, candidate: str, row: scoring.EvalRow) -> bool | None:
        if not self.enabled or self._call is None:
            return None
        key = self._key(row.gold_root_cause, candidate)
        item = self._entries.get(key)
        if isinstance(item, dict) and isinstance(item.get("agree"), bool):
            self.cache_hits += 1
            return bool(item["agree"])

        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": JUDGE_USER_PROMPT.format(
                gold=row.gold_root_cause, candidate=candidate)},
        ]
        try:
            resp = self._call(messages, timeout=self.settings.llm_timeout_s)
        except Exception as exc:  # noqa: BLE001 —— 判分失败等价于"判不了"，不能中断整轮
            logger.warning("judge 调用失败（{}）：{}", row.id, exc)
            return None
        data = _loads_json_object(getattr(resp, "content", "") or "")
        if not data or not isinstance(data.get("agree"), bool):
            logger.warning("judge 输出不是合法 JSON（{}）：{}", row.id,
                           (getattr(resp, "content", "") or "")[:120])
            return None

        self.calls += 1
        self.prompt_tokens += int(getattr(resp, "prompt_tokens", 0) or 0)
        self.completion_tokens += int(getattr(resp, "completion_tokens", 0) or 0)
        agree = bool(data["agree"])
        self._entries[key] = {
            "agree": agree,
            "prompt_tokens": int(getattr(resp, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(resp, "completion_tokens", 0) or 0),
            "at": _now(),
        }
        return agree

    @property
    def cost_cny(self) -> float:
        s = self.settings
        cost = (
            self.prompt_tokens * float(s.llm_price_in_cny_per_mtok)
            + self.completion_tokens * float(s.llm_price_out_cny_per_mtok)
        ) / 1_000_000
        return round(cost, 6)


# ------------------------------------------------------------------ Harness


@dataclass
class VariantRun:
    """一档跑完的现场：逐条结果 + 聚合指标 + 引用校验计数。"""

    variant: str
    results: list[scoring.EvalResult]
    metrics: scoring.VariantMetrics
    invalid_citations: int = 0
    orphan_causes: int = 0
    elapsed_s: float = 0.0


class Harness:
    """把五档变体接到真实组件上。构造一次，五档复用同一个 retriever / reranker。"""

    def __init__(self, settings: Settings, *, stream: bool = False):
        self.settings = settings
        self.stream = bool(stream)
        self.retriever = Retriever.from_settings(settings)
        self.reranker = get_reranker(settings)

    # -- 计划 --------------------------------------------------
    def plan_for(self, row: scoring.EvalRow, variant: str) -> QueryPlan:
        if variant == "full":
            # 唯一一档开 Query 理解：子库过滤 + 查询扩展都在 analyze 里
            return query_understanding.analyze(row.symptom, settings=self.settings)
        # 其余四档的基线：不抽错误码、不扩查询、不带子库，search_text 就是原始报错
        return QueryPlan(raw=row.symptom, clean=row.symptom)

    # -- 召回 --------------------------------------------------
    def candidates_for(
        self, plan: QueryPlan, variant: str
    ) -> tuple[list[ScoredChunk], bool]:
        """返回 (重排前候选, 是否重排)。召回池统一 20 条，recall@20 才同口径。"""
        s = self.settings
        pool = max(int(s.top_k_fuse or 20), int(s.top_k_final or 5), 20)
        if variant == "bm25":
            return self.retriever.bm25_search(plan, k=pool), False
        if variant == "vector":
            return self.retriever.vector_search(plan, k=pool), False
        use_sub_index = variant == "full"
        fused = self.retriever.retrieve(plan, limit=pool, use_sub_index=use_sub_index)
        return fused, variant in ("hybrid_rerank", "full")

    def rerank_top(self, plan: QueryPlan, candidates: list[ScoredChunk]) -> tuple[list[ScoredChunk], float]:
        """重排取 Top5；返回 (结果, 这一条的重排成本)。失败时组件自己降级，不抛异常。"""
        top = self.reranker.rerank(
            plan.search_text, candidates, top_n=int(self.settings.top_k_final or 5))
        stats = getattr(self.reranker, "last_stats", {}) or {}
        return top, float(stats.get("cost_cny") or 0.0)

    def fed_chunks(self, plan: QueryPlan, chunks: Sequence[ScoredChunk]) -> list[ScoredChunk]:
        """真正喂给 LLM 的块：与 generate() 内部同一条预算公式，口径不能分叉。"""
        if not chunks:
            return []
        overhead = len(prompts.render_user_message(plan.search_text, []))
        budget = max(
            int(generator_mod.MIN_CONTEXT_BUDGET),
            int(generator_mod.PROMPT_CHAR_BUDGET) - overhead,
        )
        _text, used = generator_mod.build_context(list(chunks), budget=budget)
        return list(used)

    # -- 生成 --------------------------------------------------
    def generate_card(
        self, plan: QueryPlan, fed: Sequence[ScoredChunk], *, started: float
    ) -> tuple[DiagnosisCard, float | None]:
        """返回 (卡, TTFT)。TTFT 只在流式时量，起点是整条请求的 started。"""
        if not fed:
            # 与 open_stream 的失败分支同口径：没有可喂的块 -> 生成侧拒答卡，不调 LLM
            return generator_mod.generate(plan.search_text, [], settings=self.settings), None

        if self.stream:
            t_gen = time.perf_counter()
            session = generator_mod.open_stream(plan.search_text, list(fed), settings=self.settings)
            ttft: float | None = None
            for _piece in session:
                if ttft is None:
                    ttft = round((time.perf_counter() - started) * 1000, 1)
            card = generator_mod.finalize_stream(
                session.text,
                list(fed),
                settings=self.settings,
                prompt_tokens=session.prompt_tokens,
                completion_tokens=session.completion_tokens,
                started=t_gen,
                model=session.model,
            )
            return card, ttft

        return generator_mod.generate(plan.search_text, list(fed), settings=self.settings), None

    # -- 单条 --------------------------------------------------
    def run_sample(
        self, row: scoring.EvalRow, variant: str
    ) -> tuple[scoring.EvalResult, int, int]:
        """跑一条样本，返回 (EvalResult, 非法引用数, 无有效证据的 cause 数)。

        任何异常都收敛到 error 字段：一条样本挂掉不能毁掉整轮 40 条。
        """
        t0 = time.perf_counter()
        recall_urls: tuple[str, ...] = ()
        rerank_top1: float | None = None
        cost = 0.0
        try:
            plan = self.plan_for(row, variant)
            candidates, need_rerank = self.candidates_for(plan, variant)
            recall_urls = tuple(c.url for c in candidates if c.url)

            if need_rerank:
                context, rerank_cost = self.rerank_top(plan, candidates)
                cost += rerank_cost
                degraded = bool(context) and bool(context[0].degraded)
            else:
                context = list(candidates[: int(self.settings.top_k_final or 5)])
                degraded = False
            # 阈值标定要的是重排原始分（不是 RRF 量级），降级时为 None
            rerank_top1 = (
                context[0].rerank_score if (need_rerank and context) else None
            )

            # 闸门一：与 pipeline 一致，只在有重排分、且重排没降级时套阈值
            if need_rerank and not degraded and guard.should_abstain(
                context, float(self.settings.abstain_threshold)
            ):
                return (
                    scoring.EvalResult(
                        row_id=row.id,
                        variant=variant,
                        abstained=True,
                        recall_urls=recall_urls, rerank_top1=rerank_top1,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                        cost_cny=round(cost, 6),
                    ),
                    0,
                    0,
                )

            fed = self.fed_chunks(plan, context)
            card, ttft = self.generate_card(plan, fed, started=t0)
            report = guard.verify_citations(card, {c.chunk_id for c in fed if c.chunk_id})

            result = scoring.EvalResult(
                row_id=row.id,
                variant=variant,
                abstained=bool(card.abstained),
                causes=tuple(_cause_text(c) for c in card.causes),
                citations=tuple(c.url for c in card.citations if c.url),
                recall_urls=recall_urls, rerank_top1=rerank_top1,
                context_urls=tuple(c.url for c in fed if c.url),
                ttft_ms=ttft,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                cost_cny=round(float(card.cost_cny or 0.0) + cost, 6),
            )
            return result, len(report.invalid_ids), len(report.orphan_causes)
        except Exception as exc:  # noqa: BLE001 —— 单条失败只记 error，不中断整轮
            logger.warning("{} / {} 失败：{}: {}", variant, row.id, type(exc).__name__, exc)
            return (
                scoring.EvalResult(
                    row_id=row.id,
                    variant=variant,
                    abstained=False,
                    recall_urls=recall_urls, rerank_top1=rerank_top1,
                    latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                    cost_cny=round(cost, 6),
                    error="{}: {}".format(type(exc).__name__, exc)[:500],
                ),
                0,
                0,
            )


# --------------------------------------------------------------- 聚合与 judge


def apply_judge(
    metrics: scoring.VariantMetrics,
    results: Sequence[scoring.EvalResult],
    rows: Sequence[scoring.EvalRow],
    judge: JudgeRunner,
) -> scoring.VariantMetrics:
    """用 judge 重算 Top-1/Top-3，覆盖 summarize 的关键词结果（冻结契约的缺口）。

    分母与 summarize 完全一致：正样本里 error 的排除、拒答的仍留在分母里；只对非拒答
    样本的 causes[:3] 调 judge（关键词命中就不会触发）。
    """
    if not judge.enabled:
        return metrics
    by_id = {r.id: r for r in rows}
    pos_ok = [
        (by_id[res.row_id], res)
        for res in results
        if res.ok and not by_id[res.row_id].negative
    ]
    denom = len(pos_ok)
    top1 = top3 = judged = 0
    for row, res in pos_ok:
        if res.abstained:
            continue
        # 省钱快路径：Top-1 关键词就命中时，Top-3 必然命中，不必再把后两条候选送去 judge
        if res.causes and scoring.keyword_hit(row.gold_keywords, res.causes[0]):
            top1 += 1
            top3 += 1
            continue
        hit1, hit3, used = scoring.root_cause_hit(res.causes, row, judge.verdict)
        top1 += int(hit1)
        top3 += int(hit3)
        judged += int(used)
    if denom:
        metrics.top1 = top1 / denom
        metrics.top3 = top3 / denom
    metrics.judge_used = judged
    return metrics


def run_variant(
    variant: str,
    rows: Sequence[scoring.EvalRow],
    harness: Harness,
    judge: JudgeRunner,
) -> VariantRun:
    started = time.perf_counter()
    results: list[scoring.EvalResult] = []
    invalid = orphan = 0
    total = len(rows)
    for idx, row in enumerate(rows, start=1):
        res, bad, orph = harness.run_sample(row, variant)
        results.append(res)
        invalid += bad
        orphan += orph
        flag = "ERR " + (res.error or "")[:80] if not res.ok else ("abstain" if res.abstained else "ok")
        print(
            "  [{} {}] {}/{} {} {:.0f}ms ¥{:.4f}".format(
                variant.ljust(13), flag.ljust(9), idx, total, row.id,
                res.latency_ms, res.cost_cny,
            ),
            file=sys.stderr,
        )

    metrics = scoring.summarize(results, rows, variant=variant)
    metrics = apply_judge(metrics, results, rows, judge)
    elapsed = time.perf_counter() - started
    print(
        "  == {} 完成：{} 条 / {:.1f}s / Top-1 {:.1%}".format(
            variant, total, elapsed, metrics.top1,
        ),
        file=sys.stderr,
    )
    return VariantRun(
        variant=variant,
        results=results,
        metrics=metrics,
        invalid_citations=invalid,
        orphan_causes=orphan,
        elapsed_s=elapsed,
    )


# ------------------------------------------------------------------ 报告


def _money_percentile(values: Sequence[float], q: float) -> float | None:
    """金额分位：与 app.obs.stats.percentile 同为最近秩，但不保留 1 位——按毫秒口径取整
    会把 ¥0.006 压成 ¥0.0，成本表的 P50 就失去意义。"""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    idx = max(0, min(n - 1, math.ceil(q / 100 * n) - 1))
    return round(ordered[idx], 6)


def _cost_lines(runs: Sequence[VariantRun], judge: JudgeRunner) -> str:
    gen_total = sum(res.cost_cny for run in runs for res in run.results)
    judge_cost = judge.cost_cny
    lines = [
        "| 项 | 值 |",
        "|---|---|",
        "| 生成成本合计 | ¥{:.6f} |".format(gen_total),
        "| judge 成本合计 | ¥{:.6f}（调用 {} 次，缓存命中 {} 次） |".format(
            judge_cost, judge.calls, judge.cache_hits),
        "| **总花费** | **¥{:.6f}** |".format(gen_total + judge_cost),
        "",
        "| 变体 | 条数 | 单条均值 | 单条 P50 | 单条 P95 | 总耗时 |",
        "|---|---|---|---|---|---|",
    ]
    for run in runs:
        costs = [res.cost_cny for res in run.results if res.ok]
        p50 = _money_percentile(costs, 50)
        p95 = _money_percentile(costs, 95)
        avg = sum(costs) / len(costs) if costs else 0.0
        lines.append(
            "| {} | {} | ¥{:.6f} | {} | {} | {:.1f}s |".format(
                run.metrics.label,
                len(costs),
                avg,
                "—" if p50 is None else "¥{:.6f}".format(p50),
                "—" if p95 is None else "¥{:.6f}".format(p95),
                run.elapsed_s,
            )
        )
    return "\n".join(lines)


def render_report(
    runs: Sequence[VariantRun],
    rows: Sequence[scoring.EvalRow],
    judge: JudgeRunner,
    *,
    args: argparse.Namespace,
    settings: Settings,
) -> str:
    positives, negatives = scoring.split_rows(rows)
    limit_note = "（--limit {}）".format(args.limit) if args.limit else ""
    head = [
        "# On-call Copilot 评测报告（M10）",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 运行时间 | {} |".format(_now()),
        "| 数据集 | {}（{} 条：正 {} / 负 {}）{} |".format(
            args.dataset, len(rows), len(positives), len(negatives), limit_note),
        "| 变体 | {} |".format("、".join(run.metrics.label for run in runs)),
        "| 生成模型 | {}（thinking={}，temperature={}） |".format(
            settings.llm_model, settings.llm_thinking, settings.llm_temperature),
        "| judge | {}{} |".format(
            args.judge,
            "（缓存 eval/.judge_cache.json）" if judge.enabled else "（不调 LLM，仅关键词）"),
        "| 流式 | {} |".format(
            "是：open_stream + finalize_stream，TTFT 从请求开始量" if args.stream
            else "否：generate 整卡，TTFT 记 None（表中显示 —）"),
        "",
        "## §6.3 对比表",
        "",
        scoring.render_table([run.metrics for run in runs]),
        "",
        "> Top-1/Top-3 由 {} 判定；失败明细仍按关键词判定，两者可能有个别样本不一致。".format(
            "LLM-as-judge（关键词先判，漏判才调模型）" if judge.enabled else "关键词"),
        ">",
        "> 列口径：**recall@20 只看召回池、分母是全部正样本**（生成之前的事，拒答不该拖累它）；"
        "**引用命中只看答出来的卡、分母是作答样本数**（拒答的卡没有 citations 可核验）。"
        "两者都把拒答样本的处置交给误拒率一列。",
        "",
        "## 失败明细（Top-1 未命中或引用未命中）",
        "",
    ]
    body: list[str] = []
    for run in runs:
        body.append("### {}".format(run.metrics.label))
        body.append("")
        detail = scoring.failure_details(run.results, rows, limit=FAILURE_LIMIT)
        body.append(detail if detail else "（无失败样本）")
        body.append("")
        body.append(
            "引用校验：非法 chunk_id {} 条 / 无有效证据的 cause {} 条"
            "（生成侧已过滤，非 0 说明有降级或过滤缺口）".format(
                run.invalid_citations, run.orphan_causes)
        )
        if run.metrics.judge_used:
            body.append(
                "本档有 {} 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词"
                "判定，这部分样本可能同时出现在上面，属已知差异。".format(run.metrics.judge_used)
            )
        body.append("")

    tail = [
        "## 成本与耗时",
        "",
        _cost_lines(runs, judge),
        "",
        "## 口径与已知差异（读表前必看）",
        "",
        "- 五档只在检索链上不同，生成侧是同一套 prompt、同一个模型：",
    ]
    tail += ["  - {}：{}".format(scoring.VARIANT_LABELS[run.variant], VARIANT_CHAINS[run.variant])
             for run in runs]
    tail += [
        "- 非 full 变体不调 analyze()（QueryPlan 只填 raw/clean），所以基线里没有错误码抽取与"
        "查询扩展；这是为了把「Query 理解」的增量隔离出来。",
        "- 无重排的三档（bm25/vector/hybrid）没有与 abstain_threshold 同量纲的分数，"
        "不套检索侧闸门，只靠生成侧兜底：它们的误拒率天然偏低，负样本拒答准确率也偏低，"
        "对比 full 时要带上这条。",
        "- bm25/vector 的召回池也是 20 条（只把前 top_k_final 条喂生成），否则 recall@20 对这两档退化成 recall@top_k_final。",
        "- error 样本只计入 errors，不进 Top-1/recall/成本的分母（scoring.summarize 口径）。",
    ]
    if judge.enabled:
        tail += [
            "- full 的 Query 理解若走 LLM，其 token 不计入 card.cost_cny（analyze 不返回 usage），"
            "所以 full 成本略低估；严格对齐请设 QUERY_LLM_ENABLED=0 再跑。",
        ]
    return "\n".join(head + body + tail) + "\n"


def _dump_results(
    path: Path, runs: Sequence[VariantRun], rows: Sequence[scoring.EvalRow]
) -> None:
    """逐条落盘：阈值标定与失败归因都吃这份明细，报告只放聚合。"""
    by_id = {r.id: r for r in rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for run in runs:
            for res in run.results:
                row = by_id[res.row_id]
                fh.write(json.dumps({
                    "variant": res.variant,
                    "row_id": res.row_id,
                    "negative": row.negative,
                    "abstained": res.abstained,
                    "error": res.error,
                    "rerank_top1": res.rerank_top1,
                    "ttft_ms": res.ttft_ms,
                    "latency_ms": res.latency_ms,
                    "cost_cny": res.cost_cny,
                    "causes": list(res.causes),
                    "citations": list(res.citations),
                    "recall_urls": list(res.recall_urls),
                    # 真正喂进 prompt 的那几块（≤ top_k_final）：失败归因要区分
                    # "召回池里没有"与"召回有但没进上下文"，只有 recall_urls 看不出来。
                    "context_urls": list(res.context_urls),
                }, ensure_ascii=False) + "\n")


def _json_summary(
    runs: Sequence[VariantRun], rows: Sequence[scoring.EvalRow], judge: JudgeRunner,
    args: argparse.Namespace, settings: Settings,
) -> dict[str, Any]:
    positives, negatives = scoring.split_rows(rows)
    gen_total = round(sum(r.cost_cny for run in runs for r in run.results), 6)
    return {
        "generated_at": _now(),
        "dataset": str(args.dataset),
        "rows": len(rows),
        "positives": len(positives),
        "negatives": len(negatives),
        "limit": args.limit,
        "stream": bool(args.stream),
        "judge": args.judge,
        "model": settings.llm_model,
        "thinking": settings.llm_thinking,
        "temperature": settings.llm_temperature,
        "variants": [run.metrics.as_dict() for run in runs],
        "cost_cny": {
            "generation": gen_total,
            "judge": judge.cost_cny,
            "total": round(gen_total + judge.cost_cny, 6),
        },
        "judge_calls": judge.calls,
        "judge_cache_hits": judge.cache_hits,
        "errors": {run.variant: sum(1 for r in run.results if not r.ok) for run in runs},
        "elapsed_s": {run.variant: round(run.elapsed_s, 1) for run in runs},
    }


# ------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description="M10 评测 harness：五档检索变体逐条真调 LLM，产出 §6.3 对比表与失败明细",
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--variant",
        action="append",
        choices=[*scoring.VARIANTS, "all"],
        default=None,
        help="bm25 / vector / hybrid / hybrid_rerank / full，可重复；all = 五档全跑（默认 full）",
    )
    parser.add_argument(
        "--dataset", default=str(_EVAL_DIR / DEFAULT_DATASET),
        help="评测集 jsonl（默认 eval/dataset.jsonl）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试省钱用）")
    parser.add_argument(
        "--out", default=str(_EVAL_DIR / DEFAULT_OUT),
        help="Markdown 报告路径（默认 eval/report.md）",
    )
    parser.add_argument(
        "--dump-results", default=None, metavar="PATH",
        help="逐条结果写成 JSONL（含 rerank_top1 原始分），用于阈值标定，不影响报告",
    )
    parser.add_argument(
        "--stream", action="store_true",
        help="走 SSE 链路（open_stream/finalize_stream）并量 TTFT；不加则 TTFT 为 None",
    )
    parser.add_argument(
        "--judge", choices=("llm", "keyword"), default="llm",
        help="根因判定方式（默认 llm：关键词漏判才调模型，结果落 eval/.judge_cache.json）",
    )
    parser.add_argument(
        "--temperature", type=float, default=None, metavar="T",
        help="覆盖 llm_temperature（只影响本次评测；默认沿用配置，即 0.2。取 0 用于可复现对比）",
    )
    parser.add_argument("--json", action="store_true", help="stdout 输出 JSON 摘要（表打到 stderr）")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印运行计划（变体、条数、每档链路、模型），不建索引、不调模型、不花钱",
    )
    return parser


def resolve_variants(values: Sequence[str] | None) -> list[str]:
    picked: list[str] = []
    for value in values or ["full"]:
        candidates = list(scoring.VARIANTS) if value == "all" else [value]
        for item in candidates:
            if item not in picked:
                picked.append(item)
    return picked


def _print_plan(args: argparse.Namespace, variants: Sequence[str], rows: Sequence[scoring.EvalRow]) -> None:
    positives, negatives = scoring.split_rows(rows)
    try:
        settings = get_settings()
        model = "{}（thinking={}，temperature={}）".format(
            settings.llm_model, settings.llm_thinking,
            args.temperature if args.temperature is not None else settings.llm_temperature)
    except Exception as exc:  # noqa: BLE001 —— 只打印计划，配置缺也不该炸
        model = "读取配置失败：{}".format(exc)
    print("运行计划（--dry-run：不建索引、不调模型、不花钱）")
    print("  数据集：{}（{} 条：正 {} / 负 {}）{}".format(
        args.dataset, len(rows), len(positives), len(negatives),
        "；--limit {}".format(args.limit) if args.limit else ""))
    print("  变体：{} 个 -> {}".format(len(variants), "、".join(variants)))
    print("  生成模型：{}".format(model))
    print("  judge：{}{}".format(
        args.judge, "（落 eval/.judge_cache.json，可复用）" if args.judge == "llm" else "（不调 LLM）"))
    print("  流式：{}".format("是（量 TTFT）" if args.stream else "否（TTFT 记 None）"))
    print("  逐变体链路：")
    for variant in variants:
        print("    - {}：{}".format(scoring.VARIANT_LABELS[variant], VARIANT_CHAINS[variant]))
    n = len(rows) * len(variants)
    print("  预计调用：{} 条 x {} 档 = {} 次生成；judge 只判关键词漏判的样本（最多 3 次/条）".format(
        len(rows), len(variants), n))
    print("  成本估算：按 §6.5 实测单条生成约 ¥0.0043 估，本轮生成约 ¥{:.4f}（judge 另计）".format(
        0.0043 * n))


def main(argv: Sequence[str] | None = None) -> int:
    force_utf8()
    args = build_parser().parse_args(argv)
    variants = resolve_variants(args.variant)

    dataset = Path(args.dataset)
    if not dataset.exists():
        print(
            "数据集不存在：{}（评测集由数据集负责人产出；先等 eval/dataset.jsonl，"
            "或用 --dataset eval/.smoke.jsonl 自检）".format(dataset),
            file=sys.stderr,
        )
        return 2
    try:
        rows = scoring.load_dataset(dataset)
    except ValueError as exc:
        print("数据集不合法：{}".format(exc), file=sys.stderr)
        return 2
    if args.limit is not None:
        if args.limit <= 0:
            print("--limit 必须 > 0", file=sys.stderr)
            return 2
        rows = rows[: args.limit]

    if args.dry_run:
        _print_plan(args, variants, rows)
        return 0

    settings = get_settings()
    if args.temperature is not None:
        settings = settings.model_copy(update={"llm_temperature": args.temperature})
        print("已覆盖 llm_temperature={}（只影响本次评测）".format(settings.llm_temperature),
              file=sys.stderr)
    judge = JudgeRunner(
        settings, _EVAL_DIR / JUDGE_CACHE_NAME, enabled=(args.judge == "llm"),
    )
    try:
        harness = Harness(settings, stream=args.stream)
    except Exception as exc:  # noqa: BLE001 —— 索引没建好时给出可执行的提示
        print(
            "初始化检索组件失败：{}: {}\n"
            "检查 data/lancedb 与 data/bm25.pkl 是否已建好（python -m app.index.indexer）。".format(
                type(exc).__name__, exc),
            file=sys.stderr,
        )
        return 2

    runs: list[VariantRun] = []
    rc = 0
    try:
        for variant in variants:
            runs.append(run_variant(variant, rows, harness, judge))
    except KeyboardInterrupt:
        print("\n已中断：保留已跑完的变体结果", file=sys.stderr)
        rc = 130
    except Exception:  # noqa: BLE001 —— 保留现场，至少把已跑完的写成报告
        traceback.print_exc()
        rc = 1
    finally:
        judge.save()

    if not runs:
        return rc or 1

    table = scoring.render_table([run.metrics for run in runs])
    console = sys.stderr if args.json else sys.stdout
    print("", file=console)
    print(table, file=console)
    print("", file=console)

    report = render_report(runs, rows, judge, args=args, settings=settings)
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print("报告已写入：{}".format(out), file=console)
    except OSError as exc:
        print("报告写入失败：{}".format(exc), file=sys.stderr)
        rc = rc or 1

    if args.dump_results:
        try:
            _dump_results(Path(args.dump_results), runs, rows)
            print("逐条结果已写入：{}".format(args.dump_results), file=console)
        except OSError as exc:
            print("逐条结果写入失败：{}".format(exc), file=sys.stderr)
            rc = rc or 1

    if args.json:
        print(json.dumps(
            _json_summary(runs, rows, judge, args, settings),
            ensure_ascii=False, indent=2,
        ))
    return rc


if __name__ == "__main__":
    sys.exit(main())
