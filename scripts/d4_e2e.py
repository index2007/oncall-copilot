"""D4 验收脚本：非流式端到端出诊断卡，并把每段耗时打出来。

用法（在 oncall-copilot/ 下）：

    ./.venv/Scripts/python.exe scripts/d4_e2e.py                 # 3 条样例全跑
    ./.venv/Scripts/python.exe scripts/d4_e2e.py --query "..."   # 单条
    ./.venv/Scripts/python.exe scripts/d4_e2e.py --stage-only    # 只到重排，不调 LLM（省钱）
    ./.venv/Scripts/python.exe scripts/d4_e2e.py --json          # 机器可读
    ./.venv/Scripts/python.exe scripts/d4_e2e.py --repeat 3      # 每条样例跑 3 次，报 P50/P95

判定：整卡耗时 P95 ≤4.0 s（清单 §6.5，同时打印 worst）；拒答路径不调 LLM；引用全部落在本次上下文。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.core.pipeline import Diagnoser, DiagnoseResult  # noqa: E402

SAMPLES: list[str] = [
    "kubectl 看到一个 Pod 一直重启，kubectl describe pod 显示 Last State: Terminated, Reason: OOMKilled, exit code 137，节点内存还有富余",
    "业务反馈访问站点大量 502，nginx error log 里出现 upstream prematurely closed connection while reading response header from upstream",
    "MySQL 报错 ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction，业务是并发更新同一批订单",
]


def _print_result(res: DiagnoseResult, show_card: bool = True) -> None:
    row = res.summary_row()
    print("=" * 78)
    print("query:", res.query[:100])
    print("plan :", json.dumps(res.plan.brief(), ensure_ascii=False))
    print("time :", json.dumps(row, ensure_ascii=False))
    if res.report is not None:
        print(
            "cite : ok=%s total=%d valid=%d invalid=%s orphan=%s coverage=%.2f"
            % (res.report.ok, res.report.total, res.report.valid, res.report.invalid_ids, res.report.orphan_causes, res.report.coverage)
        )
    if res.context:
        print("top5 :")
        for c in res.context:
            score = c.rerank_score if c.rerank_score is not None else c.score
            print("       %-14s %6.3f  %s" % (c.chunk_id, score, (c.heading_path or c.title)[:52]))
    if show_card:
        print("card :")
        print(json.dumps(res.card.model_dump(), ensure_ascii=False, indent=2)[:1800])


def _pct(values: list[float], q: float) -> float:
    """最近秩百分位：q=50 即中位数。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = min(len(ordered) - 1, max(0, int(round(q / 100.0 * (len(ordered) - 1)))))
    return ordered[pos]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D4 端到端诊断卡验收")
    ap.add_argument("--query", default="", help="单条 query；不给则跑内置 3 条样例")
    ap.add_argument("--stage-only", action="store_true", help="只跑到重排与拒答判定，不调 LLM")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--repeat", type=int, default=1, help="每条样例重复跑几次（测 P50/P95）")
    args = ap.parse_args(argv)

    settings = get_settings()
    if not settings.sf_api_key:
        print("[!] SF_API_KEY 为空，检查 ONCALL_ENV_FILE 是否指向项目外的密钥文件", file=sys.stderr)
        return 2

    diagnoser = Diagnoser.from_settings(settings)
    queries = [args.query] if args.query else SAMPLES
    rows: list[dict] = []
    t_start = time.perf_counter()

    for q in queries:
        for _ in range(max(1, args.repeat)):
            res = diagnoser.run(q, allow_llm=not args.stage_only and None)
            row = res.summary_row()
            row["gen_ms"] = row["t_generate_ms"]
            row["prompt_tokens"] = res.card.prompt_tokens
            row["completion_tokens"] = res.card.completion_tokens
            row["retry"] = bool(res.regenerated)
            rows.append(row)
            if args.json:
                rows[-1]["query"] = res.query
                rows[-1]["card"] = res.card.model_dump()
                rows[-1]["citations_ok"] = bool(res.report.ok) if res.report is not None else None
            else:
                _print_result(res, show_card=not args.stage_only)

    elapsed = time.perf_counter() - t_start
    if args.json:
        print(json.dumps({"rows": rows, "elapsed_s": round(elapsed, 2)}, ensure_ascii=False, indent=2))
    else:
        print("=" * 78)
        print("%-4s %-8s %-7s %-9s %-8s %-8s %-7s" % ("#", "abstain", "causes", "latency", "gen", "out_tok", "cost"))
        for i, r in enumerate(rows, start=1):
            print(
                "%-4d %-8s %-7d %-9s %-8s %-8s %-7s"
                % (
                    i,
                    "yes" if r["abstained"] else "no",
                    r["causes"],
                    str(int(r["latency_ms"])) + "ms",
                    str(int(r["gen_ms"])) + "ms",
                    r["completion_tokens"],
                    r["cost_cny"],
                )
            )
        lat = [float(r["latency_ms"] or 0) for r in rows]
        p50, p95 = _pct(lat, 50), _pct(lat, 95)
        worst = max(lat)
        cost = sum(float(r["cost_cny"] or 0) for r in rows)
        print(
            "total %.1fs | 整卡 P50 %.0f ms / P95 %.0f ms / worst %.0f ms | 门槛 P95 4000 ms → %s | 花费 ¥%.4f"
            % (elapsed, p50, p95, worst, "PASS" if p95 <= 4000 else "FAIL", cost)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
