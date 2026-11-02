"""POST /v1/admin/index：同步触发一次增量索引，返回 IndexReport 的映射。

为什么同步（而不是 BackgroundTasks）：索引与服务共用同一个 index_incremental，
扔后台会出现"响应说成功了、实际失败没人看"的问题；本项目演示场景下就地等
（增量二次运行 <1 s，全量 98 s）是可接受的。响应里带 elapsed_s，README 的
"已知缺陷"写明了"同步执行、生产应改后台任务"。真要做对：改成任务队列
（Celery / RQ）或 BackgroundTasks + GET /v1/admin/index/{job_id} 查状态。

为什么必须 run_in_threadpool：index_incremental 是同步阻塞函数（HTTP 调 embedding、
写 LanceDB、落 BM25），直接在 async def 里调会把事件循环卡死，连 /v1/health 都探不到。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_app_settings
from app.api.schemas import AdminIndexRequest, AdminIndexResponse
from app.config import Settings
from app.index.indexer import IndexReport, index_incremental

router = APIRouter(tags=["admin"])

SYNC_CAVEAT = "同步执行，生产环境应改为后台任务"


def _to_response(report: IndexReport) -> AdminIndexResponse:
    """IndexReport（索引器内部对象）→ HTTP 契约。字段一一对应，只做四舍五入。"""
    note = (report.note or "").strip()
    return AdminIndexResponse(
        ok=True,
        docs_scanned=int(report.docs_scanned),
        added=int(report.added),
        updated=int(report.updated),
        deleted=int(report.deleted),
        skipped=int(report.skipped),
        chunks_total=int(report.chunks_total),
        chunks_written=int(report.chunks_written),
        api_calls=int(report.api_calls),
        cache_hits=int(report.cache_hits),
        tokens=int(report.tokens),
        cost_cny=round(float(report.cost_cny), 6),
        elapsed_s=round(float(report.elapsed_s), 2),
        note=(note + "；" + SYNC_CAVEAT) if note else SYNC_CAVEAT,
    )


@router.post("/v1/admin/index", response_model=AdminIndexResponse, summary="触发增量索引")
async def admin_index(
    req: AdminIndexRequest,
    settings: Settings = Depends(get_app_settings),
) -> AdminIndexResponse:
    try:
        report = await run_in_threadpool(
            index_incremental,
            settings=settings,
            full=req.full,
            limit=req.limit,
            dry_run=req.dry_run,
            trigger="api",
        )
    except Exception as exc:  # noqa: BLE001 - 统一转 500；原因只进日志，不回给调用方
        logger.exception("admin/index 失败：{}", exc)
        raise HTTPException(status_code=500, detail="internal error") from exc
    return _to_response(report)
