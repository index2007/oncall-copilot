"""D5 接口验收探针：真的起服务、真的打 7 个接口，把每个接口的耗时与结论打印成表。

用法：
    # 一个终端起服务
    ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8077
    # 另一个终端
    python scripts/d5_probe.py            # 全量（会花约 6 分钱：3 次诊断 + 2 条告警 + 1 次流式）
    python scripts/d5_probe.py --no-llm   # 只打不需要模型的接口（/health、/sources、admin dry_run）

它不替代单测：单测证明分支正确，探针证明"起真服务、走真网络"这条路上没有断。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

# 允许直接 python scripts/d5_probe.py 跑（脚本目录不在 sys.path 里时 import app 会失败）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8077")

SAMPLES = [
    ("k8s", "Pod 突然重启，kubectl describe 里 Last State 是 Terminated，Reason: OOMKilled，Exit Code: 137，节点内存还很充裕"),
    ("nginx", "nginx 502 Bad Gateway，error.log 里 upstream prematurely closed connection while reading response header from upstream"),
    ("mysql", "MySQL 报 ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction，业务侧库存扣减接口偶发超时"),
]

WEBHOOK_PAYLOAD = {
    "version": "4",
    "groupKey": "{}:{alertname=\"KubePodCrashLooping\"}",
    "status": "firing",
    "receiver": "oncall-copilot",
    "groupLabels": {"alertname": "KubePodCrashLooping"},
    "commonLabels": {"severity": "critical", "namespace": "prod"},
    "commonAnnotations": {},
    "externalURL": "http://alertmanager:9093",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "KubePodCrashLooping", "namespace": "prod", "pod": "order-api-7d9f-2xk4", "severity": "critical"},
            "annotations": {"summary": "Pod order-api 反复重启", "description": "5 分钟内重启 7 次，最后状态 CrashLoopBackOff"},
            "startsAt": "2026-11-02T12:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus:9090/graph?g0.expr=kube_pod_container_status_restarts_total",
            "fingerprint": "a1b2c3d4e5f60001",
        },
        {
            "status": "resolved",
            "labels": {"alertname": "NodeDiskPressure", "instance": "10.0.0.8:9100", "severity": "warning"},
            "annotations": {"summary": "节点磁盘压力已恢复"},
            "startsAt": "2026-11-02T11:00:00Z",
            "endsAt": "2026-11-02T11:50:00Z",
            "generatorURL": "http://prometheus:9090/graph?g0.expr=node_filesystem_avail_bytes",
            "fingerprint": "a1b2c3d4e5f60002",
        },
    ],
}


def _ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _sample_chunk_id() -> str:
    """从向量库直接取一条 chunk_id，用于 /v1/sources/{chunk_id} 的成功分支。"""
    from app.config import get_settings

    import lancedb

    s = get_settings()
    db = lancedb.connect(str(s.lancedb_path))
    table = db.open_table(s.table_name)
    try:  # lancedb 的普通扫描；装了 pylance 才能 to_lance，所以先试 search
        rows = table.search().limit(1).to_list()
    except Exception:  # noqa: BLE001 - 退化成走一次检索，反正只取一个 id
        from app.core.query_understanding import analyze
        from app.core.retriever import Retriever

        hits = Retriever.from_settings(s).retrieve(analyze("Pod OOMKilled 容器退出码 137"), limit=1)
        return hits[0].chunk_id if hits else ""
    return str(rows[0]["chunk_id"]) if rows else ""


def check_health(c: httpx.Client) -> dict:
    t = time.perf_counter()
    r = c.get(BASE + "/v1/health")
    body = r.json()
    return {
        "接口": "GET /v1/health",
        "状态": r.status_code,
        "耗时": _ms(t),
        "结论": body.get("status", "?") + " / checks=" + ",".join(k for k, v in body.get("checks", {}).items() if v.get("ok")),
    }


def check_metrics(c: httpx.Client) -> dict:
    t = time.perf_counter()
    r = c.get(BASE + "/v1/metrics/summary", params={"window": 50})
    body = r.json()
    return {
        "接口": "GET /v1/metrics/summary",
        "状态": r.status_code,
        "耗时": _ms(t),
        "结论": "requests=%s ttft_p50=%s 整卡_p50=%s 拒答率=%s" % (
            body.get("requests"),
            body.get("ttft_p50_ms"),
            body.get("latency_p50_ms"),
            body.get("abstain_rate"),
        ),
    }


def check_sources(c: httpx.Client, chunk_id: str) -> list[dict]:
    out = []
    t = time.perf_counter()
    r = c.get(BASE + "/v1/sources/" + chunk_id)
    body = r.json()
    out.append({
        "接口": "GET /v1/sources/{chunk_id}",
        "状态": r.status_code,
        "耗时": _ms(t),
        "结论": "命中 %s，正文 %d 字" % (body.get("chunk_id", "?")[:16], len(body.get("text") or "")),
    })
    t = time.perf_counter()
    r = c.get(BASE + "/v1/sources/c_does_not_exist")
    out.append({
        "接口": "GET /v1/sources/{bad}",
        "状态": r.status_code,
        "耗时": _ms(t),
        "结论": str(r.json().get("detail")),
    })
    return out


def check_admin(c: httpx.Client) -> dict:
    t = time.perf_counter()
    r = c.post(BASE + "/v1/admin/index", json={"dry_run": True, "limit": 5})
    body = r.json()
    return {
        "接口": "POST /v1/admin/index (dry_run)",
        "状态": r.status_code,
        "耗时": _ms(t),
        "结论": "scanned=%s added=%s updated=%s chunks_total=%s api_calls=%s elapsed=%ss" % (
            body.get("docs_scanned"),
            body.get("added"),
            body.get("updated"),
            body.get("chunks_total"),
            body.get("api_calls"),
            body.get("elapsed_s"),
        ),
    }


def check_webhook(c: httpx.Client) -> dict:
    t = time.perf_counter()
    r = c.post(BASE + "/webhook/alertmanager", json=WEBHOOK_PAYLOAD)
    body = r.json()
    first = (body.get("results") or [{}])[0]
    return {
        "接口": "POST /webhook/alertmanager",
        "状态": r.status_code,
        "耗时": _ms(t),
        "结论": "accepted=%s skipped=%s 首条:diagnosis=%s abstained=%s causes=%s" % (
            body.get("accepted"),
            body.get("skipped"),
            str(first.get("diagnosis_id"))[:12],
            first.get("abstained"),
            (first.get("causes") or [None])[0],
        ),
    }


def check_diagnose(c: httpx.Client, tag: str, text: str) -> dict:
    t = time.perf_counter()
    r = c.post(BASE + "/v1/diagnose", json={"query": text}, timeout=120)
    body = r.json()
    card = body.get("card") or {}
    return {
        "接口": "POST /v1/diagnose [%s]" % tag,
        "状态": r.status_code,
        "耗时": _ms(t),
        "结论": "abstained=%s causes=%d ttft=%s 整卡=%s ￥%.4f 首因=%s" % (
            card.get("abstained"),
            len(card.get("causes") or []),
            card.get("ttft_ms"),
            card.get("latency_ms"),
            float(card.get("cost_cny") or 0),
            ((card.get("causes") or [{}])[0].get("title") or "-")[:28],
        ),
    }


def check_stream(c: httpx.Client, text: str) -> dict:
    """真 SSE：量首 token（到第一个 delta 帧）与整卡（done 帧）。"""
    started = time.perf_counter()
    first_delta_ms = None
    done: dict = {}
    events: list[str] = []
    payload = {"query": text, "stream": True}
    with c.stream("POST", BASE + "/v1/ask", json=payload, timeout=120) as resp:
        if resp.status_code != 200:
            resp.read()
            return {"接口": "POST /v1/ask (stream)", "状态": resp.status_code, "耗时": _ms(started), "结论": resp.text[:120]}
        for line in resp.iter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
                events.append(name)
            elif line.startswith("data:"):
                if events and events[-1] == "delta" and first_delta_ms is None:
                    first_delta_ms = _ms(started)
                if events and events[-1] == "done":
                    done = json.loads(line.split(":", 1)[1].strip())
    card = done.get("card") or {}
    return {
        "接口": "POST /v1/ask (stream)",
        "状态": 200,
        "耗时": _ms(started),
        "结论": "首token=%s ms done.ttft=%s 整卡=%s ms ￥%.4f 帧序=%s" % (
            first_delta_ms,
            done.get("ttft_ms"),
            done.get("latency_ms"),
            float(done.get("cost_cny") or 0),
            "→".join([e for e in events if e != "delta"]),
        ),
        "_card": card,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="跳过需要模型调用的接口（省钱）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows: list[dict] = []
    with httpx.Client(timeout=120) as c:
        try:
            rows.append(check_health(c))
        except Exception as exc:  # noqa: BLE001
            print("服务没起来？%s: %s" % (type(exc).__name__, exc))
            return 2
        rows.append(check_metrics(c))
        chunk_id = _sample_chunk_id()
        if chunk_id:
            rows.extend(check_sources(c, chunk_id))
        rows.append(check_admin(c))
        if not args.no_llm:
            rows.append(check_webhook(c))
            for tag, text in SAMPLES:
                rows.append(check_diagnose(c, tag, text))
            rows.append(check_stream(c, SAMPLES[0][1]))
            rows.append(check_metrics(c))

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("%-34s %-6s %-8s %s" % ("接口", "状态", "耗时ms", "结论"))
        for r in rows:
            print("%-34s %-6s %-8s %s" % (r["接口"], r["状态"], r["耗时"], r["结论"]))

    bad = [r for r in rows if int(r["状态"]) >= 400]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
