"""POST /v1/ask：同步诊断 + SSE 流式诊断，共用一个入参（AskRequest.stream 开关）。

两条路径共用同一套 core：同步走 pipeline.Diagnoser.run()，流式走 core.streaming.stream_diagnose()。
这一层只负责三件事——生成 request_id、把事件包成 SSE、落 query_log（写失败不影响响应）。

为什么流式要放进一个线程 + 队列，而不是直接在 async 生成器里迭代：
retriever / rerank / LLM 都是阻塞调用，直接在事件循环里跑会把整个进程卡住（一个流式请求
就能让 /v1/health 失去响应）。把生产端丢进线程池、消费端在事件循环里读队列，既能并发，
又能在客户端断开时用 stop 标志让生产端在下一个片段边界退出。

SSE 的三个响应头都不是可选的：
- Content-Type: text/event-stream —— sse-starlette 负责（Starlette 会补 charset）；
- Cache-Control: no-cache —— 中间代理不得缓存流；
- X-Accel-Buffering: no —— nginx 默认缓冲代理响应，不关掉首 token 会被攒到整段生成完。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, get_diagnoser
from app.api.schemas import AskRequest, DiagnoseResponse
from app.core.pipeline import Diagnoser
from app.core.streaming import stream_diagnose
from app.obs.db import DB
from app.obs.logging import new_request_id

router = APIRouter(tags=["ask"])

ENDPOINT = "/v1/ask"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def log_query_safe(db: DB, *, request_id: str, endpoint: str, query: str, **fields: Any) -> None:
    """落一行 query_log；失败只警告。

    日志表是观测用的旁路：它挂了也不能让用户拿不到答案（SQLite 锁、磁盘满都会发生）。
    所以这里吞异常，但一定留 warning——否则"为什么 metrics 少了请求"会查半天。
    """
    try:
        db.log_query(request_id=request_id, endpoint=endpoint, query=query, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("query_log 写入失败（不影响响应）：{}: {}", type(exc).__name__, exc)


async def run_diagnose(
    diagnoser: Diagnoser,
    req: AskRequest,
    *,
    request_id: str,
    db: DB,
    endpoint: str,
) -> DiagnoseResponse:
    """同步诊断 + 落日志。/v1/ask(stream=false) 与 /v1/diagnose 共用这一条路径。"""
    started = time.perf_counter()
    try:
        # 阻塞链路丢进线程池：一个 2~3 s 的 LLM 调用不该让事件循环停摆
        result = await run_in_threadpool(
            diagnoser.run, req.query, allow_llm=req.allow_llm, top_n=req.top_n
        )
    except Exception as exc:  # noqa: BLE001 - 先留痕再抛给 main.py 的兜底处理器
        logger.warning("诊断失败 rid={}: {}: {}", request_id, type(exc).__name__, exc)
        log_query_safe(
            db,
            request_id=request_id,
            endpoint=endpoint,
            query=req.query,
            status="error",
            latency_ms=int(round((time.perf_counter() - started) * 1000)),
        )
        raise

    resp = DiagnoseResponse(
        diagnosis_id=result.card.diagnosis_id,
        card=result.card,
        plan=result.plan,
        citations=result.card.citations,
        report=result.report,
        abstained_at=result.abstained_at,
        regenerated=result.regenerated,
        timings=result.timings.as_dict(),
    )
    log_query_safe(
        db,
        request_id=request_id,
        endpoint=endpoint,
        query=req.query,
        plan_json=result.plan.model_dump_json(),
        hit_chunk_ids=",".join(c.chunk_id for c in result.hits if c.chunk_id),
        context_chunk_ids=",".join(c.chunk_id for c in result.context if c.chunk_id),
        answer_len=len(result.card.summary or ""),
        abstained=result.card.abstained,
        degraded=result.card.degraded,
        status="ok",
        ttft_ms=result.card.ttft_ms,
        latency_ms=result.card.latency_ms,
        prompt_tokens=result.card.prompt_tokens,
        completion_tokens=result.card.completion_tokens,
        cost_cny=float(result.card.cost_cny or 0.0),
        # 分段耗时与拒答来源落库：/v1/metrics/summary 的 stages / abstain_sources 靠这两列，
        # 不落库就只能现算，事后无法复现"P50 里检索占多少"。
        timings_json=json.dumps(result.timings.as_dict(), ensure_ascii=False),
        abstained_at=result.abstained_at,
    )
    return resp


def _sse_response(
    diagnoser: Diagnoser,
    req: AskRequest,
    request_id: str,
    db: DB,
) -> EventSourceResponse:
    """把一个同步事件流包成 EventSourceResponse。

    帧的 event 名严格只有 meta / citations / delta / done / error；
    data 一律是 JSON 字符串（sse-starlette 对 str 原样输出，对 dict 走 json.dumps
    且遇到 pydantic 模型会炸，所以 core 层已经把模型 dump 成 dict）。
    """
    started = time.perf_counter()

    async def frames():
        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        sentinel = object()
        state: dict[str, Any] = {"plan": None, "context_ids": [], "card": None, "status": "ok"}

        def put(item: Any) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # 事件循环已经关了（进程退出中），丢弃即可
                pass

        def pump() -> None:
            gen = stream_diagnose(
                diagnoser,
                req.query,
                top_n=req.top_n,
                allow_llm=req.allow_llm,
                request_id=request_id,
            )
            try:
                for item in gen:
                    if stop.is_set():  # 客户端已断开：下一个片段边界就收手
                        break
                    put(item)
            except BaseException as exc:  # noqa: BLE001 - 兜底：上游没料到的异常也要变成 error 帧
                if not stop.is_set():
                    logger.warning("流式编排异常 rid={}: {}: {}", request_id, type(exc).__name__, exc)
                    # 这里是"编排线程自己炸了"，不是某一段的已知失败——各段失败在
                    # stream_diagnose 里已经变成带准确 stage 的 error 帧（D8/F2）。
                    put(("error", {"stage": "internal", "message": type(exc).__name__ + ": " + str(exc)[:300]}))
            finally:
                gen.close()
                put(sentinel)

        # 生产端在线程里跑；消费端只认队列（不等这个 future，否则首字要等整卡结束）。
        # 线程无法被强杀：客户端断开时只能置 stop 让它在下一次 put 前收手，
        # 最坏情况要等当前这次 LLM 读超时（llm_timeout_s）。
        future = loop.run_in_executor(None, pump)

        def _on_pump_done(fut: Any) -> None:
            # 取一次异常，避免线程里逃出来的异常变成 "exception was never retrieved"
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc is not None:
                logger.warning("流式生产端退出 rid={}: {}: {}", request_id, type(exc).__name__, exc)

        future.add_done_callback(_on_pump_done)

        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                event, data = item
                if event == "meta":
                    state["plan"] = data.get("plan")
                elif event == "citations":
                    state["context_ids"] = [c.get("chunk_id", "") for c in (data.get("context") or [])]
                elif event == "done":
                    state["card"] = data.get("card") or {}
                    state["hit_ids"] = data.get("hit_chunk_ids") or []
                    state["timings"] = data.get("timings") or {}
                    state["abstained_at"] = data.get("abstained_at")
                elif event == "error":
                    state["status"] = "error"
                yield {"event": event, "data": json.dumps(data, ensure_ascii=False)}
        except (asyncio.CancelledError, GeneratorExit):
            # 客户端断开：停止生产端，finally 里落 aborted
            state["status"] = "aborted"
            raise
        finally:
            stop.set()
            if not future.done():
                future.cancel()  # 已在跑时是 no-op；至少拦住还没开始执行的情况
            card = state.get("card") or {}
            plan = state.get("plan") or {}
            log_query_safe(
                db,
                request_id=request_id,
                endpoint=ENDPOINT,
                query=req.query,
                plan_json=json.dumps(plan, ensure_ascii=False) if plan else "",
                # hit_chunk_ids 来自 done 帧额外带的原始召回集；context_chunk_ids 是真正喂给
                # 模型的那几条。两者都留，事后才能区分"召回了但没进上下文"与"压根没召回到"。
                hit_chunk_ids=",".join(cid for cid in (state.get("hit_ids") or []) if cid),
                context_chunk_ids=",".join(cid for cid in state["context_ids"] if cid),
                answer_len=len(str(card.get("summary") or "")),
                abstained=bool(card.get("abstained")),
                degraded=bool(card.get("degraded")),
                status=state["status"],
                ttft_ms=card.get("ttft_ms"),
                latency_ms=card.get("latency_ms")
                if card.get("latency_ms") is not None
                else int(round((time.perf_counter() - started) * 1000)),
                prompt_tokens=int(card.get("prompt_tokens") or 0),
                completion_tokens=int(card.get("completion_tokens") or 0),
                cost_cny=float(card.get("cost_cny") or 0.0),
                timings_json=json.dumps(state.get("timings") or {}, ensure_ascii=False),
                abstained_at=state.get("abstained_at"),
            )

    return EventSourceResponse(frames(), headers=SSE_HEADERS)


@router.post("/v1/ask")
async def ask(
    req: AskRequest,
    request: Request,
    db: DB = Depends(get_db),
    diagnoser: Diagnoser = Depends(get_diagnoser),
):
    """一次问诊：stream=false 返回 DiagnoseResponse；stream=true 返回 SSE。

    request_id 必须复用中间件生成的那个，否则响应头 X-Request-ID、query_log
    与日志里的 rid 会变成三个不同的值。
    """
    request_id = getattr(request.state, "request_id", None) or new_request_id()
    if not req.stream:
        return await run_diagnose(diagnoser, req, request_id=request_id, db=db, endpoint=ENDPOINT)
    return _sse_response(diagnoser, req, request_id, db)
