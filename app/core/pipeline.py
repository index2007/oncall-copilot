"""D4 端到端编排：一句报错文本 → 一张带引用的诊断卡（非流式）。

链路（与设计清单 §2 一致）：

    analyze() → Retriever.retrieve() → rerank() → should_abstain() → generate() → verify_citations()

编排层故意不塞业务逻辑：每一段都能单独替换、单独计时。三个依赖（重排 / 生成 / 校验）
以可注入函数的形式传入，单测里换成假实现，跑真实链路时用默认实现。

为什么拒答判定放在生成之前：重排 Top1 都低于阈值，说明语料里没有能支撑结论的东西，
此时调 LLM 既浪费钱也必然诱导它编。这条路径不调 LLM，实测 <100 ms。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from loguru import logger

from app.config import Settings, get_settings
from app.core import guard as guard_mod
from app.core import reranker as rerank_mod
from app.core.query_understanding import analyze
from app.core.retriever import Retriever, RetrievalFailed
from app.core.schemas import CitationReport, DiagnosisCard, QueryPlan, ScoredChunk


@dataclass
class StageTiming:
    """每段耗时（毫秒）。面试问「时间花在哪」时直接给这张表。"""

    query_understanding_ms: float = 0.0
    retrieve_ms: float = 0.0
    rerank_ms: float = 0.0
    generate_ms: float = 0.0
    verify_ms: float = 0.0
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 1) for k, v in asdict(self).items()}


@dataclass
class DiagnoseResult:
    """一次诊断的完整现场：卡、计划、召回、上下文、校验报告、分段耗时。"""

    query: str
    plan: QueryPlan
    card: DiagnosisCard
    hits: list[ScoredChunk] = field(default_factory=list)
    context: list[ScoredChunk] = field(default_factory=list)
    report: CitationReport | None = None
    timings: StageTiming = field(default_factory=StageTiming)
    abstained_at: str | None = None  # retrieval / gate / generator / None
    regenerated: bool = False

    @property
    def abstained(self) -> bool:
        return bool(self.card.abstained)

    def summary_row(self) -> dict[str, Any]:
        """一行可打印的摘要（脚本与日志共用）。"""
        row: dict[str, Any] = {
            "abstained": self.abstained,
            "abstained_at": self.abstained_at,
            "degraded": self.card.degraded,
            "causes": len(self.card.causes),
            "citations": len(self.card.citations),
            "hits": len(self.hits),
            "context": len(self.context),
            "cost_cny": round(self.card.cost_cny, 6),
            "latency_ms": self.card.latency_ms,
        }
        for k, v in self.timings.as_dict().items():
            row["t_" + k] = v
        return row


def _abstain_card(plan: QueryPlan, hits: list[ScoredChunk], reason: str, top_score: float | None) -> DiagnosisCard:
    """拒答卡：结构完整，但不给 causes（§7：abstained=true 时 causes 必须为空）。"""
    summary = "语料里没有足够证据支撑结论，按设计拒绝作答（" + reason + "）。"
    if top_score is not None:
        summary += " 重排最高分 " + format(top_score, ".3f") + "，低于阈值。"
    if hits:
        summary += " 最接近的原文片段：" + hits[0].preview(60)
    return DiagnosisCard(
        abstained=True,
        summary=summary,
        causes=[],
        abort_reason=reason,
        degraded=False,
    )


def _retrieval_failed_card(plan: QueryPlan, exc: RetrievalFailed) -> DiagnosisCard:
    """两路召回都不可用时的拒答卡（D8/F1）。

    与"检索不到"的区别写在 abort_reason 上：retrieval_failed = 查不了（索引/密钥/依赖坏了），
    no_hits = 查得到但没有相关内容。degraded 同时置真，观测侧能单独统计这一类，
    不会把它混进拒答准确率的分母里当成模型判错。
    """
    summary = "召回失败（向量路与 BM25 路都不可用），按设计拒绝作答（retrieval_failed）。"
    detail = str(exc)[:200]
    if detail:
        summary += " 失败原因：" + detail
    return DiagnosisCard(
        abstained=True,
        summary=summary,
        causes=[],
        abort_reason="retrieval_failed",
        degraded=True,
    )


def _degraded_legs(retriever: Retriever) -> list[str]:
    """本次召回有几路是降级回来的（D8/F1：Retriever.last_stats["degraded_legs"]）。"""
    stats = getattr(retriever, "last_stats", None) or {}
    return [str(x) for x in (stats.get("degraded_legs") or [])]


def _degraded_card(card: DiagnosisCard, context: list[ScoredChunk], report: CitationReport) -> DiagnosisCard:
    """引用校验两次都不过：拒答 + 只回原文片段（ADR-4 最后一档 / ADR-5②）。

    abstained=True 是 ADR-5② 的原话（"引用全落空 → 拒答"）：模型给了结论但一条都
    落不到资料上，等于没答。degraded=True 同时置上，观测侧才能把这种拒答与"检索不到"
    分开统计；causes 清空，客户端只会看到原文片段。
    """
    lines: list[str] = ["模型结论的引用全部未通过校验，按设计拒绝猜测（只回原文片段）："]
    for i, c in enumerate(context[:3], start=1):
        lines.append(str(i) + ". " + c.preview(120))
    bad = ", ".join(report.invalid_ids[:5]) if report.invalid_ids else "（有结论缺证据）"
    lines.append("未通过的引用：" + bad)
    return card.model_copy(
        update={
            "abstained": True,
            "degraded": True,
            "causes": [],
            "citations": list(card.citations),
            "abort_reason": "citations_invalid",
            "summary": "\n".join(lines),
        }
    )


def _sanitize_card(card: DiagnosisCard, context: list[ScoredChunk]) -> DiagnosisCard:
    """校验通过后的净化：证据指向不存在块的 cause 整条丢掉。"""
    from app.core.generator import sanitize_card

    return sanitize_card(card, context)


class Diagnoser:
    """端到端诊断器。依赖全部可注入，便于单测与变体对比。"""

    def __init__(
        self,
        retriever: Retriever,
        settings: Settings | None = None,
        *,
        rerank_fn: Callable[..., list[ScoredChunk]] | None = None,
        generate_fn: Callable[..., DiagnosisCard] | None = None,
    ):
        self.retriever = retriever
        self.settings = settings or get_settings()
        self._rerank = rerank_fn or rerank_mod.rerank
        self._generate = generate_fn or generate_default

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Diagnoser":
        s = settings or get_settings()
        return cls(retriever=Retriever.from_settings(s), settings=s)

    # -- 主流程 ------------------------------------------------
    def run(self, text: str, *, allow_llm: bool | None = None, top_n: int | None = None) -> DiagnoseResult:
        """一次完整诊断。任何一段失败都按降级链走，不向调用方抛业务异常。"""
        s = self.settings
        t0 = time.perf_counter()
        timings = StageTiming()

        t = time.perf_counter()
        plan = analyze(text, settings=s, allow_llm=allow_llm)
        timings.query_understanding_ms = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        try:
            hits = self.retriever.retrieve(plan, limit=s.top_k_fuse)
        except RetrievalFailed as exc:
            # 查不了 ≠ 没命中（D8/F1）：不 500，给一张结构完整的降级拒答卡，HTTP 层照常 200。
            # 真正不可预期的异常仍向上抛，由 app/main.py 的 500 兜底（那里的语义是"服务出事了"）。
            logger.warning("召回失败，转降级拒答卡：{}", exc)
            timings.retrieve_ms = (time.perf_counter() - t) * 1000
            timings.total_ms = (time.perf_counter() - t0) * 1000
            card = _retrieval_failed_card(plan, exc).model_copy(
                update={"latency_ms": int(timings.total_ms)}
            )
            result = DiagnoseResult(query=text, plan=plan, card=card, hits=[], context=[], timings=timings)
            result.abstained_at = "retrieval"
            return result
        timings.retrieve_ms = (time.perf_counter() - t) * 1000
        # 单路降级要读一次：后面的重排/生成不该再去碰 retriever 的内部状态。
        partial_legs = _degraded_legs(self.retriever)

        t = time.perf_counter()
        context = self._rerank(plan.search_text, hits, top_n=top_n or s.top_k_final) if hits else []
        timings.rerank_ms = (time.perf_counter() - t) * 1000

        result = DiagnoseResult(query=text, plan=plan, card=DiagnosisCard(), hits=hits, context=context, timings=timings)

        # 闸门一：检索为空或最高分低于阈值 → 不调 LLM
        # 重排降级时 rerank_score 全为 None，分数会退回 RRF 量级（1/61≈0.016），此时比对阈值必然误拒，
        # 所以降级那一次跳过闸门一，把判断交给生成侧 + 引用校验（ADR-5 的双保险本来就该这样兜）。
        rerank_degraded = bool(context) and context[0].degraded
        if not rerank_degraded and guard_mod.should_abstain(context, s.abstain_threshold):
            top = _top_score(context)
            card = _abstain_card(plan, context, "low_confidence" if context else "no_hits", top)
            timings.total_ms = (time.perf_counter() - t0) * 1000
            result.card = card.model_copy(
                update={"latency_ms": int(timings.total_ms), "degraded": bool(partial_legs)}
            )
            result.abstained_at = "gate"
            return result

        allowed = {c.chunk_id for c in context if c.chunk_id}

        # 生成 + 引用校验（校验不过 → 重生成一次 → 再不过降级）
        card = DiagnosisCard()
        report: CitationReport | None = None
        for attempt in (1, 2):
            t = time.perf_counter()
            try:
                card = self._generate(plan.search_text, context)
            except Exception as exc:  # noqa: BLE001 - 生成模块自身应降级，这里兜底
                logger.warning("生成失败，转降级卡：{}: {}", type(exc).__name__, exc)
                timings.generate_ms += (time.perf_counter() - t) * 1000
                card = DiagnosisCard(degraded=True, summary="生成失败：" + str(exc)[:200])
                break
            timings.generate_ms += (time.perf_counter() - t) * 1000

            t = time.perf_counter()
            report = guard_mod.verify_citations(card, allowed)
            timings.verify_ms += (time.perf_counter() - t) * 1000

            if report.ok or card.abstained:
                break
            logger.info(
                "引用校验未通过（invalid={}, orphan={}），第 {} 次重生成",
                report.invalid_ids,
                report.orphan_causes,
                attempt,
            )
            result.regenerated = True

        if report is not None and not report.ok and not card.abstained:
            # ADR-5②：引用全落空 → 拒答（不是"降级成原文片段但仍然作答"）。degraded
            # 同时置 True，观测侧才能区分"没证据拒答"与"模型编引用被拦下"。
            card = _degraded_card(card, context, report)

        # 通过校验的卡才净化：把证据指向不存在块的 cause 整条丢掉（ADR-4 后置过滤）。
        # 校验已在原始卡上做过，这里的净化只负责"展示的卡不出现编造的引用"。
        if report is not None and report.ok and not card.abstained and not card.degraded:
            card = _sanitize_card(card, context)

        # 生成器自己判"引用全落空"（sanitize=True 那条路）时已经 abstain，这里补上
        # degraded，保证两种入口（编排层两次校验失败 / 生成器内部判定）在观测上同形。
        if card.abstained and card.abort_reason == "citations_invalid":
            card = card.model_copy(update={"degraded": True})

        # 生成侧自己的拒答（模型认为证据不足）要能区分出来：观测侧看
        # gate 拒答 = 没调 LLM，generator 拒答 = 调了但模型判断证据不够。
        if card.abstained and result.abstained_at is None:
            result.abstained_at = "generator"

        if rerank_degraded or partial_legs:
            card = card.model_copy(update={"degraded": True})

        # 单路召回降级在卡上留痕。不覆盖已有的 abstain 原因：low_confidence /
        # citations_invalid 比"少了一路召回"更有信息量，混在一起反而查不出根因。
        if partial_legs and not card.abstained and not card.abort_reason:
            card = card.model_copy(update={"abort_reason": "retrieval_partial: " + ", ".join(partial_legs)})

        timings.total_ms = (time.perf_counter() - t0) * 1000
        result.card = card.model_copy(update={"latency_ms": int(timings.total_ms)})
        result.report = report
        return result


def _top_score(chunks: list[ScoredChunk]) -> float | None:
    """拒答判定用的分数（回退规则只在 guard.best_score 一处实现）。"""
    return guard_mod.best_score(chunks)


def generate_default(query: str, chunks: list[ScoredChunk]) -> DiagnosisCard:
    """默认生成实现（延迟导入：没配密钥时也不该在 import 阶段炸）。

    sanitize=False：把模型原始输出交给编排层做引用校验（ADR-4），否则拿已净化的卡
    去"校验"永远通过，重生成分支是死代码。净化在重生成之后、返回之前做。
    """
    from app.core.generator import generate

    return generate(query, chunks, sanitize=False)
