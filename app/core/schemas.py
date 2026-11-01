"""核心数据模型：QueryPlan（M4 输出）与 ScoredChunk（召回/重排/生成共用）。

为什么用 Pydantic：这两个对象要直接进 SSE 帧、进评测报告、进 /v1/sources 响应，
必须能 model_dump() 成 JSON；同时 ScoredChunk 要能吞下 LanceDB / BM25 回来的
dict（字段多一点少一点都不能炸），所以 extra="ignore"。

score 的口径：
- 向量路 score = 1 - 余弦距离（0~1，越大越像）
- BM25 路 score = rank_bm25 原始分（无上界）
- 融合后 score = RRF 分（1/(60+rank) 求和，跟原始分数量纲无关）
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["diagnose", "howto", "explain", "unknown"]
INTENTS: tuple[str, ...] = ("diagnose", "howto", "explain", "unknown")

# 太泛的事件词：记录进 event_reasons（可解释、可统计），但不拼进检索文本，
# 否则 "Error"/"Pending" 这种词会把 query 向量稀释成"什么都像"。
WEAK_EVENTS: frozenset[str] = frozenset({"error", "pending", "terminating", "unhealthy", "backoff"})

# chunk 的元数据字段（与 chunker.Chunk.as_row() / LanceDB META_FIELDS 对齐）
CHUNK_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "doc_id",
    "text",
    "heading_path",
    "url",
    "anchor",
    "order",
    "token_count",
    "source",
    "title",
    "doc_hash",
)


class QueryPlan(BaseModel):
    """一次查询的结构化检索计划。规则产出，必要时 LLM 补全。"""

    model_config = ConfigDict(extra="ignore")

    raw: str = ""
    clean: str = ""
    intent: Intent = "unknown"
    resource_type: str | None = None
    error_codes: list[str] = Field(default_factory=list)
    event_reasons: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    sub_index: str | None = None

    # 是否"该调"LLM（规则没命中且够长）与是否"真调"了，两个数分开报
    need_llm: bool = False
    llm_used: bool = False
    llm_error: str | None = None
    llm_latency_ms: float | None = None

    source: str = "rule"  # rule / rule+llm / rule+llm_failed
    language: str = "zh"

    @property
    def rule_hit(self) -> bool:
        """规则是否命中到任何可用信号。"""
        return bool(self.error_codes or self.event_reasons or self.resource_type)

    @property
    def search_text(self) -> str:
        """交给双路召回的文本：原始 query + 错误码 + 强事件词。

        清单 M5 细节：向量查询文本 = 原始 query + 抽出的 error_codes/event_reasons。
        实测这样做能把"我的容器被杀了"这种口语 query 拉到 OOMKilled 那几页。
        """
        base = (self.clean or self.raw or "").strip()
        parts = [base] if base else []
        joined = " ".join(parts)
        joined_lc = joined.lower()
        for code in self.error_codes:
            if code and code.lower() not in joined_lc:
                parts.append(code)
                joined_lc += " " + code.lower()
        for reason in self.event_reasons:
            if reason.lower() in WEAK_EVENTS:
                continue
            if reason.lower() not in joined_lc:
                parts.append(reason)
                joined_lc += " " + reason.lower()
        if self.resource_type and self.resource_type.lower() not in joined_lc:
            parts.append(self.resource_type)
        return " ".join(p for p in parts if p)

    def brief(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "resource_type": self.resource_type,
            "sub_index": self.sub_index,
            "error_codes": self.error_codes,
            "event_reasons": self.event_reasons,
            "keywords": self.keywords,
            "need_llm": self.need_llm,
            "llm_used": self.llm_used,
            "source": self.source,
        }


class ScoredChunk(BaseModel):
    """一个候选块 + 它在各路召回里的名次与分数。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    doc_id: str
    text: str = ""
    heading_path: str = ""
    url: str = ""
    anchor: str = ""
    order: int = 0
    token_count: int = 0
    source: str = ""
    title: str = ""
    doc_hash: str = ""

    score: float = 0.0
    vector_score: float | None = None
    bm25_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    vector_rank: int | None = None
    bm25_rank: int | None = None
    rank: int | None = None
    degraded: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any], **overrides: Any) -> "ScoredChunk":
        data: dict[str, Any] = {k: row[k] for k in CHUNK_FIELDS if row.get(k) is not None}
        if "score" not in data:
            data["score"] = float(row.get("score") or 0.0)
        data.update(overrides)
        return cls(**data)

    def preview(self, width: int = 80) -> str:
        text = " ".join((self.text or "").split())
        return text[:width] + ("..." if len(text) > width else "")

    def cite(self) -> str:
        """引用标签：生成侧按 [chunk_id] 标注，校验侧拿它对照上下文。"""
        return "[" + self.chunk_id + "]"


# ---------------------------------------------------------------------------
# 生成侧（M7）与校验侧（M8）的共享契约。先冻结字段，两边并行开发，谁都不许改这里。
# 字段与设计清单 §7 的 DiagnosisCard 响应体一一对应。
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """一条结论的证据：chunk_id 必须能在本次上下文里找到，否则校验侧判无效。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    url: str = ""
    score: float | None = None


class Cause(BaseModel):
    """一条根因假设。confidence 是模型自评，校验侧只做区间夹取，不重算。"""

    model_config = ConfigDict(extra="ignore")

    title: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    explanation: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class Citation(BaseModel):
    """给前端做的引用面板：点开能跳原文。"""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str
    title: str = ""
    heading_path: str = ""
    url: str = ""


class DiagnosisCard(BaseModel):
    """结构化诊断卡（§7 响应体）。abstained=true 时 causes 必须为空。"""

    model_config = ConfigDict(extra="ignore")

    diagnosis_id: str = ""
    abstained: bool = False
    summary: str = ""
    causes: list[Cause] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    latency_ms: int = 0
    ttft_ms: int | None = None
    cost_cny: float = 0.0
    degraded: bool = False

    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    abort_reason: str | None = None

    def cited_ids(self) -> set[str]:
        """卡里出现过的所有 chunk_id（causes.evidence + citations）。"""
        ids: set[str] = set()
        for cause in self.causes:
            for ev in cause.evidence:
                if ev.chunk_id:
                    ids.add(ev.chunk_id)
        for cit in self.citations:
            if cit.chunk_id:
                ids.add(cit.chunk_id)
        return ids


class CitationReport(BaseModel):
    """M8 校验结果。ok=False 时按设计清单走「重生成一次再降级」。"""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    valid: int = 0
    invalid_ids: list[str] = Field(default_factory=list)
    orphan_causes: list[int] = Field(default_factory=list)
    coverage: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.invalid_ids and not self.orphan_causes
