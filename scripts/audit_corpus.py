#!/usr/bin/env python
"""审计 content/<source>/*.md 与 index.jsonl 的一致性：dup / orphan / missing + 跨源重复。

用法: python scripts/audit_corpus.py [--json]
退出码: 0 全部一致；1 存在差异。
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"


def parse_frontmatter(p: Path) -> dict:
    txt = p.read_text(encoding="utf-8")
    if not txt.startswith("---"):
        return {"_error": "no-frontmatter"}
    end = txt.find("\n---", 3)
    if end < 0:
        return {"_error": "unclosed-frontmatter"}
    meta: dict = {}
    for line in txt[3:end].splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v[:1] in ('"', "[", "{"):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        meta[k.strip()] = v
    return meta


def load_rows(path: Path):
    rows, broken = [], []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append((lineno, json.loads(line)))
        except json.JSONDecodeError:
            broken.append(lineno)
    return rows, broken


def audit_source(sdir: Path) -> dict:
    idx = sdir / "index.jsonl"
    mds = sorted(p for p in sdir.glob("*.md"))
    rows, broken = (load_rows(idx) if idx.exists() else ([], []))
    md_slugs = {p.stem: p for p in mds}
    by_slug: dict[str, list[int]] = defaultdict(list)
    for lineno, row in rows:
        if isinstance(row, dict) and row.get("slug"):
            by_slug[row["slug"]].append(lineno)
    dup = sorted((s, ls) for s, ls in by_slug.items() if len(ls) > 1)
    row_slugs = set(by_slug)
    orphan = sorted(row_slugs - set(md_slugs))
    missing = sorted(set(md_slugs) - row_slugs)
    # frontmatter 与索引行不一致（同 slug 的字段漂移）
    mismatch = []
    first_row = {}
    for _, row in rows:
        if isinstance(row, dict) and row.get("slug") and row["slug"] not in first_row:
            first_row[row["slug"]] = row
    for slug, p in md_slugs.items():
        fm = parse_frontmatter(p)
        row = first_row.get(slug)
        if not row:
            continue
        for key in ("url", "doc_hash"):
            if fm.get(key) and row.get(key) and fm[key] != row[key]:
                mismatch.append(f"{slug}:{key}")
    chars = []
    for slug, p in md_slugs.items():
        fm = parse_frontmatter(p)
        try:
            chars.append(int(fm.get("chars") or 0))
        except (TypeError, ValueError):
            chars.append(0)
    return {
        "source": sdir.name,
        "files": len(mds),
        "rows": len(rows),
        "dup": dup,
        "orphan": orphan,
        "missing": missing,
        "broken_lines": broken,
        "field_mismatch": mismatch,
        "chars_total": sum(chars),
        "chars_min": min(chars) if chars else 0,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not CONTENT.exists():
        print(f"content 目录不存在: {CONTENT}")
        return 1
    stats = [audit_source(d) for d in sorted(CONTENT.iterdir()) if d.is_dir()]

    # 跨源 doc_hash 重复组
    hashes: dict[str, list[str]] = defaultdict(list)
    for d in sorted(CONTENT.iterdir()):
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            h = parse_frontmatter(p).get("doc_hash")
            if isinstance(h, str) and h:
                hashes[h].append(f"{d.name}/{p.stem}")
    dup_groups = {h: v for h, v in hashes.items() if len(v) > 1}

    width = max([len(s["source"]) for s in stats] + [6])
    print(f"{'源':<{width}}{'md':>5}{'rows':>6}{'dup':>5}{'orphan':>8}{'missing':>9}{'字数':>10}{'最短':>7}")
    for s in stats:
        print(f"{s['source']:<{width}}{s['files']:>5}{s['rows']:>6}{len(s['dup']):>5}"
              f"{len(s['orphan']):>8}{len(s['missing']):>9}{s['chars_total']:>10}{s['chars_min']:>7}")
    print(f"\n跨源 doc_hash 重复组: {len(dup_groups)}")
    for h, items in dup_groups.items():
        print(f"  {h[:12]}  {' , '.join(items)}")

    bad = 0
    for s in stats:
        if s["dup"]:
            bad += 1
            print(f"\n[{s['source']}] dup 行:")
            for slug, lines in s["dup"]:
                print(f"  {slug}  index 行号 {lines}")
        if s["orphan"]:
            bad += 1
            print(f"\n[{s['source']}] orphan（有索引行、无 md）: {s['orphan']}")
        if s["missing"]:
            bad += 1
            print(f"\n[{s['source']}] missing（有 md、无索引行）: {s['missing']}")
        if s["broken_lines"]:
            bad += 1
            print(f"\n[{s['source']}] 坏行行号: {s['broken_lines']}")
        if s["field_mismatch"]:
            bad += 1
            print(f"\n[{s['source']}] frontmatter/index 字段不一致: {s['field_mismatch']}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
