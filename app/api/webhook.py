"""POST /webhook/alertmanager：兼容 Alertmanager v4 payload 的告警入口。

为什么不自定义 /v1/alert：真实环境里 Prometheus 推的就是这个格式，服务端零改动就能接；
演示用 scripts/simulate_alert.py 发的是同格式 payload（不是自造协议）。这里只取
labels / annotations / status / fingerprint，其余字段由 pydantic extra=ignore 丢掉。

处理口径（README"告警接入"一节有对应说明）：
- 只处理 status=="firing"；"resolved" 不调诊断、不计费，计入 skipped；
- 一次 webhook 最多诊断 MAX_ALERTS 条（默认 5）：告警风暴时按顺序处理前几条，
  其余计入 skipped，避免一次 webhook 串起几十次 LLM 调用；
- 每条告警单独 try/except：一条失败不影响其它条，失败那条 accepted 不计数、
  error 字段写清异常类型 + 消息，并照常落 query_log（status="error"），
  这样 /v1/metrics/summary 的 error_rate 有分母；
- 诊断是同步阻塞调用（LLM 数秒），用 run_in_threadpool 丢线程池：组内顺序处理，
  但不霸占事件循环；
- results 里一条对应"实际尝试诊断的一条告警"（跳过的不占位），失败那条 status="error"。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from loguru import logger
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, get_diagnoser
from app.api.schemas import WebhookAlert, WebhookAlertSummary, WebhookPayload, WebhookResponse
from app.core.pipeline import Diagnoser
from app.obs.db import DB
from app.obs.logging import new_request_id

router = APIRouter(tags=["alerts"])

MAX_ALERTS = 5  # 一次 webhook 最多诊断几条；其余算 skipped

# 标签 -> 中文说明。拼成自然语言而不是 JSON：这段文本要进 embedding 与 LLM prompt，
# 塞 JSON 只会引入引号、括号这类无意义 token，还会稀释 alertname 的语义。
_OBJECT_LABELS: tuple[tuple[str, str], ...] = (
    ("cluster", "集群"),
    ("namespace", "命名空间"),
    ("pod", "Pod"),
    ("node", "节点"),
    ("instance", "实例"),
    ("container", "容器"),
    ("job", "任务"),
    ("service", "服务"),
    ("app", "应用"),
)
_OBJECT_LABEL_KEYS = frozenset(key for key, _ in _OBJECT_LABELS)
_RESERVED_LABELS = frozenset({"alertname", "severity"})
_RESERVED_ANNOTATIONS = frozenset({"summary", "description"})


def build_diagnosis_text(alert: WebhookAlert) -> str:
    """把一条 v4 alert 拼成一段自然语言（summary + description + 关键标签）。"""
    labels = dict(alert.labels or {})
    ann = dict(alert.annotations or {})
    alertname = labels.get("alertname") or "未命名告警"
    severity = labels.get("severity") or ""
    headline = (ann.get("summary") or "").strip()

    head = "告警 " + alertname
    if severity:
        head += "（级别 " + severity + "）"
    if headline:
        head += "：" + headline
    lines = [head]

    desc = (ann.get("description") or "").strip()
    if desc:
        lines.append("现象与影响：" + desc)

    objects = [cn + " " + labels[key] for key, cn in _OBJECT_LABELS if labels.get(key)]
    objects += [
        key + "=" + value
        for key, value in sorted(labels.items())
        if key not in _OBJECT_LABEL_KEYS and key not in _RESERVED_LABELS and value
    ]
    if objects:
        lines.append("涉及对象：" + "，".join(objects))

    extras = [
        key + "=" + value[:200]
        for key, value in sorted(ann.items())
        if key not in _RESERVED_ANNOTATIONS and value
    ]
    if extras:
        lines.append("补充信息：" + "，".join(extras[:5]))
    return "\n".join(lines)


def _log_alert(
    db: DB,
    *,
    request_id: str,
    text: str,
    card: Any,
    latency_ms: int,
    status: str,
    abstained_at: str | None = None,
) -> None:
    """落一行 query_log；写日志失败不许把告警请求带崩（与 db.log_query 的约定一致）。"""
    try:
        db.log_query(
            request_id=request_id,
            endpoint="/webhook/alertmanager",
            query=text,
            answer_len=len(getattr(card, "summary", "") or ""),
            abstained=bool(getattr(card, "abstained", False)),
            degraded=bool(getattr(card, "degraded", False)),
            status=status,
            latency_ms=round(float(latency_ms), 1),
            prompt_tokens=int(getattr(card, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(card, "completion_tokens", 0) or 0),
            cost_cny=float(getattr(card, "cost_cny", 0.0) or 0.0),
            abstained_at=abstained_at,
        )
    except Exception as exc:  # noqa: BLE001 - 可观测性不能反噬主流程
        logger.warning("webhook 落 query_log 失败：{}: {}", type(exc).__name__, exc)


@router.post("/webhook/alertmanager", response_model=WebhookResponse, summary="Alertmanager v4 告警入口")
async def receive_alertmanager(
    payload: WebhookPayload,
    request: Request,
    db: DB = Depends(get_db),
    diagnoser: Diagnoser = Depends(get_diagnoser),
) -> WebhookResponse:
    started_all = time.perf_counter()
    request_id = getattr(request.state, "request_id", None) or new_request_id()
    accepted = 0
    skipped = 0
    processed = 0
    results: list[WebhookAlertSummary] = []

    for alert in payload.alerts:
        if alert.status != "firing":
            skipped += 1
            continue
        if processed >= MAX_ALERTS:
            skipped += 1
            continue
        processed += 1

        text = build_diagnosis_text(alert)
        row = WebhookAlertSummary(
            fingerprint=alert.fingerprint,
            alertname=alert.labels.get("alertname", ""),
            status=alert.status,
            severity=alert.labels.get("severity", ""),
        )
        card: Any = None
        abstained_at: str | None = None
        started = time.perf_counter()
        try:
            result = await run_in_threadpool(diagnoser.run, text)
            card = result.card
            # 假诊断器（测试/外部实现）可能没有这个字段，缺了就当没记
            abstained_at = getattr(result, "abstained_at", None)
            row.diagnosis_id = card.diagnosis_id
            row.abstained = bool(card.abstained)
            row.degraded = bool(getattr(card, "degraded", False))
            row.causes = [cause.title for cause in card.causes]
            row.summary = card.summary
            accepted += 1
        except Exception as exc:  # noqa: BLE001 - 单条告警失败不影响其它条
            # 失败条的 status 用 "error" 而不是原始 "firing"：调用方看 results 就知道
            # 这条没出诊断卡，具体原因在 error 字段（异常类型 + 消息）。
            row.status = "error"
            row.error = type(exc).__name__ + ": " + str(exc)
            logger.exception(
                "webhook 诊断失败 alertname={} fingerprint={}: {}",
                row.alertname,
                row.fingerprint,
                exc,
            )
        row.latency_ms = int((time.perf_counter() - started) * 1000)
        _log_alert(
            db,
            request_id=request_id,
            text=text,
            card=card,
            latency_ms=row.latency_ms,
            status="ok" if card is not None else "error",
            abstained_at=abstained_at,
        )
        results.append(row)

    return WebhookResponse(
        accepted=accepted,
        skipped=skipped,
        results=results,
        latency_ms=int((time.perf_counter() - started_all) * 1000),
    )
