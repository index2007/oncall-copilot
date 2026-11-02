"""D5-A 流式编排：一次问诊 → 一串 (event, data) 事件。

帧序（设计清单 §6.5）：

    meta → citations → delta* → done

任何阶段异常都用 error 收尾并结束（HTTP 200 已经发出，状态码改不了，错误只能走事件）。

为什么单独一个模块、而不是给 pipeline.run() 加个 stream=True：run() 的语义是"一次调用
返回一张卡"，每个分支都是同步返回；流式要求把同一条链路切成可观察的阶段、把首 token
暴露出去、并在引用校验不过时重生成。两者共用同一批依赖（retriever / rerank / generate）
与同一条降级链（guard / generator），但控制流完全不同——塞进一个函数会让 run() 的每个
return 前都要判断"现在是不是流式"。

TTFT 口径：从 stream_diagnose() 被调用的那一刻（time.perf_counter()）到第一个 delta
yield 出去。不含 FastAPI 建 SSE 响应、不含客户端网络——这是服务侧能测的最小口径，
客户端感知的 TTFT 会更大（多一跳网络 + 浏览器解析）。

拒答（闸门一）不调 LLM：重排 Top1 低于阈值说明语料里没有能支撑结论的东西，此时开流
既浪费钱又必然诱导模型编；这条路径只有 meta / citations / done 三帧，实测几十毫秒。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator

from loguru import logger

from app.config import Settings, get_settings
from app.core import guard as guard_mod
from app.core.generator import _parse_card, finalize_stream, open_stream
from app.core.pipeline import Diagnoser, StageTiming, _abstain_card, _degraded_card, _top_score
from app.core.query_understanding import analyze
from app.core.schemas import Citation, CitationReport, DiagnosisCard, QueryPlan, ScoredChunk
from app.obs.logging import current_request_id, new_request_id

def _raw_card(text: str) -> DiagnosisCard | None:
    """解析模型原始输出（未经 _sanitize 净化）；解析失败返回 None，由调用方回退。"""
    try:
        return _parse_card(text)
    except Exception:  # noqa: BLE001 - 非 JSON / 结构不对：交给 finalize_stream 的降级链
        return None


# 生成侧的注入点：(query, chunks, settings=...) -> 逐段产出正文的迭代器。
# 默认是 generator.open_stream；单测/演示换成假实现，既不联网也不 sleep。
StreamFactory = Callable[..., Iterator[str]]

# 一个 SSE 事件：(event, data)。data 必须是可 json.dumps 的纯数据——
# sse-starlette 的 ensure_bytes 只认 bytes/str/dict/list，pydantic 模型要在这一层 dump 掉。
Event = tuple[str, dict[str, Any]]

CONTEXT_PREVIEW_CHARS = 160


def _strip(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


def _citations_frame(context: list[ScoredChunk]) -> dict[str, Any]:
    """citations 帧：客户端在等首 token 时先把"这次依据了哪几段原文"画出来。

    两份数据是故意的：citations 是窄对象（chunk_id/title/heading_path/url），
    用于点开跳原文与标号；context 多带 score / rerank_score / preview，用于
    "为什么是这几段"的解释。全文不吐——那是 GET /v1/sources/{chunk_id} 的事。
    """
    citations = [
        Citation(
            chunk_id=c.chunk_id,
            title=c.title,
            heading_path=c.heading_path,
            url=c.url,
        ).model_dump()
        for c in context
        if (c.chunk_id or "").strip()
    ]
    items = [
        {
            "chunk_id": c.chunk_id,
            "title": c.title,
            "heading_path": c.heading_path,
            "url": c.url,
            "score": float(c.score or 0.0),
            "rerank_score": c.rerank_score,
            "preview": c.preview(CONTEXT_PREVIEW_CHARS),
        }
        for c in context
    ]
    return {"citations": citations, "context": items}


def _done_payload(
    card: DiagnosisCard,
    *,
    ttft_ms: int | None,
    timings: StageTiming,
    report: CitationReport | None,
    regenerated: bool,
    hits: list[ScoredChunk] | None = None,
) -> dict[str, Any]:
    """done 帧：与 /v1/ask 非流式响应同源的那张卡 + 流式专属的三个数。

    cost_cny / prompt_tokens 都来自卡本身（finalize_stream 里按 settings 单价算），
    这里只做转发，不重算——两处各算一遍，迟早一个用旧价、一个用新价。
    """
    return {
        "card": card.model_dump(),
        "ttft_ms": ttft_ms,
        "latency_ms": card.latency_ms,
        "cost_cny": float(card.cost_cny),
        "timings": timings.as_dict(),
        "report": report.model_dump() if report is not None else None,
        "regenerated": regenerated,
        # 原始召回集：帧契约里原本没有，但流式也要落 query_log 的 hit_chunk_ids。
        # 加一个客户端可忽略的字段，比让日志少一列、事后查不出"到底召回了什么"更值。
        "hit_chunk_ids": [c.chunk_id for c in (hits or []) if c.chunk_id],
    }


def _error(stage: str, exc: BaseException) -> Event:
    return ("error", {"stage": stage, "message": type(exc).__name__ + ": " + _strip(str(exc))})


def stream_diagnose(
    diagnoser: Diagnoser,
    text: str,
    *,
    top_n: int | None = None,
    allow_llm: bool | None = None,
    stream_factory: StreamFactory | None = None,
    request_id: str | None = None,
) -> Iterator[Event]:
    """流式诊断：产出 (event, data)，调用方负责包成 SSE。

    依赖全部来自传入的 diagnoser（retriever / _rerank / settings），生成侧按
    stream_factory → diagnoser.stream_fn → generator.open_stream 的顺序取，所以单测
    既能塞假 LLM，也能塞假 retriever，不需要真索引、不花钱。

    重生成口径（与 pipeline 一致但受流式约束）：引用校验不过 → 重新开流一次（最多 1 次），
    不发明新事件类型，客户端看到的是 delta 继续流，done 里 regenerated=true；
    两次都不过 → 走 pipeline._degraded_card，把原文片段摆出来、causes 清空。
    """
    s: Settings = getattr(diagnoser, "settings", None) or get_settings()
    started = time.perf_counter()
    rid = request_id or current_request_id()
    if rid in ("", "-"):
        rid = new_request_id()
    timings = StageTiming()

    # -- 1. Query 理解：完成后立刻发 meta（客户端此时能画出"在查什么"） ----
    try:
        t = time.perf_counter()
        plan: QueryPlan = analyze(text, settings=s, allow_llm=allow_llm)
        timings.query_understanding_ms = (time.perf_counter() - t) * 1000
    except Exception as exc:  # noqa: BLE001 - 规则侧理论上不抛，兜底仍要收口
        logger.warning("流式 Query 理解失败：{}: {}", type(exc).__name__, exc)
        yield _error("retrieve", exc)
        return

    yield ("meta", {"plan": plan.model_dump(), "request_id": rid})

    # -- 2. 召回 -----------------------------------------------------------
    try:
        t = time.perf_counter()
        hits: list[ScoredChunk] = diagnoser.retriever.retrieve(plan, limit=s.top_k_fuse)
        timings.retrieve_ms = (time.perf_counter() - t) * 1000
    except Exception as exc:  # noqa: BLE001
        logger.warning("流式召回失败：{}: {}", type(exc).__name__, exc)
        yield _error("retrieve", exc)
        return

    # -- 3. 重排 -----------------------------------------------------------
    try:
        t = time.perf_counter()
        context: list[ScoredChunk] = (
            diagnoser._rerank(plan.search_text, hits, top_n=top_n or s.top_k_final) if hits else []
        )
        timings.rerank_ms = (time.perf_counter() - t) * 1000
    except Exception as exc:  # noqa: BLE001
        logger.warning("流式重排失败：{}: {}", type(exc).__name__, exc)
        yield _error("rerank", exc)
        return

    # 重排完成、开流之前：引用面板先发出去，客户端不用等模型吐字
    yield ("citations", _citations_frame(context))

    # -- 4. 闸门一：低分直接拒答，不调 LLM ---------------------------------
    # 口径与 pipeline.run() 一致：重排降级那一次跳过闸门（rerank_score 回退成 RRF 分，
    # 比阈值必然误拒），交给生成侧 + 引用校验兜。
    rerank_degraded = bool(context) and bool(context[0].degraded)
    if not rerank_degraded and guard_mod.should_abstain(context, s.abstain_threshold):
        top = _top_score(context)
        card = _abstain_card(plan, context, "low_confidence" if context else "no_hits", top)
        timings.total_ms = (time.perf_counter() - started) * 1000
        card = card.model_copy(update={"latency_ms": int(timings.total_ms)})
        yield (
            "done",
            _done_payload(card, ttft_ms=None, timings=timings, report=None, regenerated=False, hits=hits),
        )
        return

    allowed = {c.chunk_id for c in context if c.chunk_id}
    factory: StreamFactory = stream_factory or getattr(diagnoser, "stream_fn", None) or open_stream

    # -- 5. 生成 + 引用校验（不过则重生成一次，再不过降级） ------------------
    card: DiagnosisCard | None = None
    report: CitationReport | None = None
    regenerated = False
    ttft_ms: int | None = None

    for attempt in (1, 2):
        t = time.perf_counter()
        buf: list[str] = []  # 本次尝试的完整正文；重生成时清空，只解析第二次的输出
        session = None
        try:
            session = factory(plan.search_text, context, settings=s)
            for delta in session:
                if not delta:
                    continue
                if ttft_ms is None:
                    # 首 token：从入口算起，包含 Query 理解 + 召回 + 重排 + 建上下文
                    ttft_ms = int(round((time.perf_counter() - started) * 1000))
                buf.append(delta)
                yield ("delta", {"text": delta})
        except Exception as exc:  # noqa: BLE001 - 网络 / 协议 / 解析错误都在这里收口
            logger.warning("流式生成失败（第 {} 次）：{}: {}", attempt, type(exc).__name__, exc)
            timings.generate_ms += (time.perf_counter() - t) * 1000
            yield _error("generate", exc)
            return
        finally:
            # 客户端断开 / 异常时也要把上游 HTTP 流关掉，否则连接会挂到超时
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 关流失败不该覆盖真实错误
                    pass

        timings.generate_ms += (time.perf_counter() - t) * 1000
        prompt_tokens = int(getattr(session, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(session, "completion_tokens", 0) or 0)
        model = str(getattr(session, "model", "") or s.llm_model)

        try:
            card = finalize_stream(
                "".join(buf),
                context,
                settings=s,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                started=started,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式收尾失败：{}: {}", type(exc).__name__, exc)
            yield _error("generate", exc)
            return

        t = time.perf_counter()
        try:
            # 校验对象是"未净化"的模型原始输出，不是 finalize_stream 的产物。
            # finalize_stream 里的 _sanitize 会把不在 context 里的引用与 evidence 全丢掉、
            # 把没有有效证据的 cause 整条删掉，于是净化后的卡永远校验通过——拿安全网自己
            # 的输出再问一遍安全网，重生成分支就成了摆设。两件事要分开：_sanitize 保证
            # "最终展示不出现编造的引用"，verify 判断"模型是在编还是在引"。
            # 非 JSON 时没有可比的原始卡，退回校验净化卡，降级由 json_invalid 那条路管。
            report = guard_mod.verify_citations(_raw_card("".join(buf)) or card, allowed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式引用校验失败：{}: {}", type(exc).__name__, exc)
            yield _error("verify", exc)
            return
        timings.verify_ms += (time.perf_counter() - t) * 1000

        # 只在"校验通过"或"已经重生成过一次"时收手。这里刻意不因 card.abstained 提前
        # 退出：generator._finalize 在证据全被净化掉时会自己给一张 abstained 卡
        # （abort_reason=citations_invalid），那是安全网的第一档，不是校验通过——
        # 让它短路就等于把"重生成一次"这一步删了。模型真的主动拒答时报告是 ok 的
        # （total=0、无 invalid），走的是上面那个分支。
        if report.ok or attempt == 2:
            break

        # 不发明 retry 事件：delta 继续流，done 里 regenerated=true 告诉客户端
        # "流里的字是过程，卡才是结论"。重生成时 buf 清空，只按第二次的输出解析。
        regenerated = True
        logger.info(
            "流式引用校验未通过（invalid={}, orphan={}），重生成一次",
            report.invalid_ids,
            report.orphan_causes,
        )

    if card is None:  # 理论不可达：循环至少走一次且异常都提前 return
        yield _error("generate", RuntimeError("没有产出任何卡片"))
        return

    if report is not None and not report.ok:
        # 两次都没过：放弃模型结论，只回原文片段（ADR-4 最后一档）。abstained 归 False——
        # 这次不是"没证据所以拒绝作答"，而是"有证据但模型在编引用"，计入 degrade 而不是 abstain，
        # 否则观测侧的拒答率会被幻觉引用污染。
        card = _degraded_card(card, context, report).model_copy(update={"abstained": False})
    if rerank_degraded:
        card = card.model_copy(update={"degraded": True})

    timings.total_ms = (time.perf_counter() - started) * 1000
    card = card.model_copy(update={"latency_ms": int(timings.total_ms), "ttft_ms": ttft_ms})
    yield (
        "done",
        _done_payload(card, ttft_ms=ttft_ms, timings=timings, report=report, regenerated=regenerated, hits=hits),
    )
