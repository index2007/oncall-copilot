"""GET /v1/sources/{chunk_id}：把诊断卡里的一个 [chunk_id] 还原成原文 + url。

为什么原文不截断：引用面板的用途是"点开原文，自己判断 LLM 有没有瞎说"，截断会把
结论依据的那一句切掉。单块上限 800 token，一次请求吐全文不会撑爆响应。

为什么直接查 LanceDB 表而不是走 VectorStore 抽象：抽象层只有
upsert / replace_doc / delete_docs / search / count / drop 六个方法，
"按 chunk_id 取一条"是 KV 查询、不是检索，硬塞进 search() 会让两边语义都变形
（换 pgvector 时这里就是一句 SELECT ... WHERE chunk_id = $1，已在下面标出）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from app.api.deps import get_app_settings
from app.api.schemas import SourceResponse
from app.config import Settings
from app.index.vector_store import VectorStore, open_vector_store

router = APIRouter(tags=["sources"])

# 与 chunker.Chunk.as_row() / vector_store.META_FIELDS 对齐（vector 单列，不回给客户端）
SOURCE_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "doc_id",
    "source",
    "title",
    "heading_path",
    "url",
    "anchor",
    "order",
    "token_count",
    "text",
)


def get_vector_store(settings: Settings = Depends(get_app_settings)) -> VectorStore | None:
    """只读打开向量库。

    目录不存在时返回 None 而不是先建目录：全新仓库还没跑过索引时，一个 GET 请求
    不该在磁盘上留下一个空的 lancedb/。
    """
    if not settings.lancedb_path.is_dir():
        return None
    return open_vector_store(settings.lancedb_path, settings.table_name, settings.embed_dim)


def fetch_chunk_row(store: VectorStore, chunk_id: str) -> dict[str, Any] | None:
    """按 chunk_id 取一行（表不存在 / 没有这条 → None）。"""
    # 换 pgvector 时，这一行换成 cur.execute("SELECT ... WHERE chunk_id = %s", (chunk_id,))
    table = store.table()  # type: ignore[attr-defined]  # 只有 LanceDB 实现有这个方法
    if table is None:
        return None
    # chunk_id 是本项目自己生成的 c_<hex10>，这里仍然转义单引号：它是 URL path 进来的
    expr = "chunk_id = '" + str(chunk_id).replace("'", "''") + "'"
    rows = table.search().where(expr).limit(1).to_list()
    if not rows:
        return None
    row = dict(rows[0])
    row.pop("vector", None)  # 4096 字节的向量对"看原文"没用，不回给客户端
    return row


@router.get(
    "/v1/sources/{chunk_id}",
    response_model=SourceResponse,
    summary="按 chunk_id 取原文",
    responses={404: {"description": "chunk not found"}},
)
def get_source(
    chunk_id: str,
    store: VectorStore | None = Depends(get_vector_store),
) -> SourceResponse:
    if store is None:
        raise HTTPException(status_code=404, detail="chunk not found")
    row = fetch_chunk_row(store, chunk_id)
    if row is None:
        logger.info("sources 未命中：chunk_id={}", chunk_id)
        raise HTTPException(status_code=404, detail="chunk not found")

    payload: dict[str, Any] = {k: row[k] for k in SOURCE_FIELDS if row.get(k) is not None}
    payload["chunk_id"] = str(row.get("chunk_id") or chunk_id)
    payload["order"] = int(row.get("order") or 0)
    payload["token_count"] = int(row.get("token_count") or 0)
    payload["text"] = str(row.get("text") or "")
    return SourceResponse(**payload)
