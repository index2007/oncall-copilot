"""POST /v1/diagnose：只做非流式诊断（流式统一走 /v1/ask）。

为什么留这个"重复"的端点：设计清单 §6 的兼容口径——已有调用方（脚本、webhook 回填、
评测脚本）认的是 /v1/diagnose 的同步 JSON 响应；流式是新增能力，用 /v1/ask 的 stream
开关表达。两个端点在实现上共用 ask.run_diagnose()，行为不会分叉，也不多一份日志逻辑。

stream=true 在这里直接 400，而不是悄悄降级成同步响应：调用方明明要流式、却收到一个
JSON，是最难排查的一类"看起来成功"的故障；400 把用哪个端点说清楚。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.ask import run_diagnose
from app.api.deps import get_db, get_diagnoser
from app.api.schemas import AskRequest, DiagnoseResponse
from app.core.pipeline import Diagnoser
from app.obs.db import DB
from app.obs.logging import new_request_id

router = APIRouter(tags=["diagnose"])

ENDPOINT = "/v1/diagnose"


@router.post("/v1/diagnose", response_model=DiagnoseResponse)
async def diagnose_query(
    req: AskRequest,
    request: Request,
    db: DB = Depends(get_db),
    diagnoser: Diagnoser = Depends(get_diagnoser),
) -> DiagnoseResponse:
    """非流式诊断：返回卡 + 计划 + 校验报告 + 分段耗时。"""
    if req.stream:
        raise HTTPException(
            status_code=400,
            detail="stream=true 请改用 POST /v1/ask（/v1/diagnose 只提供非流式响应）",
        )
    return await run_diagnose(
        diagnoser,
        req,
        request_id=getattr(request.state, "request_id", None) or new_request_id(),
        db=db,
        endpoint=ENDPOINT,
    )
