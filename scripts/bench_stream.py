"""SSE 流式基准：TTFT / 整卡 / 分段 / 成本。

跑法（先起服务）：
    ONCALL_ENV_FILE=../.secrets/oncall-copilot.env ./.venv/Scripts/python.exe scripts/bench_stream.py
    ... --repeat 2 --json

单次约 0.005 元，9 条默认用例约 0.05 元。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

DEFAULT_QUERIES = [
    "Pod 一直 CrashLoopBackOff，日志最后一行是 exit code 137，节点内存充足",
    "容器被 OOMKilled，节点的可用内存还有 30%",
    "nginx 报 upstream prematurely closed connection while reading response header",
    "nginx 502 Bad Gateway，后端是 uwsgi，access log 里 upstream_response_time 是 -",
    "MySQL 报 ERROR 1213 (40001): Deadlock found when trying to get lock",
    "MySQL 连接数打满，报 Too many connections",
    "redis 报 MISCONF Redis is configured to save RDB snapshots but is currently not able to persist",
    "Redis 报 OOM command not allowed when used memory > 'maxmemory'",
    "kubectl describe pod 显示 0/3 nodes are available: 3 Insufficient cpu，怎么排查",
]


def one(client: httpx.Client, base: str, query: str) -> dict:
    t0 = time.perf_counter()
    ttft_ms = None
    done = None
    n_delta = 0
    with client.stream("POST", base + "/v1/ask", json={"query": query, "stream": True}) as resp:
        resp.raise_for_status()
        event = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                if event == "delta":
                    n_delta += 1
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                elif event == "done":
                    done = json.loads(line[5:].strip())
                elif event == "error":
                    raise RuntimeError(json.loads(line[5:].strip()))
    wall_ms = (time.perf_counter() - t0) * 1000
    card = (done or {}).get("card") or {}
    return {
        "query": query,
        "wall_ms": round(wall_ms, 1),
        "ttft_ms": None if ttft_ms is None else round(ttft_ms, 1),
        "done_ttft_ms": (done or {}).get("ttft_ms"),
        "latency_ms": (done or {}).get("latency_ms"),
        "abstained": card.get("abstained"),
        "causes": len(card.get("causes") or []),
        "degraded": card.get("degraded"),
        "cost_cny": (done or {}).get("cost_cny"),
        "completion_tokens": card.get("completion_tokens"),
        "deltas": n_delta,
        "timings": (done or {}).get("timings"),
    }


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8077")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows: list[dict] = []
    with httpx.Client(timeout=120.0) as client:
        for _ in range(args.repeat):
            for query in DEFAULT_QUERIES:
                try:
                    row = one(client, args.base, query)
                except Exception as exc:  # noqa: BLE001 - 基准脚本，失败要留痕
                    row = {"query": query, "error": type(exc).__name__ + ": " + str(exc)}
                rows.append(row)
                if row.get("error"):
                    print("ERROR " + row["query"][:40] + " -> " + row["error"])
                else:
                    print(
                        "%7.1f  ttft=%-7s deltas=%-3d causes=%d abstain=%-5s cost=%.4f  %s"
                        % (
                            row["wall_ms"],
                            row["ttft_ms"],
                            row["deltas"],
                            row["causes"],
                            row["abstained"],
                            row["cost_cny"] or 0.0,
                            row["query"][:34],
                        )
                    )
                time.sleep(1.0)

    ok = [r for r in rows if not r.get("error")]
    ttfts = [r["ttft_ms"] for r in ok if r["ttft_ms"]]
    walls = [r["wall_ms"] for r in ok]
    answered = [r for r in ok if not r["abstained"]]
    summary = {
        "runs": len(rows),
        "ok": len(ok),
        "errors": len(rows) - len(ok),
        "ttft_p50": pct(ttfts, 0.50),
        "ttft_p95": pct(ttfts, 0.95),
        "ttft_max": max(ttfts) if ttfts else 0.0,
        "wall_p50": pct(walls, 0.50),
        "wall_p95": pct(walls, 0.95),
        "wall_max": max(walls) if walls else 0.0,
        "abstain_rate": round(len(ok) - len(answered), 3) if ok else 0.0,
        "avg_cost_cny": round(statistics.fmean([r["cost_cny"] or 0.0 for r in ok]), 5) if ok else 0.0,
        "total_cost_cny": round(sum(r["cost_cny"] or 0.0 for r in ok), 4),
        "avg_completion_tokens": round(statistics.fmean([r["completion_tokens"] or 0 for r in answered]), 1) if answered else 0.0,
        "ttft_over_1200": sum(1 for t in ttfts if t > 1200),
        "wall_over_4000": sum(1 for w in walls if w > 4000),
    }
    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else (
        "TTFT p50=%.0f p95=%.0f max=%.0f (>1200ms: %d/%d)\n整卡 p50=%.0f p95=%.0f max=%.0f (>4000ms: %d/%d)\n拒答 %d/%d 平均 %.4f 元/次 合计 %.4f 元"
        % (
            summary["ttft_p50"], summary["ttft_p95"], summary["ttft_max"], summary["ttft_over_1200"], len(ttfts),
            summary["wall_p50"], summary["wall_p95"], summary["wall_max"], summary["wall_over_4000"], len(walls),
            len(ok) - len(answered), len(ok), summary["avg_cost_cny"], summary["total_cost_cny"],
        )
    ))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("written " + str(path))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
