"""M10 评测的契约层：数据集 schema、单条结果、打分与聚合。

纯函数层：不读写文件（load_dataset 除外）、不访问网络、不 import 应用代码，
所以单测不需要语料、不需要密钥。

== 数据集契约（eval/dataset.jsonl：一行一条 JSON，UTF-8 无 BOM）==
{
  "id": "e001",                      # e001-e040 正样本 / n001-n010 负样本，唯一
  "symptom": "...",                  # 原始报错文本，原样作为 query 交给系统（50-500 字）
  "resource_type": "Pod",            # Pod / Node / 网络 / 存储 / 镜像 / 资源；负样本可为 null
  "gold_root_cause": "...",          # 一句话根因，人工标注
  "gold_doc_urls": ["https://..."],  # >=1 条，必须逐字出现在 content/<source>/index.jsonl 的 url 字段
  "gold_keywords": ["OOMKilled"],    # 2-5 个根因关键词，自动判定用（大小写不敏感）
  "negative": false                  # true 表示语料里无依据、系统必须拒答
}
负样本的 gold_doc_urls 写空数组、gold_keywords 写空数组、gold_root_cause 写
"为什么该拒答"的说明，只给人工复核看，不参与打分。

== 单条结果契约（run_eval.py 构造，见 EvalResult）==
causes 是卡片里的根因文本（title + explanation），按输出顺序；
citations 是卡片引用到的 URL；recall_urls 是重排前 20 条候选的 URL；
context_urls 是真正喂给 LLM 的 5 条 URL。三者不能混用，否则 recall@20 会虚高。

== 指标口径（与设计清单 §6.2 一致）==
- Top-1 根因命中：causes[0] 命中 gold（先关键词，未命中再 LLM-as-judge）
- Top-3 根因命中：causes[0..2] 任一命中
- recall@20：重排前 20 条候选 URL 含任一 gold_doc_urls；分母是全部正样本（含拒答），
  因为召回发生在生成之前
- 引用命中率：卡片 citations 里至少一条 URL ∈ gold_doc_urls；分母是作答样本数
  （拒答的卡没有 citations 可核验，计入分母等于重复惩罚同一次拒答）
- 拒答准确率：负样本正确拒答的比例
- 误拒率：正样本被错误拒答的比例（必须和拒答率一起看，否则调高阈值就能刷分）
- 首 token / 整卡 / 成本：TTFT 取 P50，整卡取 P50 与 P95，成本取均值
正样本算 Top-1/Top-3/recall@20/引用命中/误拒率；负样本只算拒答准确率。
指标表里的 recall@20 / 引用命中 各自的分母见上（表里是对齐后的那两列；
条件版 recall20 与全样本版 citation_hit 仍写进 JSON，便于排查）。
error 非空（网络失败、超时、解析失败）的样本不进任何分母，单独计入 errors，
否则一次网络抖动会被记成"答错"。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from app.obs.stats import percentile

# LLM-as-judge 的签名：喂（候选根因文本, 标准答案行）→ 是否同义。返回 None 表示判不了。
Judge = Callable[[str, "EvalRow"], bool | None]

_WS = re.compile(r"\s+")

VARIANTS = ("bm25", "vector", "hybrid", "hybrid_rerank", "full")

VARIANT_LABELS = {
    "bm25": "BM25 only",
    "vector": "向量 only",
    "hybrid": "混合 + RRF",
    "hybrid_rerank": "+ Rerank",
    "full": "+ Query 理解（full）",
}


# ---------------------------------------------------------------- 数据集


@dataclass(frozen=True)
class EvalRow:
    id: str
    symptom: str
    resource_type: str | None
    gold_root_cause: str
    gold_doc_urls: tuple[str, ...] = ()
    gold_keywords: tuple[str, ...] = ()
    negative: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "EvalRow":
        rid = str(raw.get("id") or "").strip()
        if not rid:
            raise ValueError("缺少 id")
        symptom = str(raw.get("symptom") or "").strip()
        if len(symptom) < 5:
            raise ValueError(f"{rid}: symptom 太短或为空")
        negative = bool(raw.get("negative", False))
        urls = tuple(str(u).strip() for u in (raw.get("gold_doc_urls") or []) if str(u).strip())
        if not negative and not urls:
            raise ValueError(f"{rid}: 正样本必须有 gold_doc_urls")
        keywords = tuple(str(k).strip() for k in (raw.get("gold_keywords") or []) if str(k).strip())
        if not negative and not keywords:
            raise ValueError(f"{rid}: 正样本必须有 gold_keywords")
        gold = str(raw.get("gold_root_cause") or "").strip()
        if not gold:
            raise ValueError(f"{rid}: 缺少 gold_root_cause")
        rt = raw.get("resource_type")
        return cls(
            id=rid,
            symptom=symptom,
            resource_type=str(rt).strip() if rt else None,
            gold_root_cause=gold,
            gold_doc_urls=urls,
            gold_keywords=keywords,
            negative=negative,
        )


def load_dataset(path: str | Path) -> list[EvalRow]:
    """读 jsonl，坏行报错并带上行号——评测集写错了必须立刻炸，不能静默跳过。"""
    rows: list[EvalRow] = []
    seen: set[str] = set()
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} JSON 解析失败: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}:{lineno} 不是 JSON 对象")
        try:
            row = EvalRow.from_dict(raw)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno} {exc}") from exc
        if row.id in seen:
            raise ValueError(f"{path}:{lineno} id 重复: {row.id}")
        seen.add(row.id)
        rows.append(row)
    if not rows:
        raise ValueError(f"{path} 里没有任何样本")
    return rows


def split_rows(rows: Sequence[EvalRow]) -> tuple[list[EvalRow], list[EvalRow]]:
    positives = [r for r in rows if not r.negative]
    negatives = [r for r in rows if r.negative]
    return positives, negatives


# ---------------------------------------------------------------- 归一化与单条判定


def _norm(text: str) -> str:
    return _WS.sub(" ", str(text).strip()).lower()


def url_key(url: str) -> str:
    """比较 URL 用：忽略大小写、末位斜杠、首尾空白；不丢 query（锚点不同算不同页）。"""
    key = _norm(url)
    if key.endswith("/"):
        key = key[:-1]
    return key


def keyword_hit(terms: Sequence[str], text: str) -> bool:
    haystack = _norm(text)
    if not haystack:
        return False
    return any(_norm(t) and _norm(t) in haystack for t in terms)


def cause_hit(cause: str, row: EvalRow, judge: Judge | None = None) -> tuple[bool, bool]:
    """单条根因是否命中 gold。返回 (命中, 是否用了 judge)。

    关键词先判是为了省钱：命中就不用调 LLM。关键词漏判（同义不同词）才交给 judge，
    最终报告里 judge_used 会如实写明有多少条依赖了模型判定。
    """
    if keyword_hit(row.gold_keywords, cause):
        return True, False
    if judge is not None:
        verdict = judge(cause, row)
        if verdict is not None:
            return bool(verdict), True
    return False, False


def root_cause_hit(
    causes: Sequence[str], row: EvalRow, judge: Judge | None = None
) -> tuple[bool, bool, bool]:
    """返回 (Top-1 命中, Top-3 命中, 是否用了 judge)。"""
    top1 = False
    top3 = False
    used = False
    for idx, cause in enumerate(causes[:3]):
        hit, judged = cause_hit(cause, row, judge)
        used = used or judged
        if hit:
            top3 = True
            if idx == 0:
                top1 = True
    return top1, top3, used


def citation_hit(citations: Sequence[str], row: EvalRow) -> bool:
    gold = {url_key(u) for u in row.gold_doc_urls}
    return any(url_key(c) in gold for c in citations)


def recall_hit(candidate_urls: Sequence[str], row: EvalRow) -> bool:
    gold = {url_key(u) for u in row.gold_doc_urls}
    return any(url_key(u) in gold for u in candidate_urls)


# ---------------------------------------------------------------- 单条结果


@dataclass(frozen=True)
class EvalResult:
    """run_eval.py 跑完一条样本后的记录。"""

    row_id: str
    variant: str
    abstained: bool
    causes: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    recall_urls: tuple[str, ...] = ()
    context_urls: tuple[str, ...] = ()
    #: 重排后 Top-1 的原始分（0-1）；无重排档或重排降级时为 None，用于阈值标定
    rerank_top1: float | None = None
    ttft_ms: float | None = None
    latency_ms: float = 0.0
    cost_cny: float = 0.0
    judge_used: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------- 聚合


@dataclass
class VariantMetrics:
    variant: str
    positives: int = 0
    negatives: int = 0
    top1: float = 0.0
    top3: float = 0.0
    recall20: float = 0.0
    # 只看召回池、与是否拒答无关的同一指标：分母是全部可用正样本。
    # 两者并列是刻意的——只报条件版会让"模型拒答"伪装成"召回失败"（D7 实测差 30 个点）。
    recall20_pool: float = 0.0
    citation_hit: float = 0.0
    citation_hit_answered: float = 0.0
    false_abstain: float = 0.0
    abstain_accuracy: float = 0.0
    ttft_p50_ms: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    cost_avg_cny: float = 0.0
    errors: int = 0
    judge_used: int = 0

    @property
    def label(self) -> str:
        return VARIANT_LABELS.get(self.variant, self.variant)

    def table_row(self) -> str:
        """按设计清单 §6.3 的 11 列顺序输出一行 Markdown。

        误拒率与拒答准确率必须并排：只报拒答准确率，把阈值调高就能刷到 100%，
        误拒率（正样本被误杀的比例）是它的对手方，两个一起看才有意义。
        """
        cells = [
            self.label,
            _pct(self.top1),
            _pct(self.top3),
            _pct(self.recall20_pool),
            _pct(self.citation_hit_answered),
            _pct(self.false_abstain),
            _pct(self.abstain_accuracy),
            _num(self.ttft_p50_ms),
            _num(self.latency_p50_ms),
            _num(self.latency_p95_ms),
            f"¥{self.cost_avg_cny:.4f}",
        ]
        return "| " + " | ".join(cells) + " |"

    def as_dict(self) -> dict[str, object]:
        return {
            "variant": self.variant,
            "positives": self.positives,
            "negatives": self.negatives,
            "top1": self.top1,
            "top3": self.top3,
            "recall20": self.recall20,
            "recall20_pool": self.recall20_pool,
            "citation_hit": self.citation_hit,
            "citation_hit_answered": self.citation_hit_answered,
            "false_abstain": self.false_abstain,
            "abstain_accuracy": self.abstain_accuracy,
            "ttft_p50_ms": self.ttft_p50_ms,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "cost_avg_cny": self.cost_avg_cny,
            "errors": self.errors,
            "judge_used": self.judge_used,
        }


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _num(value: float | None) -> str:
    return "—" if value is None else str(int(value)) if value >= 100 else f"{value:.1f}"


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def summarize(
    results: Sequence[EvalResult],
    rows: Sequence[EvalRow],
    *,
    variant: str | None = None,
) -> VariantMetrics:
    """把一条变体的全部结果聚成指标表。

    rows 必须覆盖 results 里的每个 row_id；对不上说明数据集被改过而结果没重跑，
    直接报错比出一个错数字更安全。
    """
    by_id = {r.id: r for r in rows}
    metrics = VariantMetrics(variant=variant or (results[0].variant if results else "unknown"))

    pos_ok: list[tuple[EvalRow, EvalResult]] = []
    neg_ok: list[tuple[EvalRow, EvalResult]] = []
    latencies: list[float] = []
    ttfts: list[float] = []
    costs: list[float] = []

    for res in results:
        row = by_id.get(res.row_id)
        if row is None:
            raise ValueError(f"结果里的 row_id 不在数据集中: {res.row_id}")
        if not res.ok:
            metrics.errors += 1
            continue
        (neg_ok if row.negative else pos_ok).append((row, res))
        latencies.append(res.latency_ms)
        costs.append(res.cost_cny)
        if res.ttft_ms is not None:
            ttfts.append(res.ttft_ms)

    metrics.positives = len(pos_ok)
    metrics.negatives = len(neg_ok)

    top1 = top3 = cite = recall = recall_pool = cited_answered = 0
    false_abstain = 0
    for row, res in pos_ok:
        # 召回发生在生成之前：拒答的样本同样有 recall_urls，必须计入召回分母，
        # 否则"模型不敢答"会被算成"召回没找到"，两件事就分不开了。
        if recall_hit(res.recall_urls, row):
            recall_pool += 1
        if res.abstained:
            false_abstain += 1
            continue  # 拒答的样本没有 causes，不能算"答案错"两次
        if recall_hit(res.recall_urls, row):
            recall += 1
        cited_answered += 1
        if citation_hit(res.citations, row):
            cite += 1
        hit1, hit3, judged = root_cause_hit(res.causes, row)
        top1 += int(hit1)
        top3 += int(hit3)
        metrics.judge_used += int(res.judge_used or judged)

    correct_abstain = sum(1 for _row, res in neg_ok if res.abstained)
    for _row, res in neg_ok:
        metrics.judge_used += int(res.judge_used)

    metrics.top1 = _ratio(top1, len(pos_ok))
    metrics.top3 = _ratio(top3, len(pos_ok))
    metrics.recall20_pool = _ratio(recall_pool, len(pos_ok))
    metrics.recall20 = _ratio(recall, len(pos_ok))
    metrics.citation_hit = _ratio(cite, len(pos_ok))
    # 引用只能核验答出来的卡：拒答样本没有 citations，计入分母等于重复惩罚。
    metrics.citation_hit_answered = _ratio(cite, cited_answered)
    metrics.false_abstain = _ratio(false_abstain, len(pos_ok))
    metrics.abstain_accuracy = _ratio(correct_abstain, len(neg_ok))
    metrics.ttft_p50_ms = percentile(ttfts, 50)
    metrics.latency_p50_ms = percentile(latencies, 50)
    metrics.latency_p95_ms = percentile(latencies, 95)
    metrics.cost_avg_cny = round(sum(costs) / len(costs), 6) if costs else 0.0
    return metrics


# ---------------------------------------------------------------- 报告片段

TABLE_HEADER = (
    "| 变体 | Top-1 | Top-3 | recall@20 | 引用命中 | 误拒率 | 拒答准确率 | 首字 P50 (ms) | "
    "整卡 P50 (ms) | 整卡 P95 (ms) | 成本/次 |"
)
TABLE_DIVIDER = "|---|---|---|---|---|---|---|---|---|---|---|"


def render_table(metrics: Sequence[VariantMetrics]) -> str:
    return "\n".join([TABLE_HEADER, TABLE_DIVIDER, *(m.table_row() for m in metrics)])


def failure_details(
    results: Sequence[EvalResult],
    rows: Sequence[EvalRow],
    *,
    limit: int = 10,
) -> str:
    """Top-1 未命中或引用未命中的明细，报告最后一段。"""
    by_id = {r.id: r for r in rows}
    lines: list[str] = []
    for res in results:
        row = by_id.get(res.row_id)
        if row is None or row.negative or not res.ok or res.abstained:
            continue
        hit1, _hit3, _judged = root_cause_hit(res.causes, row)
        cite = citation_hit(res.citations, row)
        if hit1 and cite:
            continue
        lines.append(f"### {row.id}（{row.resource_type or '未分类'}）")
        lines.append(f"- 输入：{row.symptom}")
        lines.append(f"- gold：{row.gold_root_cause}")
        lines.append(f"- 实际 Top-1：{res.causes[0] if res.causes else '（无 causes）'}")
        lines.append(f"- 上下文：{', '.join(res.context_urls[:5]) or '（空）'}")
        if not cite:
            lines.append("- 引用未命中 gold_doc_urls")
        if len(lines) >= limit * 6:
            lines.append(f"\n（已达 {limit} 条上限，其余略）")
            break
    return "\n".join(lines)


def iter_jsonl(path: str | Path) -> Iterator[dict]:
    """保守读取原始 jsonl（调试用，不做 schema 校验）。"""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)
