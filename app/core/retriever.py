"""检索器（M5）：向量 + BM25 双路召回，RRF 融合。

为什么 RRF 而不是加权求和（清单 ADR-2）：向量余弦是 0~1，BM25 分无上界且随语料
变化，两者量纲不可比；加权求和要手调权重、换语料就失效。RRF 只吃名次：
score = Σ 1/(k + rank)，k=60。名次相同则分数相同，天然免疫量纲问题。

召回层只负责"别漏"（清单 M5 验收：recall@20 ≥0.95），精排在 M6 的重排器。
所以这里两路各取 top_k_vec / top_k_bm25 = 20，融合后仍保留 20 条交给重排。

query 侧的两个细节：
1. 送进两路的不只是原始 query，而是 QueryPlan.search_text（原文 + 错误码 + 强事件词）。
   实测"我的容器被杀了"这种口语 query，加上 OOMKilled / exit code 137 后 Top5 全中。
2. 命中子库（QueryPlan.sub_index）时加元数据过滤：向量路用 LanceDB where，
   BM25 路在打分遍历里过滤。但过滤只提精度、不能牺牲召回——融合后不足 limit 时
   自动补跑一次不限源的召回，把缺的补齐（宁可多给重排器几条）。

降级（D8/F1）：两路召回各自兜底，单路失败只丢那一路的候选，另一路照常融合——
embedding 401、LanceDB 目录坏了、bm25.pkl 丢了都不该让整个问诊失败。两路**都**失败
才算"查不了"（RetrievalFailed），由编排层转成拒答卡；这与"两路都成功但都没命中"
（返回空列表，正常走 no_hits 拒答）是两件事，不能混成一种。

单路降级不往 chunk 上打 degraded 标记：重排成功时 reranker 会把参与重排的块重写成
degraded=False（app/core/reranker.py），标记会被冲掉。改由 last_stats["degraded_legs"]
上报，编排层读完写卡的 degraded 与 abort_reason（retrieval_partial: vector）。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Iterable

from loguru import logger

from app.config import Settings, get_settings
from app.core.query_understanding import analyze
from app.core.schemas import QueryPlan, ScoredChunk
from app.index.bm25_store import BM25Store
from app.index.embedder import Embedder
from app.index.vector_store import VectorStore, open_vector_store


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class RetrievalFailed(RuntimeError):
    """两路召回都不可用（是"查不了"，不是"没命中"）。

    携带两路各自的错误摘要，便于编排层写进降级卡、日志里看出到底哪一路先挂。
    """

    def __init__(self, vector_error: str | None, bm25_error: str | None):
        self.vector_error = vector_error
        self.bm25_error = bm25_error
        parts = []
        if vector_error:
            parts.append("向量路 " + vector_error)
        if bm25_error:
            parts.append("BM25 路 " + bm25_error)
        super().__init__("；".join(parts) or "两路召回均不可用")


class Retriever:
    def __init__(
        self,
        *,
        store: VectorStore,
        bm25: BM25Store,
        embedder: Embedder,
        settings: Settings | None = None,
    ):
        self.store = store
        self.bm25 = bm25
        self.embedder = embedder
        self.settings = settings or get_settings()
        self.last_stats: dict[str, Any] = {}
        # 单路失败原因（每次 retrieve 开头清空）。None = 该路正常。
        self.vector_error: str | None = None
        self.bm25_error: str | None = None
        # 装配时就已知 BM25 索引是空的（pkl 缺失/损坏被 BM25Store.load 吞成空索引）。
        # 只有 from_settings 会置它：单测里手工构造的空 BM25 是"没数据"，不是"坏了"。
        self.bm25_loaded_empty: bool = False

    # -- 装配 --------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Retriever":
        s = settings or get_settings()
        store = open_vector_store(s.lancedb_path, s.table_name, s.embed_dim)
        bm25 = BM25Store.load(s.bm25_path)
        embedder = Embedder(
            api_key=s.sf_api_key,
            base_url=s.sf_base_url,
            model=s.embed_model,
            dim=s.embed_dim,
            cache_path=s.embed_cache_path,
            batch_size=s.embed_batch,
            timeout=s.embed_timeout_s,
            price_cny_per_mtok=s.embed_price_cny_per_mtok,
        )
        retriever = cls(store=store, bm25=bm25, embedder=embedder, settings=s)
        # BM25Store.load(missing_ok=True) 对缺失/空的 pkl 只告警、返回空索引，
        # search() 随后静默返回 []——"没索引"和"没命中"在查询侧长得一模一样。
        # 装配期就把这件事钉成标记，查询时如实记成单路降级，而不是装作没命中。
        retriever.bm25_loaded_empty = len(getattr(bm25, "rows", [])) == 0
        if retriever.bm25_loaded_empty:
            logger.warning("BM25 索引为空（{}），本次进程内 BM25 一路始终降级", s.bm25_path)
        return retriever

    # -- 单路召回 ----------------------------------------------
    def vector_search(self, q: QueryPlan, k: int | None = None, *, where: str | None = None) -> list[ScoredChunk]:
        k = k or self.settings.top_k_vec
        try:
            vector = self.embedder.embed_one(q.search_text)
            rows = self.store.search(vector, k=k, where=where)
        except Exception as exc:  # noqa: BLE001 - 单路失败不该影响另一路
            self.vector_error = type(exc).__name__ + ": " + str(exc)[:200]
            logger.warning("向量召回失败，降级为 BM25 单路：{}", self.vector_error)
            return []
        out: list[ScoredChunk] = []
        for i, row in enumerate(rows, start=1):
            score = float(row.get("score") or 0.0)
            out.append(ScoredChunk.from_row(row, score=score, vector_score=score, vector_rank=i))
        return out

    def bm25_search(self, q: QueryPlan, k: int | None = None, *, source: str | None = None) -> list[ScoredChunk]:
        k = k or self.settings.top_k_bm25
        try:
            rows = self.bm25.search(q.search_text, k=k, source=source)
        except Exception as exc:  # noqa: BLE001 - 单路失败不该影响另一路
            self.bm25_error = type(exc).__name__ + ": " + str(exc)[:200]
            logger.warning("BM25 召回失败，降级为向量单路：{}", self.bm25_error)
            return []
        if not rows and self.bm25_loaded_empty:
            # 空索引不会抛异常（见 BM25Store.load），只能靠装配期的标记认出来
            self.bm25_error = "EmptyBM25Index: bm25.pkl 缺失或为空（BM25Store.load 返回空索引）"
            return []
        out: list[ScoredChunk] = []
        for i, row in enumerate(rows, start=1):
            score = float(row.get("score") or 0.0)
            out.append(ScoredChunk.from_row(row, score=score, bm25_score=score, bm25_rank=i))
        return out

    # -- 融合 --------------------------------------------------
    @staticmethod
    def rrf_fuse(*rankings: Iterable[ScoredChunk], k: int = 60, limit: int = 20) -> list[ScoredChunk]:
        """Reciprocal Rank Fusion：score = Σ 1/(k + rank)，rank 从 1 开始。

        同一路里重复出现的块只记一次（否则重复项会白拿一份票）。
        多路都命中的块得票更多，这正是 RRF 的意图：多路一致 = 更可信。
        """
        votes: dict[str, float] = {}
        best: dict[str, ScoredChunk] = {}
        for ranking in rankings:
            seen: set[str] = set()
            for rank, chunk in enumerate(ranking, start=1):
                cid = chunk.chunk_id
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                votes[cid] = votes.get(cid, 0.0) + 1.0 / (k + rank)
                prev = best.get(cid)
                if prev is None:
                    best[cid] = chunk.model_copy(update={"rrf_score": votes[cid], "score": votes[cid]})
                    continue
                merged = {
                    "vector_score": prev.vector_score if prev.vector_score is not None else chunk.vector_score,
                    "bm25_score": prev.bm25_score if prev.bm25_score is not None else chunk.bm25_score,
                    "vector_rank": prev.vector_rank if prev.vector_rank is not None else chunk.vector_rank,
                    "bm25_rank": prev.bm25_rank if prev.bm25_rank is not None else chunk.bm25_rank,
                    "rrf_score": votes[cid],
                    "score": votes[cid],
                }
                best[cid] = prev.model_copy(update=merged)
        ordered = sorted(best.values(), key=lambda c: (-c.score, c.chunk_id))
        return [c.model_copy(update={"rank": i}) for i, c in enumerate(ordered[: max(0, limit)], start=1)]

    # -- 端到端检索 --------------------------------------------
    def retrieve(
        self,
        q: QueryPlan,
        *,
        limit: int | None = None,
        use_sub_index: bool | None = None,
    ) -> list[ScoredChunk]:
        started = time.perf_counter()
        limit = limit or self.settings.top_k_fuse
        self.vector_error = None
        self.bm25_error = None
        if use_sub_index is None:
            use_sub_index = self.settings.sub_index_filter
        sub = q.sub_index if use_sub_index else None
        where = "source = " + _quote(sub) if sub else None

        embed_ms = 0.0
        fallback = False
        vec: list[ScoredChunk] = []
        bm: list[ScoredChunk] = []

        vec_started = time.perf_counter()
        vec = self.vector_search(q, where=where)
        embed_ms = (time.perf_counter() - vec_started) * 1000
        bm = self.bm25_search(q, source=sub)

        # 两路都失败 = 查不了（索引坏 / 密钥错），不是"没命中"。必须抛，不能返回 []：
        # 返回空会被当成 no_hits 正常拒答，观测侧就看不出"是坏了还是真没资料"。
        if self.vector_error and self.bm25_error:
            self.last_stats = self._stats(
                q, sub, vec=vec, bm=bm, fused=[], embed_ms=embed_ms, started=started
            )
            raise RetrievalFailed(self.vector_error, self.bm25_error)

        fused = self.rrf_fuse(vec, bm, k=self.settings.rrf_k, limit=limit)

        if sub and len(fused) < limit:
            # 过滤只提精度，不能牺牲召回：补跑不限源的召回，再融合一次
            vec_all = self.vector_search(q)
            bm_all = self.bm25_search(q)
            fused = self.rrf_fuse(vec, bm, vec_all, bm_all, k=self.settings.rrf_k, limit=limit)
            fallback = True

        self.last_stats = self._stats(
            q, sub, vec=vec, bm=bm, fused=fused, embed_ms=embed_ms, started=started, fallback=fallback
        )
        return fused

    def _stats(
        self,
        q: QueryPlan,
        sub: str | None,
        *,
        vec: list[ScoredChunk],
        bm: list[ScoredChunk],
        fused: list[ScoredChunk],
        embed_ms: float,
        started: float,
        fallback: bool = False,
    ) -> dict[str, Any]:
        """一份检索现场。失败路径也要有（否则抛异常时调用方读不到哪一路挂了）。"""
        legs = [name for name, err in (("vector", self.vector_error), ("bm25", self.bm25_error)) if err]
        return {
            "query": q.clean,
            "sub_index": sub,
            "filtered": bool(sub),
            "fallback": fallback,
            "vector_candidates": len(vec),
            "bm25_candidates": len(bm),
            "fused": len(fused),
            "vector_ms": round(embed_ms, 1),
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
            # 降级口径：degraded_legs 非空 = 少了一路（另一路照常用）。空 = 两路都正常。
            "vector_error": self.vector_error,
            "bm25_error": self.bm25_error,
            "degraded_legs": legs,
        }

    def retrieve_text(self, text: str, *, limit: int | None = None, **kw: Any) -> tuple[QueryPlan, list[ScoredChunk]]:
        plan = analyze(text)
        return plan, self.retrieve(plan, limit=limit, **kw)


def _print_results(chunks: list[ScoredChunk], show_text: bool) -> None:
    if not chunks:
        print("(无结果)")
        return
    header = "{:>3}  {:<14} {:<8} {:>4} {:>4} {:>7}  {}"
    print(header.format("rnk", "chunk_id", "source", "vec", "bm25", "rrf", "heading_path"))
    for c in chunks:
        v = "-" if c.vector_rank is None else str(c.vector_rank)
        b = "-" if c.bm25_rank is None else str(c.bm25_rank)
        print(header.format(
            str(c.rank), c.chunk_id, c.source or "-", v, b, "{:.4f}".format(c.score), c.heading_path[:60],
        ))
        if show_text:
            print("      " + c.preview(110))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="检索自检：Query 理解 -> 双路召回 -> RRF")
    parser.add_argument("text", nargs="+", help="报错/告警文本")
    parser.add_argument("--k", type=int, default=5, help="融合后显示条数（默认 5）")
    parser.add_argument("--limit", type=int, default=None, help="融合后保留条数（默认取 top_k_fuse=20）")
    parser.add_argument("--no-filter", action="store_true", help="禁用子库过滤")
    parser.add_argument("--show-text", action="store_true", help="打印块正文前 110 字")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    plan = analyze(" ".join(args.text))
    retriever = Retriever.from_settings()
    chunks = retriever.retrieve(plan, limit=args.limit or args.k, use_sub_index=not args.no_filter)

    if args.json:
        print(json.dumps(
            {"plan": plan.model_dump(), "stats": retriever.last_stats, "chunks": [c.model_dump() for c in chunks]},
            ensure_ascii=False, indent=2,
        ))
        return 0

    print("plan:", json.dumps(plan.brief(), ensure_ascii=False))
    print("search_text:", plan.search_text)
    print("stats:", json.dumps(retriever.last_stats, ensure_ascii=False))
    _print_results(chunks, args.show_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
