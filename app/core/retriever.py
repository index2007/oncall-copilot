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
        return cls(store=store, bm25=bm25, embedder=embedder, settings=s)

    # -- 单路召回 ----------------------------------------------
    def vector_search(self, q: QueryPlan, k: int | None = None, *, where: str | None = None) -> list[ScoredChunk]:
        k = k or self.settings.top_k_vec
        vector = self.embedder.embed_one(q.search_text)
        rows = self.store.search(vector, k=k, where=where)
        out: list[ScoredChunk] = []
        for i, row in enumerate(rows, start=1):
            score = float(row.get("score") or 0.0)
            out.append(ScoredChunk.from_row(row, score=score, vector_score=score, vector_rank=i))
        return out

    def bm25_search(self, q: QueryPlan, k: int | None = None, *, source: str | None = None) -> list[ScoredChunk]:
        k = k or self.settings.top_k_bm25
        rows = self.bm25.search(q.search_text, k=k, source=source)
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
        fused = self.rrf_fuse(vec, bm, k=self.settings.rrf_k, limit=limit)

        if sub and len(fused) < limit:
            # 过滤只提精度，不能牺牲召回：补跑不限源的召回，再融合一次
            vec_all = self.vector_search(q)
            bm_all = self.bm25_search(q)
            fused = self.rrf_fuse(vec, bm, vec_all, bm_all, k=self.settings.rrf_k, limit=limit)
            fallback = True

        self.last_stats = {
            "query": q.clean,
            "sub_index": sub,
            "filtered": bool(sub),
            "fallback": fallback,
            "vector_candidates": len(vec),
            "bm25_candidates": len(bm),
            "fused": len(fused),
            "vector_ms": round(embed_ms, 1),
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return fused

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
