"""API 层的请求 / 响应模型（只做边界转换，不重复 core 里的领域模型）。

为什么单独一个文件而不是塞进 core/schemas.py：core 里的 DiagnosisCard 是
"诊断结果"这个领域对象，会被 pipeline、guard、评测脚本共用；这里的
DiagnoseResponse / SSEFrame 是"HTTP 边界"的契约，字段随接口演进变，两者的
变更频率不同。边界模型负责三件事：校验入参、裁剪出参（不把内部 hits 全吐出去）、
给 /docs 提供可直接试的示例。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.schemas import Citation, CitationReport, DiagnosisCard, QueryPlan

SSEEvent = Literal["meta", "citations", "delta", "done", "error"]


class AskRequest(BaseModel):
    """POST /v1/ask 与 /v1/diagnose 的共用入参。"""

    query: str = Field(..., min_length=1, max_length=4000, description="报错文本 / 告警文本")
    stream: bool = Field(False, description="true 时走 SSE（text/event-stream）")
    top_n: int | None = Field(None, ge=1, le=20, description="最终上下文条数，缺省取 TOP_K_FINAL")
    allow_llm: bool | None = Field(
        None, description="Query 理解是否允许调 LLM；缺省按 settings 与 query 长度决定"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "Pod 一直重启，kubectl describe 显示 OOMKilled，exit code 137",
                    "stream": False,
                }
            ]
        }
    }


class ContextItem(BaseModel):
    """SSE citations 帧里的一条候选上下文（比 ScoredChunk 窄，不吐全文）。"""

    chunk_id: str
    title: str = ""
    heading_path: str = ""
    url: str = ""
    score: float = 0.0
    rerank_score: float | None = None
    preview: str = ""


class DiagnoseResponse(BaseModel):
    """非流式响应：卡片 + 计划 + 校验报告 + 分段耗时。"""

    diagnosis_id: str = ""
    card: DiagnosisCard
    plan: QueryPlan
    citations: list[Citation] = Field(default_factory=list)
    report: CitationReport | None = None
    abstained_at: str | None = None
    regenerated: bool = False
    timings: dict[str, float] = Field(default_factory=dict)


class SSEFrame(BaseModel):
    """SSE 帧的 data 部分统一包一层，客户端按 event 分支处理。

    帧序（设计清单 §6.5）：meta → citations → delta* → done；任何一帧之前都可以
    用 error 收尾（HTTP 200 已发出后无法改状态码，错误只能走事件）。
    """

    event: SSEEvent
    data: dict[str, Any] = Field(default_factory=dict)


class AskResponse(DiagnoseResponse):
    """目前与 DiagnoseResponse 同构；保留独立名字，方便以后只调一处。"""


class SourceResponse(BaseModel):
    """GET /v1/sources/{chunk_id}：一段原文 + 可点开的 url。"""

    chunk_id: str
    doc_id: str = ""
    source: str = ""
    title: str = ""
    heading_path: str = ""
    url: str = ""
    anchor: str = ""
    order: int = 0
    token_count: int = 0
    text: str = ""


class AdminIndexRequest(BaseModel):
    full: bool = Field(False, description="true 全量重建；false 走 doc_hash 增量")
    dry_run: bool = Field(False, description="只算要做什么，不写索引")
    limit: int | None = Field(None, ge=1, description="只处理前 N 篇（调试用）")


class AdminIndexResponse(BaseModel):
    ok: bool = True
    docs_scanned: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    chunks_total: int = 0
    chunks_written: int = 0
    api_calls: int = 0
    cache_hits: int = 0
    tokens: int = 0
    cost_cny: float = 0.0
    elapsed_s: float = 0.0
    note: str = ""


class MetricsSummaryResponse(BaseModel):
    """GET /v1/metrics/summary：近 N 次请求的延迟 / 拒答 / 成本。"""

    window: int
    requests: int = 0
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    abstain_rate: float = 0.0
    degraded_rate: float = 0.0
    error_rate: float = 0.0
    avg_cost_cny: float = 0.0
    total_cost_cny: float = 0.0
    avg_completion_tokens: float = 0.0
    by_endpoint: dict[str, int] = Field(default_factory=dict)
    index: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = "ok"
    version: str = ""
    checks: dict[str, Any] = Field(default_factory=dict)
    index: dict[str, Any] = Field(default_factory=dict)


class WebhookAlertSummary(BaseModel):
    fingerprint: str = ""
    alertname: str = ""
    status: str = ""
    severity: str = ""
    diagnosis_id: str = ""
    abstained: bool = False
    causes: list[str] = Field(default_factory=list)
    summary: str = ""
    latency_ms: int = 0
    error: str = ""


class WebhookResponse(BaseModel):
    """POST /webhook/alertmanager 的响应（v4 payload 里有几条 alerts 就回几条）。"""

    accepted: int = 0
    skipped: int = 0
    results: list[WebhookAlertSummary] = Field(default_factory=list)
    latency_ms: int = 0


class WebhookAlert(BaseModel):
    """Alertmanager v4 alert 里我们真正会用到的字段（其余忽略）。"""

    status: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str = ""
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str = ""


class WebhookPayload(BaseModel):
    """Alertmanager v4 webhook payload（只取所需字段，未声明的会被忽略）。"""

    version: str = ""
    groupKey: str = ""
    truncatedAlerts: int = 0
    status: str = ""
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[WebhookAlert] = Field(default_factory=list)
