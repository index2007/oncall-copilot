"""GET /v1/health：存活 + 索引版本（清单 §7"存活 + 索引版本"）。

为什么降级也返回 HTTP 200：健康检查是给探针 / 负载均衡用的，状态码只表达"进程在不在、
能不能接连接"。索引缺失、语料为空这类问题要让值班从 checks 里看到具体坏在哪一项，但
503 会让探针直接把实例摘掉——降级时检索仍可用、拒答仍生效，不该从负载均衡上消失。
所以 status=degraded 只体现在响应体里，HTTP 恒为 200（README 健康检查一节写明这条取舍）。

checks 的口径（全部只读，不触发索引、不调任何外部 API）：
- db：SQLite 能查（表没建 = fail，说明还没跑过索引）
- vector_store：LanceDB 目录存在且非空
- bm25：bm25.pkl 存在且非空
- content：语料目录下 .md 的篇数
- provider_keys：DEEPSEEK / SF 两个 key 是否配置（只报 true/false，值永远不出现在响应里）

为什么 provider_keys 也算关键项：没配 key 时服务只能检索、不能生成诊断卡，对外就是
一个"半残"的服务；把它算进 degraded，比伪装成 ok 诚实。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_app_settings, get_db
from app.api.schemas import HealthResponse
from app.config import Settings
from app.obs.db import DB

router = APIRouter(tags=["ops"])

# 这五项全 ok 才是 status=ok；顺序同时决定 README / 排障时的阅读顺序。
CRITICAL_CHECKS: tuple[str, ...] = ("db", "vector_store", "bm25", "content", "provider_keys")


def _service_version() -> str:
    """版本号只有一个来源：app/main.py 的 VERSION。

    反向 import 是安全的：main 导入本模块时 main 已在 sys.modules 里；单独 import
    本模块（比如只跑 health 的单测）拿不到就返回空串，不让健康检查因此 500。
    """
    try:
        from app.main import VERSION
    except Exception:  # noqa: BLE001 - 没有 main 时只是不报版本
        return ""
    return str(VERSION)


def _check_db(db: DB) -> dict[str, Any]:
    """能不能查到 documents 表。表不存在（没跑过索引）算 fail，而不是 500。"""
    try:
        stats = db.doc_stats()
    except Exception as exc:  # noqa: BLE001 - 任何 sqlite 错误都归为这一项失败
        return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
    return {"ok": True, "docs": int(stats["docs"]), "chunks": int(stats["chunks"])}


def index_snapshot(db: DB) -> dict[str, Any]:
    """索引摘要：documents 的汇总 + 最近一次 index_run。

    metrics 用的是同一个形状（从本模块 import），外部消费方读 /v1/health 与
    /v1/metrics/summary 时看到的是同一套字段，不用记两套。
    """
    try:
        stats = db.doc_stats()
        runs = db.last_runs(1)
    except Exception:  # noqa: BLE001 - 索引状态读不到时给空摘要，不让接口挂掉
        return {}
    last = runs[0] if runs else None
    return {
        "docs": int(stats["docs"]),
        "chunks": int(stats["chunks"]),
        "runs": int(stats["runs"]),
        "last_run_at": (last.get("finished_at") or last.get("started_at")) if last else None,
        "last_run": None
        if not last
        else {
            "run_id": last.get("run_id"),
            "trigger": last.get("trigger"),
            "full_rebuild": bool(last.get("full_rebuild")),
            "added": int(last.get("added") or 0),
            "updated": int(last.get("updated") or 0),
            "deleted": int(last.get("deleted") or 0),
            "skipped": int(last.get("skipped") or 0),
            "chunks_written": int(last.get("chunks_written") or 0),
            "elapsed_s": round(float(last.get("elapsed_s") or 0.0), 2),
            "finished_at": last.get("finished_at") or "",
        },
    }


def _check_vector_store(settings: Settings) -> dict[str, Any]:
    """只判"目录在且非空"：真正的 chunk 计数要开 LanceDB 表，健康检查不该这么重。"""
    path = settings.lancedb_path
    if not path.is_dir():
        return {"ok": False, "path": str(path), "entries": 0}
    entries = sum(1 for _ in path.iterdir())
    return {"ok": entries > 0, "path": str(path), "entries": entries}


def _check_bm25(settings: Settings) -> dict[str, Any]:
    path = settings.bm25_path
    size = path.stat().st_size if path.is_file() else 0
    return {"ok": path.is_file() and size > 0, "path": str(path), "bytes": size}


def _check_content(settings: Settings) -> dict[str, Any]:
    path = settings.content_path
    pages = sum(1 for _ in path.rglob("*.md")) if path.is_dir() else 0
    return {"ok": pages > 0, "path": str(path), "pages": pages}


def _check_provider_keys(settings: Settings) -> dict[str, Any]:
    """只回布尔：key 的值一旦进了响应体，日志 / 截图 / issue 都可能带着它。"""
    deepseek = bool(settings.deepseek_api_key)
    sf = bool(settings.sf_api_key)
    return {"ok": deepseek and sf, "deepseek": deepseek, "sf": sf}


@router.get("/v1/health", response_model=HealthResponse, summary="存活 + 索引版本")
def health(
    settings: Settings = Depends(get_app_settings),
    db: DB = Depends(get_db),
) -> HealthResponse:
    checks: dict[str, Any] = {
        "db": _check_db(db),
        "vector_store": _check_vector_store(settings),
        "bm25": _check_bm25(settings),
        "content": _check_content(settings),
        "provider_keys": _check_provider_keys(settings),
    }
    degraded = [name for name in CRITICAL_CHECKS if not checks[name].get("ok")]
    return HealthResponse(
        status="degraded" if degraded else "ok",
        version=_service_version(),
        checks=checks,
        index=index_snapshot(db),
    )
