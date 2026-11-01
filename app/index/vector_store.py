"""VectorStore：先定抽象，再给 LanceDB 实现（清单 ADR-8）。

为什么抽象：本机只能用嵌入式 LanceDB（不装 Docker、不起外部数据库），
但生产上该换 PostgreSQL + pgvector 或 Elasticsearch。检索层只依赖
VectorStore 的 4 个方法，换存储不改一行调用代码——这是简历里"接口已抽象、
可替换"的依据，不是空话。

距离口径：向量在 Embedder 里做了 L2 归一化，因此余弦相似度 = 1 - 余弦距离。
search() 统一返回 0~1 的 score（越大越像），下游 RRF 只关心排名。

LanceDB 的默认 metric 是平方 L2（同样的向量距离为 0），这里显式用 cosine，
避免以后换 metric 时分数解释跟着变。
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Iterable

import lancedb
import pyarrow as pa
from loguru import logger

# 与 chunker.Chunk.as_row() 的字段一一对应（vector 单独加）
META_FIELDS = (
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

_DELETE_CHUNK = 200  # 一条 DELETE 里最多塞多少个 doc_id，避免 SQL 过长


def schema_for(dim: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("doc_id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("heading_path", pa.string()),
            pa.field("url", pa.string()),
            pa.field("anchor", pa.string()),
            pa.field("order", pa.int32()),
            pa.field("token_count", pa.int32()),
            pa.field("source", pa.string()),
            pa.field("title", pa.string()),
            pa.field("doc_hash", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]
    )


def _q(ids: Iterable[str]) -> str:
    quoted = ", ".join("'" + str(i).replace("'", "''") + "'" for i in ids)
    return "(" + quoted + ")"


class VectorStore(abc.ABC):
    """四条方法：写、按文档删、检索、数数。足够支撑增量索引与双路召回。"""

    @abc.abstractmethod
    def upsert(self, rows: list[dict]) -> int:
        """写入若干 chunk 行（每行含 vector）。返回写入条数。"""

    @abc.abstractmethod
    def replace_doc(self, doc_id: str, rows: list[dict]) -> int:
        """用新 chunk 替换某篇文档的全部 chunk（先删后写）。"""

    @abc.abstractmethod
    def delete_docs(self, doc_ids: Iterable[str]) -> int:
        """删除若干篇文档的全部 chunk，返回删除前这些文档的 chunk 数。"""

    @abc.abstractmethod
    def search(self, vector: list[float], k: int = 20, where: str | None = None) -> list[dict]:
        """向量检索，返回带 score（0~1，越大越像）的行。"""

    @abc.abstractmethod
    def count(self) -> int:
        """当前 chunk 总数。"""

    @abc.abstractmethod
    def drop(self) -> None:
        """删表（全量重建用）。"""


class LanceDBVectorStore(VectorStore):
    def __init__(self, uri: Path | str, *, table_name: str = "chunks", dim: int = 1024):
        self.uri = Path(uri)
        self.table_name = table_name
        self.dim = dim
        self._db: Any = None
        self._tbl: Any = None

    # -- 生命周期 ----------------------------------------------
    @property
    def db(self) -> Any:
        if self._db is None:
            self.uri.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.uri))
        return self._db

    def _tables(self) -> list[str]:
        names = self.db.list_tables()
        # 0.41 返回 ListTablesResponse / list；两种都兼容
        return list(names) if isinstance(names, (list, tuple)) else list(getattr(names, "tables", []))

    def table(self, *, create: bool = False) -> Any:
        if self._tbl is not None:
            return self._tbl
        if self.table_name in self._tables():
            self._tbl = self.db.open_table(self.table_name)
            return self._tbl
        if not create:
            return None
        self._tbl = self.db.create_table(self.table_name, schema=schema_for(self.dim), mode="overwrite")
        return self._tbl

    def drop(self) -> None:
        if self.table_name in self._tables():
            self.db.drop_table(self.table_name)
        self._tbl = None

    # -- 写 ----------------------------------------------------
    def upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        clean = []
        for row in rows:
            item = {k: row[k] for k in META_FIELDS}
            item["order"] = int(item["order"])
            item["token_count"] = int(item["token_count"])
            item["vector"] = [float(x) for x in row["vector"]]
            clean.append(item)
        table = self.table(create=True)
        table.add(clean)
        return len(clean)

    def delete_docs(self, doc_ids: Iterable[str]) -> int:
        ids = [d for d in doc_ids if d]
        if not ids:
            return 0
        table = self.table()
        if table is None:
            return 0
        removed = 0
        for start in range(0, len(ids), _DELETE_CHUNK):
            part = ids[start : start + _DELETE_CHUNK]
            hit = self.db_table_count_where(table, "doc_id IN " + _q(part))
            table.delete("doc_id IN " + _q(part))
            removed += hit
        return removed

    @staticmethod
    def db_table_count_where(table: Any, where: str) -> int:
        try:
            return int(table.count_rows(where))
        except Exception:  # noqa: BLE001 - 计数失败不影响删除
            return 0

    def replace_doc(self, doc_id: str, rows: list[dict]) -> int:
        self.delete_docs([doc_id])
        return self.upsert(rows)

    # -- 读 ----------------------------------------------------
    def count(self) -> int:
        table = self.table()
        return 0 if table is None else int(table.count_rows())

    def search(self, vector: list[float], k: int = 20, where: str | None = None) -> list[dict]:
        table = self.table()
        if table is None or table.count_rows() == 0:
            return []
        query = table.search([float(x) for x in vector])
        try:
            query = query.metric("cosine")
        except Exception:  # noqa: BLE001 - 老版本没有 metric()，退回平方 L2
            logger.debug("lancedb 不支持 metric(cosine)，用平方 L2 换算")
        if where:
            query = query.where(where)
        raw = query.limit(k).to_list()
        out: list[dict] = []
        for item in raw:
            row = {key: item[key] for key in META_FIELDS if key in item}
            dist = item.get("_distance")
            row["score"] = float(1.0 - dist) if dist is not None else 0.0
            out.append(row)
        return out


def open_vector_store(uri: Path | str, table_name: str = "chunks", dim: int = 1024) -> VectorStore:
    """工厂：以后要换 pgvector，只改这里。"""
    return LanceDBVectorStore(uri, table_name=table_name, dim=dim)
