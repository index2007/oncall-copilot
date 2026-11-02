"""GET /v1/metrics/summary：近 N 次请求的延迟 / 拒答 / 成本 + 索引版本。

百分位、拒答率、成本都在 DB.metrics_summary() 里算（最近秩，不插值），这里只做
两件事：① 校验 window（1..2000，越界 422，别让一个人用 window=10**9 把 SQLite
扫一遍）；② 补 index 字段——被问"上一次索引什么时候跑的、花了多久、加了几篇"时，
一个请求就能答上来，不用去翻 data/oncall.db。

index 的形状与 /v1/health 完全一致（从 app.api.health 取同一个函数），不做第二套。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_app_settings, get_db
from app.api.health import index_snapshot
from app.api.schemas import MetricsSummaryResponse
from app.config import Settings
from app.obs.db import DB

router = APIRouter(tags=["ops"])

MAX_WINDOW = 2000  # 上限：2099 块的语料按 2000 次窗口统计已经覆盖整个压测史


@router.get("/v1/metrics/summary", response_model=MetricsSummaryResponse, summary="延迟 / 拒答 / 成本汇总")
def metrics_summary(
    window: int | None = Query(
        None,
        ge=1,
        le=MAX_WINDOW,
        description="统计最近多少次请求；缺省取 settings.metrics_window（默认 200），上限 2000",
    ),
    settings: Settings = Depends(get_app_settings),
    db: DB = Depends(get_db),
) -> MetricsSummaryResponse:
    # 缺省值走配置（settings.metrics_window=200），显式传参再按 1..2000 校验；
    # 配置被改成越界值时也在这里夹住，保证响应里的 window 永远是真实生效值。
    used = settings.metrics_window if window is None else window
    used = max(1, min(int(used), MAX_WINDOW))

    data = db.metrics_summary(used)
    data["index"] = index_snapshot(db)
    return MetricsSummaryResponse(**data)
