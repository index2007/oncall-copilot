"""M3 索引器：扫描语料 -> 切分 -> 批量 embedding -> 写 LanceDB + BM25 -> 更新 SQLite。

增量口径（ADR-7）：

- 每篇文档的 doc_hash = sha256(正文)。与 documents 表比对，只对新增 / 变更的
  文档重新切分与 embedding；没变的文档一块都不碰（不会重复计费）。
- 文件消失的文档在 documents 表里标 is_deleted=1，同时清掉它的向量与 BM25 行；
  文件回来且内容不变时按新增重新索引（软删除可复活，避免留下孤儿块）。
- 全部写入前先把 embedding 算完：API 挂掉时索引还停在旧状态，不会只写一半。

二次运行的验收靠 index_runs 表：added=0 updated=0 deleted=0，且 chunks_written=0
（一条都不重写）、api_calls=0（缓存全命中）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import Settings, get_settings
from app.index.bm25_store import BM25Store
from app.index.chunker import Chunk, Document, chunk, load_document
from app.index.embedder import Embedder
from app.index.vector_store import VectorStore, open_vector_store
from app.obs.db import DB, utc_now

WRITE_BATCH = 512  # 一次 table.add 的行数，太大内存抖，太小写放大


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class IndexReport:
    started_at: str = ""
    finished_at: str = ""
    trigger: str = "cli"
    full_rebuild: bool = False
    docs_scanned: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    chunks_total: int = 0
    chunks_written: int = 0
    api_calls: int = 0
    cache_hits: int = 0
    tokens: int = 0
    cost_cny: float = 0.0
    elapsed_s: float = 0.0
    note: str = ""
    deleted_doc_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["elapsed_s"] = round(self.elapsed_s, 2)
        return data

    def summary(self) -> str:
        parts = [
            "docs_scanned={}".format(self.docs_scanned),
            "added={} updated={} deleted={} skipped={}".format(
                self.added, self.updated, self.deleted, self.skipped
            ),
            "chunks_total={} chunks_written={}".format(self.chunks_total, self.chunks_written),
            "api_calls={} cache_hits={} tokens={}".format(self.api_calls, self.cache_hits, self.tokens),
            "cost=¥{:.4f}".format(self.cost_cny),
            "elapsed={:.1f}s".format(self.elapsed_s),
        ]
        return "  ".join(parts)


@dataclass
class _DocEntry:
    doc: Document
    hash: str


def scan_corpus(content_dir: Path, *, limit: int | None = None) -> list[_DocEntry]:
    files = sorted(content_dir.glob("*/*.md"))
    if limit:
        files = files[:limit]
    entries: list[_DocEntry] = []
    for path in files:
        try:
            doc = load_document(path)
        except Exception as exc:  # noqa: BLE001 - 单篇坏文件不该拖垮整批
            logger.error("skip {}: {}", path, exc)
            continue
        entries.append(_DocEntry(doc=doc, hash=body_hash(doc.text)))
    return entries


def _chunk_rows(doc: Document) -> list[dict]:
    return [c.as_row() for c in chunk(doc)]


def _reset_stores(store: Any, db: DB) -> None:
    """全量重建前清空两处存储。**顺序不能换**（2026-11 外部 review 复核项 A3）。

    documents 表是"哪些文档已经是最新的"的唯一依据。先清它：即便进程在下一条语句前崩溃，
    下次增量也会把所有文档判成 changed、整批重灌，自愈；向量库那时可能还是旧数据，删改
    都会在重灌时补齐。
    反过来先 drop 向量库再清 documents，崩在两者之间就会留下"documents 说全都最新、向量库
    却是空的"——增量把所有文档 skip 掉，索引永久为空，只能再跑一次 --full 才救得回来。
    """
    db.clear()
    store.drop()
    store.table(create=True)


def index_incremental(
    *,
    settings: Settings | None = None,
    content_dir: Path | str | None = None,
    data_dir: Path | str | None = None,
    full: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    trigger: str = "cli",
    progress: bool = True,
) -> IndexReport:
    if full and limit is not None:
        # --full 会清空向量库与 documents 表，--limit 只扫前 N 篇：两个一起用等于把全库
        # 换成一个只含 N 篇的新库，而且"被删掉的文档"算不出来。调试请只用 --limit（增量语义）。
        raise ValueError("full=True 与 limit 不能同时使用：全量重建不接受只扫一部分语料")
    started = time.perf_counter()
    settings = settings or get_settings()
    report = IndexReport(started_at=utc_now(), trigger=trigger, full_rebuild=full)

    content_root = Path(content_dir) if content_dir else settings.content_path
    data_root = Path(data_dir) if data_dir else settings.data_path
    data_root.mkdir(parents=True, exist_ok=True)

    db = DB(data_root / "oncall.db")
    db.init_schema()
    store = open_vector_store(data_root / "lancedb", settings.table_name, settings.embed_dim)

    entries = scan_corpus(content_root, limit=limit)
    report.docs_scanned = len(entries)
    if not entries:
        logger.error("语料为空：{}", content_root.resolve())
        report.finished_at = utc_now()
        report.elapsed_s = time.perf_counter() - started
        return report

    # full 也要先读一次旧状态：删掉的数字（这次全量扫描相对上次少了哪些文档）
    # 只能拿旧库比出来，清库之后再比就永远是 0。
    known_before = db.doc_rows(include_deleted=True)
    known = {} if full else known_before

    on_disk = {e.doc.doc_id for e in entries}
    changed: list[_DocEntry] = []
    for entry in entries:
        prev = known.get(entry.doc.doc_id)
        if prev and prev["doc_hash"] == entry.hash and not prev.get("is_deleted"):
            report.skipped += 1
            continue
        if prev and prev["doc_hash"] == entry.hash and prev.get("is_deleted"):
            report.added += 1  # 复活：行还在、内容没变，重新进索引
        elif prev:
            report.updated += 1
        else:
            report.added += 1
        changed.append(entry)

    deleted_ids: list[str] = []
    if limit is None:
        deleted_ids = sorted(
            d for d, row in known_before.items() if d not in on_disk and not row.get("is_deleted")
        )

    logger.info(
        "scan docs={} changed={} (added={} updated={}) deleted={} skipped={}",
        report.docs_scanned,
        len(changed),
        report.added,
        report.updated,
        len(deleted_ids),
        report.skipped,
    )

    if dry_run:
        report.chunks_total = sum(len(chunk(e.doc)) for e in changed)
        report.deleted = len(deleted_ids)
        report.deleted_doc_ids = deleted_ids
        report.note = "dry-run：未写入任何存储"
        report.finished_at = utc_now()
        report.elapsed_s = time.perf_counter() - started
        return report

    # 1) 切分
    rows_by_doc: dict[str, list[dict]] = {}
    for entry in changed:
        rows_by_doc[entry.doc.doc_id] = _chunk_rows(entry.doc)
    all_rows = [row for doc_id in rows_by_doc for row in rows_by_doc[doc_id]]
    report.chunks_total = len(all_rows)

    # 2) embedding（先算完再动存储：API 失败时索引保持旧状态）
    vectors: list[list[float]] = []
    if all_rows:
        embedder = Embedder(
            api_key=settings.sf_api_key,
            base_url=settings.sf_base_url,
            model=settings.embed_model,
            dim=settings.embed_dim,
            cache_path=data_root / "embed_cache.jsonl",
            batch_size=settings.embed_batch,
            timeout=settings.embed_timeout_s,
            price_cny_per_mtok=settings.embed_price_cny_per_mtok,
        )
        texts = [row["text"] for row in all_rows]
        try:
            vectors = embedder.embed(texts, progress=progress)
        except Exception as exc:  # noqa: BLE001 - 明确报错并保留旧索引
            report.note = "embedding 失败，未写入：{}".format(exc)
            logger.error(report.note)
            report.finished_at = utc_now()
            report.elapsed_s = time.perf_counter() - started
            db.record_run(report)
            raise
        report.api_calls = embedder.api_calls
        report.cache_hits = embedder.cache_hits
        report.tokens = embedder.tokens
        report.cost_cny = embedder.cost_cny

    # 3) 向量库：删除已消失的文档 + 重建变更文档
    # 全量重建要先清空：否则上次索引过、这次已从语料删除的文档，其向量会永久留在表里，
    # 检索照样能召回（documents 表标记了 is_deleted，但向量库不认这个标记）。
    if full:
        _reset_stores(store, db)
    for start in range(0, len(deleted_ids), 100):
        store.delete_docs(deleted_ids[start : start + 100])
    for entry in changed:
        store.delete_docs([entry.doc.doc_id])
    for start in range(0, len(all_rows), WRITE_BATCH):
        batch_rows = all_rows[start : start + WRITE_BATCH]
        batch_vecs = vectors[start : start + WRITE_BATCH]
        payload = [dict(row, vector=vec) for row, vec in zip(batch_rows, batch_vecs)]
        report.chunks_written += store.upsert(payload)

    # 4) BM25：读旧索引，删变更/消失文档的行，追加新块，落盘
    bm25 = BM25Store() if full else BM25Store.load(data_root / "bm25.pkl")
    stale = set(deleted_ids) | set(rows_by_doc)
    if stale:
        bm25.remove_docs(stale)
    bm25.upsert(all_rows)
    bm25.save(data_root / "bm25.pkl")

    # 5) SQLite 状态
    for entry in changed:
        doc = entry.doc
        db.upsert_doc(
            doc_id=doc.doc_id,
            source=doc.source,
            path=doc.path,
            title=doc.display_title,
            url=doc.url,
            doc_hash=entry.hash,
            fetch_hash=doc.doc_hash,
            chunk_count=len(rows_by_doc.get(doc.doc_id, [])),
        )
    report.deleted = db.mark_deleted(deleted_ids)
    report.deleted_doc_ids = deleted_ids

    report.finished_at = utc_now()
    report.elapsed_s = time.perf_counter() - started
    report.note = "full rebuild" if full else ""
    db.record_run(report)
    logger.info("index done: {}", report.summary())
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_index(settings: Settings) -> tuple[VectorStore, BM25Store]:
    store = open_vector_store(settings.lancedb_path, settings.table_name, settings.embed_dim)
    bm25 = BM25Store.load(settings.bm25_path)
    return store, bm25


def _cmd_search(query: str, k: int, settings: Settings) -> int:
    from app.index.embedder import Embedder

    store, bm25 = _load_index(settings)
    embedder = Embedder(
        api_key=settings.sf_api_key,
        base_url=settings.sf_base_url,
        model=settings.embed_model,
        dim=settings.embed_dim,
        cache_path=settings.embed_cache_path,
        batch_size=1,
        timeout=settings.embed_timeout_s,
    )
    vec = embedder.embed_one(query)
    print("query:", query)
    print("vector top{}:".format(k))
    for hit in store.search(vec, k):
        print("  {:.4f}  {}  {}".format(hit["score"], hit["chunk_id"], hit.get("heading_path", "")[:70]))
    print("bm25 top{}:".format(k))
    for hit in bm25.search(query, k):
        print("  {:8.3f}  {}  {}".format(hit["score"], hit["chunk_id"], hit.get("heading_path", "")[:70]))
    return 0


def _cmd_stats(settings: Settings) -> int:
    store, bm25 = _load_index(settings)
    db = DB(settings.sqlite_path)
    db.init_schema()
    stats = db.doc_stats()
    print("sqlite docs={} chunks={} runs={}".format(stats["docs"], stats["chunks"], stats["runs"]))
    print("lancedb chunks={}".format(store.count()))
    print("bm25 chunks={} docs={}".format(bm25.count(), bm25.doc_stats()["docs"]))
    for run in db.last_runs(3):
        print(
            "  run#{run_id} {finished_at} added={added} updated={updated} deleted={deleted} "
            "skipped={skipped} chunks={chunks_written} api={api_calls} {elapsed_s:.1f}s".format(**run)
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="python -m app.index.indexer", description="M3 增量索引")
    ap.add_argument("--content", help="语料目录（默认取 Settings.content_dir）")
    ap.add_argument("--data", help="数据目录（默认取 Settings.data_dir）")
    ap.add_argument("--full", action="store_true", help="全量重建（清空向量库与 documents 表）")
    ap.add_argument("--limit", type=int, help="只索引前 N 篇（调试用，不处理删除）")
    ap.add_argument("--dry-run", action="store_true", help="只报告要做什么，不写任何存储")
    ap.add_argument("--search", help="索引完成后直接查一句，验证两路召回")
    ap.add_argument("--stats", action="store_true", help="打印索引统计，不重新索引")
    ap.add_argument("--k", type=int, default=5, help="--search 的返回条数")
    ap.add_argument("--json", action="store_true", help="把 IndexReport 打成 JSON")
    args = ap.parse_args(argv)

    if args.full and args.limit is not None:
        ap.error("--full 与 --limit 互斥：--full 会清空全库，--limit 只扫前 N 篇")

    settings = get_settings()
    if args.stats:
        return _cmd_stats(settings)

    report = index_incremental(
        settings=settings,
        content_dir=args.content,
        data_dir=args.data,
        full=args.full,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.summary())
    if args.search:
        return _cmd_search(args.search, args.k, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
