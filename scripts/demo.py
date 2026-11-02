"""一条命令跑完"报错文本 -> 诊断卡"，把每段的真实耗时打出来。

为什么不是 curl：/v1/ask 的流式与非流式响应都很大，curl 看不清分段耗时，也没法
一眼对比"首字"和"整卡"。这个脚本按 SSE 帧边界计时，末行给出门槛判定：
首字 <= 1200 ms、整卡 <= 4500 ms（设计清单 v1.5 的 P50 口径）。

用法：
    python scripts/demo.py                                  # 默认 OOM 样例，走 SSE
    python scripts/demo.py --symptom "nginx 502 upstream..." # 自定义症状
    python scripts/demo.py --no-stream                       # 走 /v1/diagnose 非流式
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"
TTFT_BUDGET_MS = 1200
TOTAL_BUDGET_MS = 4500

DEFAULT_SYMPTOM = (
    "Pod checkout-api 反复重启，kubectl describe 显示 OOMKilled，"
    "exit code 137，容器内存 limit 512Mi，监控里 RSS 峰值 498Mi。"
)


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def _print_card(card: dict[str, Any], timings: dict[str, Any], ttft_ms: int | None) -> None:
    print("\n--- 诊断卡 ---")
    print("diagnosis_id : {}".format(_fmt(card.get("diagnosis_id"))))
    print("abstained    : {}  degraded: {}".format(card.get("abstained"), card.get("degraded")))
    print("summary      : {}".format(_fmt(card.get("summary"))))
    for i, cause in enumerate(card.get("causes") or [], 1):
        print("  {}. {} (confidence={})".format(i, cause.get("title"), cause.get("confidence")))
        if cause.get("explanation"):
            print("     {}".format(cause.get("explanation")))
        for ev in cause.get("evidence") or []:
            print("     evidence: {}".format(ev.get("chunk_id")))
    for i, cmd in enumerate(card.get("verify_commands") or [], 1):
        print("  verify {}. {}".format(i, cmd))
    for i, action in enumerate(card.get("actions") or [], 1):
        print("  action {}. {}".format(i, action))
    print("\n--- 引用 ---")
    for cit in card.get("citations") or []:
        print("  {} | {}".format(cit.get("chunk_id"), cit.get("url")))

    print("\n--- 耗时 ---")
    if ttft_ms is not None:
        print("  首 token : {} ms".format(ttft_ms))
    for key, value in (timings or {}).items():
        if isinstance(value, (int, float)):
            print("  {:<22}{} ms".format(key, round(float(value), 1)))
    print("  成本     : ￥{}".format(card.get("cost_cny")))


def run_stream(url: str, symptom: str, timeout: float) -> int:
    ttft_ms: int | None = None
    card: dict[str, Any] = {}
    timings: dict[str, Any] = {}
    started = time.perf_counter()
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST",
            url.rstrip("/") + "/v1/ask",
            json={"query": symptom, "stream": True},
        ) as resp:
            resp.raise_for_status()
            event = ""
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[len("data:"):].strip())
                if event == "delta" and ttft_ms is None:
                    ttft_ms = int((time.perf_counter() - started) * 1000)
                elif event == "meta":
                    plan = data.get("plan") or {}
                    print("Query 理解 : resource={} intent={} llm={} | 条款 {} 条".format(
                        plan.get("resource_type"), plan.get("intent"),
                        plan.get("need_llm"), len(plan.get("keywords") or [])))
                elif event == "citations":
                    print("已定引用   : {} 条（开流前就发了）".format(len(data.get("context") or [])))
                elif event == "done":
                    card = data.get("card") or {}
                    timings = data.get("timings") or {}
                    ttft_ms = data.get("ttft_ms") or ttft_ms
                elif event == "error":
                    print("error 帧   : {} | {}".format(data.get("stage"), data.get("message")))
                    return 1
    total_ms = int((time.perf_counter() - started) * 1000)
    _print_card(card, timings, ttft_ms)
    print("  整卡     : {} ms".format(total_ms))
    ok = (ttft_ms is not None and ttft_ms <= TTFT_BUDGET_MS and total_ms <= TOTAL_BUDGET_MS)
    print("\n门槛: 首字 {} ms (<= {} ms) / 整卡 {} ms (<= {} ms) -> {}".format(
        _fmt(ttft_ms), TTFT_BUDGET_MS, total_ms, TOTAL_BUDGET_MS, "PASS" if ok else "FAIL"))
    return 0 if ok else 2


def run_blocking(url: str, symptom: str, timeout: float) -> int:
    started = time.perf_counter()
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url.rstrip("/") + "/v1/diagnose", json={"query": symptom})
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    resp.raise_for_status()
    body = resp.json()
    _print_card(body.get("card") or {}, body.get("timings") or {}, None)
    print("  整卡     : {} ms (实际)；卡片 latency_ms = {} ms".format(
        elapsed_ms, (body.get("card") or {}).get("latency_ms")))
    ok = elapsed_ms <= TOTAL_BUDGET_MS
    print("\n门槛: 整卡 {} ms (<= {} ms) -> {}".format(
        elapsed_ms, TOTAL_BUDGET_MS, "PASS" if ok else "FAIL"))
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一站式演示：症状 -> 诊断卡")
    parser.add_argument("--url", default=DEFAULT_URL, help="服务根地址，默认 %(default)s")
    parser.add_argument("--symptom", default=DEFAULT_SYMPTOM, help="自定义症状文本")
    parser.add_argument("--no-stream", action="store_true", help="走 /v1/diagnose 非流式")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    print("query: {}".format(args.symptom))
    if args.no_stream:
        return run_blocking(args.url, args.symptom, args.timeout)
    return run_stream(args.url, args.symptom, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
