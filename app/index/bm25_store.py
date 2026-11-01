"""BM25：rank_bm25 + jieba，落盘成 data/bm25.pkl。

为什么必须分词（清单 §3 的坑）：中文没有空格，直接按空白切分的话，
「容器内存超过 limits 被 OOMKilled」整句是一个 token，BM25 永远命中不了
OOMKilled / exit code 137 这类致命关键词——而这些恰好是运维检索里
信息量最高的部分。jieba 分词后 OOMKilled、limits、137 都能独立成词。

索引与查询共用同一个 tokenize()，否则两边切法不一致，召回直接归零。

为什么把 token 也存盘：pkl 里存 [rows, tokens]，加载时只重建 BM25Okapi
（几千块、毫秒级），不用重新分词；2099 块重新分词要几秒，能省就省。
"""

from __future__ import annotations

import logging
import pickle
import re
from pathlib import Path
from typing import Iterable

import jieba
from loguru import logger
from rank_bm25 import BM25Okapi

jieba.setLogLevel(logging.WARNING)

# 只保留字母 / 数字 / 汉字；标点与空白全部丢弃（BM25 不靠它们）
_KEEP_RE = re.compile(r"[0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+")
VERSION = 1
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    """统一的分词函数：小写 + jieba 精确模式 + 只留字母数字汉字。"""
    if not text:
        return []
    out: list[str] = []
    for token in jieba.lcut(text.lower()):
        for piece in _KEEP_RE.findall(token):
            out.append(piece)
    return out


class BM25Store:
    def __init__(self, *, k1: float = K1, b: float = B):
        self.k1 = k1
        self.b = b
        self.rows: list[dict] = []
        self.tokens: list[list[str]] = []
        self._bm25: BM25Okapi | None = None
        self._pos: dict[str, int] = {}

    # -- 构建 --------------------------------------------------
    def build(self, rows: list[dict]) -> None:
        """用给定 chunk 行重建索引（rows 里必须有 chunk_id 与 text）。"""
        self.rows = list(rows)
        self.tokens = [tokenize(r.get("text", "")) for r in self.rows]
        self._pos = {r["chunk_id"]: i for i, r in enumerate(self.rows)}
        self._rebuild()

    def _rebuild(self) -> None:
        corpus = [t for t in self.tokens if t]
        if not corpus:
            self._bm25 = None
            return
        self._bm25 = BM25Okapi(corpus, k1=self.k1, b=self.b)

    # -- 增量 --------------------------------------------------
    def remove_docs(self, doc_ids: Iterable[str]) -> int:
        ids = set(doc_ids)
        if not ids:
            return 0
        keep_rows = [r for r in self.rows if r["doc_id"] not in ids]
        removed = len(self.rows) - len(keep_rows)
        if removed:
            self.build(keep_rows)
        return removed

    def upsert(self, rows: list[dict]) -> int:
        """追加或替换：同 chunk_id 覆盖；同 doc_id 的旧块由调用方先删。"""
        if not rows:
            return 0
        by_id = {r["chunk_id"]: i for i, r in enumerate(self.rows)}
        for row in rows:
            idx = by_id.get(row["chunk_id"])
            if idx is None:
                self.rows.append(row)
                self.tokens.append(tokenize(row.get("text", "")))
                by_id[row["chunk_id"]] = len(self.rows) - 1
            else:
                self.rows[idx] = row
                self.tokens[idx] = tokenize(row.get("text", ""))
        self._pos = {r["chunk_id"]: i for i, r in enumerate(self.rows)}
        self._rebuild()
        return len(rows)

    # -- 检索 --------------------------------------------------
    def search(self, query: str, k: int = 20) -> list[dict]:
        if self._bm25 is None or not self.rows:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        out: list[dict] = []
        for i in ranked:
            score = float(scores[i])
            if score <= 0:
                continue
            row = dict(self.rows[i])
            row["score"] = score
            out.append(row)
        return out

    # -- 持久化 ------------------------------------------------
    def count(self) -> int:
        return len(self.rows)

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(
                {"version": VERSION, "k1": self.k1, "b": self.b, "rows": self.rows, "tokens": self.tokens},
                fh,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path | str, *, missing_ok: bool = True) -> "BM25Store":
        path = Path(path)
        if not path.exists():
            if missing_ok:
                logger.warning("BM25 索引不存在，返回空索引：{}", path)
                return cls()
            raise FileNotFoundError(path)
        with path.open("rb") as fh:
            payload = pickle.load(fh)
        store = cls(k1=payload.get("k1", K1), b=payload.get("b", B))
        store.rows = payload.get("rows", [])
        store.tokens = payload.get("tokens", [])
        store._pos = {r["chunk_id"]: i for i, r in enumerate(store.rows)}
        store._rebuild()
        return store

    def doc_stats(self) -> dict:
        per_doc: dict[str, int] = {}
        for r in self.rows:
            per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
        return {"chunks": len(self.rows), "docs": len(per_doc)}
