"""FastAPI 入口：把 app/api/* 的路由拼起来，挂日志、请求 id 与兜底异常处理。

为什么入口这么薄：D5 的价值全在 api/* 与 core/streaming.py，main.py 只做三件事——
① 生命周期初始化（日志、SQLite 建表）；② 给每个请求发 request_id 并写回响应头
（排查线上问题时，用户能直接把 id 报给你）；③ 未捕获异常统一转 JSON 500，
保证前端拿到的永远是 JSON，不是 uvicorn 的纯文本堆栈。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.api import admin, ask, diagnose, health, metrics, sources, webhook
from app.api.deps import get_app_settings
from app.core.http_client import close_client
from app.obs.db import DB
from app.obs.logging import new_request_id, setup_logging

VERSION = "0.5.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_app_settings()
    setup_logging(settings.log_level, settings.log_file or None)
    db = DB(settings.data_path / "oncall.db")
    db.init_schema()
    stats = db.doc_stats()
    logger.info(
        "oncall-copilot v{} 启动 | docs={} chunks={} | lancedb={}",
        VERSION,
        stats["docs"],
        stats["chunks"],
        settings.lancedb_path,
    )
    yield
    # 共享 httpx 连接池要显式关，否则 pytest / uvicorn --reload 会留一堆未关闭连接
    close_client()
    logger.info("oncall-copilot 关闭")


app = FastAPI(
    title="On-call Copilot",
    version=VERSION,
    description=(
        "运维故障诊断服务：报错文本 → Query 理解 → 向量 + BM25 双路召回 → RRF → "
        "Cross-Encoder 重排 → LLM 生成带引用的结构化诊断卡（引用后校验 + 拒答 + SSE 流式）。"
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """每个请求一个 request_id + 一条访问日志（耗时、状态码）。"""
    rid = new_request_id()
    request.state.request_id = rid
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - 交给下面的兜底处理器
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = rid
    logger.info(
        "{} {} -> {} | {:.0f} ms | rid={}",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        rid,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底：未捕获异常也返回 JSON，并把 request_id 带上（客户端可直接报给你）。"""
    rid = getattr(request.state, "request_id", "-")
    logger.exception("未捕获异常 rid={} path={}: {}", rid, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal error", "request_id": rid, "error": type(exc).__name__},
    )


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "oncall-copilot", "version": VERSION, "docs": "/docs", "health": "/v1/health"}


for module in (ask, diagnose, webhook, sources, admin, metrics, health):
    app.include_router(module.router)
