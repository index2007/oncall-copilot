"""D3 验收探针：M4 Query 理解准确率 + M5 双路召回 recall@20。

设计清单验收口径（技术路线与设计清单.md M4/M5）：
  - 50 条真实报错文本，resource_type 准确率 >= 90%
  - LLM 调用占比 < 20%
  - recall@20 >= 0.95

两份样本：
  eval/probe_query_understanding.jsonl   (text / resource_type / note)
  eval/probe_recall.jsonl                (query / marker / sources / note)
  recall 的标注不写死 doc_id，而是"某源里正文包含 marker 的文档集合"，
  换语料版本也能复算，不会因为重抓文档而失配。

用法：
  python scripts/eval_retrieval_probe.py                 # 两项都跑（默认不调 LLM）
  python scripts/eval_retrieval_probe.py --allow-llm     # 规则未命中时真的调 LLM，量真实占比
  python scripts/eval_retrieval_probe.py --skip-recall   # 只跑 M4
  python scripts/eval_retrieval_probe.py --json out.json # 另存完整结果
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings, get_settings  # noqa: E402
from app.core.query_understanding import analyze  # noqa: E402
from app.core.retriever import Retriever  # noqa: E402

EVAL_DIR = PROJECT_ROOT / "eval"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno} 不是合法 JSON: {exc}") from exc
    return rows


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


# ---------------------------------------------------------------- M4


def eval_query_understanding(
    rows: list[dict[str, Any]], settings: Settings, *, allow_llm: bool
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    correct = sub_correct = 0
    llm_calls = 0
    intents: Counter[str] = Counter()

    for row in rows:
        text = row["text"]
        expected = row.get("resource_type")
        t0 = time.perf_counter()
        plan = analyze(text, settings=settings, allow_llm=allow_llm)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if plan.llm_used:
            llm_calls += 1
        intents[plan.intent] += 1

        ok = _norm(plan.resource_type) == _norm(expected)
        correct += int(ok)
        # 资源类型为空时 sub_index 也应为空，否则算错路由
        sub_ok = (expected is None and not plan.sub_index) or (
            expected is not None and bool(plan.sub_index)
        )
        sub_correct += int(sub_ok)

        details.append(
            {
                "text": text,
                "expected": expected,
                "predicted": plan.resource_type,
                "ok": ok,
                "sub_index": plan.sub_index,
                "intent": plan.intent,
                "need_llm": plan.need_llm,
                "llm_used": plan.llm_used,
                "source": plan.source,
                "elapsed_ms": round(elapsed_ms, 2),
            }
        )

    n = len(rows)
    accuracy = correct / n if n else 0.0
    need_llm = sum(1 for d in details if d["need_llm"])
    return {
        "total": n,
        "accuracy": accuracy,
        "correct": correct,
        "sub_index_accuracy": sub_correct / n if n else 0.0,
        "need_llm": need_llm,
        "need_llm_ratio": need_llm / n if n else 0.0,
        "llm_calls": llm_calls,
        "llm_ratio": llm_calls / n if n else 0.0,
        "intents": dict(intents),
        "allowed_llm": allow_llm,
        "failures": [d for d in details if not d["ok"]],
        "details": details,
    }


def print_query_understanding(result: dict[str, Any]) -> None:
    print("== M4 Query 理解 ==")
    print(
        f"样本 {result['total']} 条 | resource_type 准确率 {result['accuracy']:.3f} "
        f"({result['correct']}/{result['total']}, 门槛 >= 0.90)"
    )
    print(
        f"sub_index 路由正确率 {result['sub_index_accuracy']:.3f} | "
        f"need_llm {result['need_llm']} 条 ({result['need_llm_ratio']:.1%}, 门槛 < 20%) | "
        f"实际调用 LLM {result['llm_calls']} 条 ({result['llm_ratio']:.1%})"
    )
    print(f"intent 分布 {result['intents']}")
    if result["failures"]:
        print("  -- 判错的样本 --")
        for f in result["failures"]:
            print(f"  [{f['predicted']!r} <= 期望 {f['expected']!r}] {f['text']}")
    print()


# ---------------------------------------------------------------- M5


def expected_docs(content_dir: Path, marker: str, sources: list[str]) -> set[str]:
    expected: set[str] = set()
    for source in sources:
        for path in sorted((content_dir / source).glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if marker in text:
                expected.add(f"{source}/{path.stem}")
    return expected


def eval_recall(
    rows: list[dict[str, Any]],
    retriever: Retriever,
    settings: Settings,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    content_dir = settings.content_path
    details: list[dict[str, Any]] = []

    for row in rows:
        query = row["query"]
        marker = row["marker"]
        sources = row["sources"]
        expected = expected_docs(content_dir, marker, sources)
        if not expected:
            details.append(
                {
                    "query": query,
                    "marker": marker,
                    "note": row.get("note", ""),
                    "expected_docs": 0,
                    "skipped": "标注为空（marker 在语料里不存在）",
                }
            )
            continue

        variants: dict[str, Any] = {}
        for name, use_filter in (("filtered", True), ("unfiltered", False)):
            t0 = time.perf_counter()
            plan, chunks = retriever.retrieve_text(
                query, limit=limit, use_sub_index=use_filter
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
            doc_ids = [c.doc_id for c in chunks]
            hit_docs = expected & set(doc_ids)
            variants[name] = {
                "top1_doc": doc_ids[0] if doc_ids else None,
                "top1_hit": bool(doc_ids) and doc_ids[0] in expected,
                "hit": bool(hit_docs),
                "hit_docs": sorted(hit_docs),
                "coverage": len(hit_docs) / len(expected),
                "elapsed_ms": round(elapsed_ms, 2),
                "returned": len(chunks),
                "top5": doc_ids[:5],
            }

        details.append(
            {
                "query": query,
                "marker": marker,
                "note": row.get("note", ""),
                "expected_docs": len(expected),
                "plan": {
                    "resource_type": plan.resource_type,
                    "sub_index": plan.sub_index,
                    "error_codes": plan.error_codes,
                    "intent": plan.intent,
                },
                "filtered": variants["filtered"],
                "unfiltered": variants["unfiltered"],
            }
        )

    usable = [d for d in details if "filtered" in d]

    def summarize(key: str) -> dict[str, Any]:
        n = len(usable)
        if not n:
            return {"queries": 0}
        hits = sum(1 for d in usable if d[key]["hit"])
        top1 = sum(1 for d in usable if d[key]["top1_hit"])
        coverage = sum(d[key]["coverage"] for d in usable) / n
        latency = sorted(d[key]["elapsed_ms"] for d in usable)
        p50 = latency[len(latency) // 2]
        p95 = latency[min(len(latency) - 1, int(round(0.95 * (len(latency) - 1))))]
        return {
            "queries": n,
            "recall_at_k": hits / n,
            "hits": hits,
            "top1_hit_rate": top1 / n,
            "mean_coverage": coverage,
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
        }

    return {
        "k": limit,
        "queries": len(details),
        "usable": len(usable),
        "filtered": summarize("filtered"),
        "unfiltered": summarize("unfiltered"),
        "details": details,
    }


def print_recall(result: dict[str, Any]) -> None:
    print(f"== M5 双路召回 recall@{result['k']} ==")
    print(f"{'query':<46}{'期望':>4} {'命中':>4} {'覆盖':>6} {'Top1':>5} {'过滤后 ms':>10}")
    for d in result["details"]:
        if "filtered" not in d:
            print(f"{d['query'][:44]:<46}{'-':>4} {'跳过':>4} {'-':>6} {'-':>5} {'-':>10}")
            continue
        f = d["filtered"]
        print(
            f"{d['query'][:44]:<46}{d['expected_docs']:>4} "
            f"{'是' if f['hit'] else '否':>4} {f['coverage']:>6.2f} "
            f"{'是' if f['top1_hit'] else '否':>5} {f['elapsed_ms']:>10.1f}"
        )
    print()
    for name, label in (("filtered", "子库过滤（默认）"), ("unfiltered", "不过滤（fallback 路径）")):
        s = result[name]
        if not s.get("queries"):
            continue
        print(
            f"{label}: recall@{result['k']} {s['recall_at_k']:.3f} ({s['hits']}/{s['queries']}, "
            f"门槛 >= 0.95) | Top1 命中 {s['top1_hit_rate']:.3f} | "
            f"平均覆盖 {s['mean_coverage']:.3f} | P50 {s['p50_ms']:.0f} ms / P95 {s['p95_ms']:.0f} ms"
        )
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D3 验收探针（M4 准确率 / M5 recall@20）")
    parser.add_argument("--allow-llm", action="store_true", help="规则未命中时真的调用 LLM")
    parser.add_argument("--skip-recall", action="store_true", help="只跑 Query 理解")
    parser.add_argument("--only-recall", action="store_true", help="只跑召回")
    parser.add_argument("--limit", type=int, default=20, help="recall 的 Top-K，默认 20")
    parser.add_argument("--json", type=Path, default=None, help="把完整结果写到该 JSON 文件")
    args = parser.parse_args(argv)

    settings = get_settings()
    out: dict[str, Any] = {}

    if not args.only_recall:
        rows = load_jsonl(EVAL_DIR / "probe_query_understanding.jsonl")
        out["query_understanding"] = eval_query_understanding(
            rows, settings, allow_llm=args.allow_llm
        )
        print_query_understanding(out["query_understanding"])

    if not args.skip_recall:
        retriever = Retriever.from_settings(settings)
        rows = load_jsonl(EVAL_DIR / "probe_recall.jsonl")
        out["recall"] = eval_recall(rows, retriever, settings, limit=args.limit)
        print_recall(out["recall"])

    if args.json:
        payload = json.dumps(out, ensure_ascii=False, indent=2)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload + "\n", encoding="utf-8")
        print(f"结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
