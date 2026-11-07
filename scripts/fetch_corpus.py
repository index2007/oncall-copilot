#!/usr/bin/env python
"""抓取公开运维文档，生成 content/ 语料。

用法：
    python scripts/fetch_corpus.py                 # 抓全部源
    python scripts/fetch_corpus.py --source k8s    # 只抓某个源
    python scripts/fetch_corpus.py --dry-run       # 只发现 URL 不下载
    python scripts/fetch_corpus.py --limit 5       # 每源限 5 篇（调试用）

纪律（清单 M1）：
  * 每请求间隔 >= 1s（--delay 可调，不可低于 0.5）
  * 带明确 User-Agent
  * 遵守 robots.txt
  * 只抓公开文档，正文保留代码块，去掉导航/侧栏/页脚
输出：
  content/<source>/<slug>.md     带 YAML front matter
  content/<source>/index.jsonl   每行一条元数据（可续跑、可查重）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"
UA = "oncall-copilot-corpus-bot/0.1 (+local research; contact: dev@example.com)"
MIN_CHARS = 400          # 低于此值的页面进入"短页判定"，不再直接丢弃
SHORT_FLOOR = 150        # 低于此值视为空壳/错误页，仍然丢弃
NAV_SELECTORS = (
    ".td-sidebar", ".td-sidebar-nav", ".td-sidebar__inner", ".td-navbar", ".td-toc",
    "#TableOfContents", ".td-page-meta", ".td-breadcrumbs", ".td-footer", ".breadcrumb",
    ".feedback--answer", ".feedback--prompt", ".feedback--yes", ".feedback--no",
    ".feedback--response", ".feedback--link", ".pageinfo", ".td-page-meta__lastmod",
    "#search", ".search",
    "[role=navigation]", "#sidebar", "#footer", "#nav", ".cookie", ".banner",
)
# 侧栏残留的指纹；命中说明容器没剔干净，走 fallback 重抽
NAV_MARKERS = ("kubectl 参考", "本页目录", "Table of Contents", "文档目录")


@dataclass
class Source:
    name: str
    lang: str
    limit: int
    sitemap: str | None = None
    seeds: tuple[str, ...] = ()
    include: str = "."
    exclude: str = ""
    priority: tuple[str, ...] = ()
    title: str = ""


SOURCES: dict[str, Source] = {
    "k8s": Source(
        name="k8s",
        title="Kubernetes 官方文档（中文）",
        lang="zh",
        limit=120,
        sitemap="https://kubernetes.io/zh-cn/sitemap.xml",
        include=r"kubernetes\.io/zh-cn/docs/(concepts|tasks|tutorials|setup/production-environment)/",
        exclude=r"/docs/reference/|/docs/contribute/|/blog/|/releases/",
        priority=(
            "debug", "troubleshoot", "logging", "monitor", "resource", "limit",
            "limit-range", "requests", "scheduling", "taint", "affinity", "probe",
            "lifecycle", "crash", "configmap", "secret", "volume", "persistent",
            "network", "service", "dns", "ingress", "autoscal", "pod", "node",
            "container", "image", "storage", "kubelet", "eviction",
        ),
    ),
    "nginx": Source(
        name="nginx",
        title="Nginx 官方文档",
        lang="en",
        limit=40,
        seeds=("https://nginx.org/en/docs/",),
        include=r"nginx\.org/en/docs/.*\.html$",
        exclude=r"x_?changes|/news\.html|download|books|security_advisories",
        priority=(
            "debugging", "error", "upstream", "proxy", "log", "limit", "timeout",
            "ssl", "rewrite", "beginner", "faq", "events", "core_module",
            "http_core", "http_log", "http_proxy", "http_upstream", "http_ssl",
            "http_rewrite", "http_access", "http_fastcgi", "http_gzip", "stream",
            "syntax", "control", "variables", "index",
        ),
    ),
    "redis": Source(
        name="redis",
        title="Redis 官方文档",
        lang="en",
        limit=40,
        sitemap="https://redis.io/docs/latest/sitemap.xml",
        include=r"redis\.io/docs/latest/(operate/oss_and_stack|develop/reference)/",
        exclude=r"/commands/|release-notes|/install/|/embeddable/",
        priority=(
            "management", "persistence", "replication", "sentinel", "cluster", "faq", "memory",
            "latency", "diagnos", "troubleshoot", "config", "security", "monitor",
            "backup", "keyspace", "slow-log", "client", "protocol-spec",
            "optimization", "expire", "eviction", "clients", "best-practices",
        ),
    ),
    "mysql": Source(
        name="mysql",
        title="MySQL 8.0 官方手册",
        lang="en",
        limit=40,
        seeds=("https://dev.mysql.com/doc/refman/8.0/en/",),
        include=r"dev\.mysql\.com/doc/refman/8\.0/en/[\w\-.]+\.html$",
        exclude=r"/doc/index|/doc/refman/8\.0/en/news|mysql-shell|heatwave|ndb|logfile-group|cluster-log|enterprise-audit|windows",
        priority=(
            "innodb-deadlock", "locking", "innodb-lock", "error", "log", "performance",
            "optimizer", "replication", "buffer-pool", "slow", "explain", "backup",
            "connection", "thread", "temporary", "tablespace", "monitor", "show-",
            "status", "variables", "memory", "swap", "disk", "too-many-connections",
            "out-of-memory", "innodb", "recovery",
        ),
    ),
}


# ── 基础设施 ────────────────────────────────────────────────


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_str(s: str) -> str:
    return s.encode("utf-8", "replace").decode("utf-8")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    drop = {"zh-cn", "en", "docs", "latest", "doc", "refman", "8.0", "index.html"}
    parts = [p for p in parts if p not in drop]
    slug = "-".join(parts)
    slug = re.sub(r"\.html?$", "", slug)
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", slug).strip("-")
    return slug or "index"


def clean_title(raw: str) -> str:
    t = re.sub(r"\s+", " ", raw or "").strip()
    if not t:
        return t
    if t.startswith("MySQL ::") or " Reference Manual ::" in t:
        t = t.split("::")[-1].strip()
    for sep in (" | ", " - ", " — "):
        if sep in t and len(t.split(sep)[-1]) > 0:
            head, tail = t.split(sep, 1)
            # 站点名通常在尾部，取更长的部分作为标题
            t = head if len(head) >= len(tail) else tail
    return t.strip()


def _nav_lines(soup: BeautifulSoup) -> set[str]:
    """抽取前先记下侧栏/导航里的条目文本，用于抽取后逐行剔除残留。"""
    lines: set[str] = set()
    for node in soup.select(".td-sidebar-nav, .td-sidebar, nav, aside"):
        for item in node.select("a, li"):
            t = re.sub(r"\s+", " ", item.get_text(" ", strip=True))
            if 4 <= len(t) <= 80:
                lines.add(t)
    return lines


def _fallback_text(soup: BeautifulSoup) -> str:
    """trafilatura 判死时的兜底：只取 main/.td-content 的可见文本。"""
    node = soup.select_one("main") or soup.select_one(".td-content") or soup.body
    if node is None:
        return ""
    for bad in node.select(
        ".feedback--answer, .feedback--prompt, .feedback--yes, .feedback--no, "
        ".feedback--response, .feedback--link, .pageinfo, .td-page-meta, .td-page-meta__lastmod, "
        "a[href^='https://github.com/kubernetes/website/commit']"
    ):
        bad.decompose()
    out: list[str] = []
    prev: str | None = None
    for line in node.get_text(chr(10), strip=True).splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == prev:
            continue
        out.append(line)
        prev = line
    return (chr(10) * 2).join(out)


def _is_index_page(md: str, soup: BeautifulSoup) -> bool:
    """目录/落地页判定：链接密度高 + 有标题 + 体量小，才允许低于 MIN_CHARS 保留。"""
    node = soup.select_one("main") or soup.select_one(".td-content") or soup
    links = len(node.select("a[href]"))
    plain = len(re.sub(r"\s+", "", md))
    density = links / max(1, plain)
    return len(node.select("h1, h2, h3")) >= 1 and len(md) < 1200 and (links >= 2 or density >= 0.02)


class FetchError(RuntimeError):
    pass


class Crawler:
    def __init__(self, delay: float = 1.0, timeout: float = 30.0, retries: int = 3) -> None:
        self.delay = max(0.5, delay)
        self.retries = retries
        self._last: float = 0.0
        self._robots: dict[str, RobotFileParser] = {}
        self.client = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
        )

    # -- 节流 -------------------------------------------------
    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    # -- robots -----------------------------------------------
    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(host)
        if rp is None:
            rp = RobotFileParser()
            rp.set_url(urljoin(host, "/robots.txt"))
            try:
                # 注意：不要用 rp.read()——它用 urllib 默认 UA，被 WAF 403 时会当成 disallow all
                self._throttle()
                resp = self.client.get(urljoin(host, "/robots.txt"))
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])  # 取不到 robots 视为全允许
            except Exception:
                rp.parse([])
            self._robots[host] = rp
        return rp.can_fetch(UA, url)

    # -- HTTP --------------------------------------------------
    def get(self, url: str) -> httpx.Response:
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                self._throttle()
                resp = self.client.get(url)
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (403, 404, 410):
                    raise FetchError(f"HTTP {resp.status_code}")
                last_err = FetchError(f"HTTP {resp.status_code}")
            except FetchError:
                raise
            except Exception as exc:  # 网络层错误可重试
                last_err = exc
            time.sleep(2 ** attempt)
        raise FetchError(f"{type(last_err).__name__}: {last_err}")

    # -- 发现 --------------------------------------------------
    def discover(self, src: Source) -> list[str]:
        urls: list[str] = []
        if src.sitemap:
            resp = self.get(src.sitemap)
            urls += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text)
        for seed in src.seeds:
            resp = self.get(seed)
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = urljoin(seed, a["href"])
                if href.startswith("http") and not href.split("#")[0].endswith((".png", ".jpg", ".gif", ".svg", ".zip", ".gz", ".pdf")):
                    urls.append(href.split("#")[0])

        seen: set[str] = set()
        picked: list[str] = []
        for u in urls:
            u = u.split("#")[0].rstrip("/") + "/" if u.endswith("/") else u.split("#")[0]
            if u in seen:
                continue
            seen.add(u)
            if not re.search(src.include, u):
                continue
            if src.exclude and re.search(src.exclude, u):
                continue
            picked.append(u)

        def score(u: str) -> tuple[int, int, str]:
            low = u.lower()
            for i, kw in enumerate(src.priority):
                if kw in low:
                    return (i, len(u), u)
            return (len(src.priority), len(u), u)

        picked.sort(key=score)
        return picked[: src.limit]

    # -- 抽取 --------------------------------------------------
    def extract(self, url: str, html: str) -> tuple[str, str, str]:
        soup = BeautifulSoup(html, "lxml")
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        title = clean_title(title)

        nav = _nav_lines(soup)
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        for sel in NAV_SELECTORS:
            for node in soup.select(sel):
                node.decompose()

        md = trafilatura.extract(
            str(soup),
            output_format="markdown",
            include_tables=True,
            include_links=False,
            include_comments=False,
            favor_recall=False,
            url=url,
        ) or ""

        # 1) 逐行剔除侧栏残留（容器万一没匹配上，这里兜底）
        if nav and md:
            kept = [ln for ln in md.splitlines() if ln.strip() not in nav]
            if len(kept) != len(md.splitlines()):
                md = chr(10).join(kept)

        # 2) 仍有指纹或正文过短 → 只从 main/.td-content 重抽
        if len(md) < MIN_CHARS or any(m in md for m in NAV_MARKERS):
            alt = _fallback_text(soup)
            if len(alt) > len(md):
                md = alt

        kind = "page"
        if len(md) < MIN_CHARS:
            kind = "index" if _is_index_page(md, soup) else "short"
        md = re.sub(chr(10) + "{3,}", chr(10) * 2, md).strip()
        return title.strip(), md, kind


# ── 主流程 ──────────────────────────────────────────────────


def write_doc(src: Source, url: str, title: str, md: str, force: bool, kind: str = "page") -> dict | None:
    out_dir = CONTENT_DIR / src.name
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(url)
    target = out_dir / f"{slug}.md"
    digest = sha256(md)
    if target.exists() and not force:
        return None

    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    front = (
        "---\n"
        f"source: {src.name}\n"
        f"url: {url}\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"lang: {src.lang}\n"
        f"doc_hash: {digest}\n"
        f"chars: {len(md)}\n"
        f"kind: {kind}\n"
        f"fetched_at: {stamp}\n"
        "---\n\n"
    )
    target.write_text(front + md + "\n", encoding="utf-8")

    meta = {
        "slug": slug,
        "source": src.name,
        "url": url,
        "title": title,
        "lang": src.lang,
        "chars": len(md),
        "doc_hash": digest,
        "kind": kind,
        "fetched_at": stamp,
    }
    upsert_index(out_dir, meta)
    return meta


def _read_frontmatter(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v[:1] == '"':
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        fm[k.strip()] = v
    return fm


def norm_url(url: str) -> str:
    """规范化 URL：去尾斜杠、丢 query/fragment，用于识别同一篇的多种 slug。"""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def upsert_index(out_dir: Path, meta: dict) -> None:
    """按 slug（兼按规范化 URL）upsert：同一篇只保留一行，--force 重抓不追加。

    全量读入 → 覆盖同 slug 行、删掉同 URL 的旧 slug 行 → 排序后写 .tmp 再 os.replace，
    中断不会留下半截 index.jsonl。
    """
    path = out_dir / "index.jsonl"
    rows: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("slug"):
                rows[row["slug"]] = row

    key = meta["slug"]
    target = norm_url(meta["url"])
    for slug in [s for s, r in rows.items() if s != key and norm_url(r.get("url", "")) == target]:
        rows.pop(slug)
    rows[key] = meta
    body = "".join(json.dumps(rows[k], ensure_ascii=False) + chr(10) for k in sorted(rows))
    _atomic_write(path, body)


def rebuild_index(out_dir: Path) -> tuple[int, int]:
    """以 md frontmatter 为准重建 index.jsonl，清掉历史重复/孤儿行。返回 (篇数, 旧行数)。"""
    path = out_dir / "index.jsonl"
    old = 0
    if path.exists():
        old = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    rows: dict[str, dict] = {}
    for md in sorted(out_dir.glob("*.md")):
        fm = _read_frontmatter(md)
        if not fm.get("url"):
            continue
        try:
            chars = int(fm.get("chars") or 0)
        except (TypeError, ValueError):
            chars = 0
        rows[md.stem] = {
            "slug": md.stem,
            "source": fm.get("source", out_dir.name),
            "url": fm.get("url", ""),
            "title": fm.get("title", ""),
            "lang": fm.get("lang", ""),
            "chars": chars,
            "doc_hash": fm.get("doc_hash", ""),
            "fetched_at": fm.get("fetched_at", ""),
        }
    body = "".join(json.dumps(rows[k], ensure_ascii=False) + chr(10) for k in sorted(rows))
    _atomic_write(path, body)
    return len(rows), old


def run(
    src: Source,
    crawler: Crawler,
    *,
    limit: int | None,
    dry_run: bool,
    force: bool,
    only: set[str] | None = None,
) -> dict:
    stats = {
        "source": src.name, "found": 0, "saved": 0, "skipped": 0,
        "failed": 0, "chars": 0, "short_kept": 0,
    }
    try:
        urls = crawler.discover(src)
    except Exception as exc:
        log(f"  ! {src.name}: 发现阶段失败 {type(exc).__name__}: {safe_str(str(exc))}")
        stats["failed"] += 1
        return stats
    if only:
        urls = [u for u in urls if slugify(u) in only or u in only]
        if not urls:
            log(f"  ! {src.name}: --only 未匹配到 URL（按 slug 或完整 URL 比对）")
            return stats
    if limit:
        urls = urls[:limit]
    stats["found"] = len(urls)
    log(f"\n[{src.name}] {src.title} — 待抓 {len(urls)} 篇")
    if dry_run:
        for u in urls:
            log(f"    {u}")
        return stats

    hashes: set[str] = set()
    for i, url in enumerate(urls, 1):
        prefix = f"  [{src.name} {i}/{len(urls)}]"
        if not crawler.allowed(url):
            log(f"{prefix} robots 禁止，跳过 {url}")
            stats["skipped"] += 1
            continue
        try:
            resp = crawler.get(url)
            title, md, kind = crawler.extract(url, resp.text)
            if not md or (len(md) < MIN_CHARS and kind != "index"):
                log(f"{prefix} 正文过短({len(md)} 字)，跳过 {url}")
                stats["skipped"] += 1
                continue
            if kind == "index":
                stats["short_kept"] += 1
                log(f"{prefix} 短目录页保留({len(md)} 字) {url}")
            digest = sha256(md)
            if digest in hashes:
                stats["skipped"] += 1
                continue
            hashes.add(digest)
            meta = write_doc(src, url, title, md, force, kind=kind)
            if meta is None:
                stats["skipped"] += 1
                continue
            stats["saved"] += 1
            stats["chars"] += len(md)
            log(f"{prefix} {len(md):6d} 字  {safe_str(title)[:50]}")
        except Exception as exc:
            stats["failed"] += 1
            log(f"{prefix} 失败 {type(exc).__name__}: {safe_str(str(exc))[:100]}  {url}")
    return stats


def _exit_code(results: list[dict], *, dry_run: bool = False) -> int:
    """退出码：只有"真的抓失败了且一篇没落"才算失败。

    幂等重跑（内容都在库里、全部跳过）是成功的正常态：0 新增 0 失败必须 exit 0，
    否则编排脚本/CI 会把"没事可做"读成"抓取挂了"（2026-11 外部 review 复核项 A6）。
    """
    if dry_run or any(r["saved"] for r in results):
        return 0
    return 1 if any(r["failed"] for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="抓取公开运维文档生成 content/ 语料")
    ap.add_argument("--source", default="all", help="逗号分隔的源名，或 all（默认）")
    ap.add_argument("--limit", type=int, default=None, help="每源最多抓几篇（覆盖配置）")
    ap.add_argument("--delay", type=float, default=1.0, help="请求间隔秒，最低 0.5")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要抓取的 URL")
    ap.add_argument("--force", action="store_true", help="已存在的文件也重新抓取")
    ap.add_argument(
        "--rebuild-index",
        action="store_true",
        help="以 md frontmatter 为准重建 index.jsonl（清重复/孤儿行，不联网）",
    )
    ap.add_argument(
        "--only",
        default=None,
        help="只处理这些篇：逗号分隔的 slug 或完整 URL（配合 --force 修单篇）",
    )
    args = ap.parse_args(argv)

    names = list(SOURCES) if args.source == "all" else [s.strip() for s in args.source.split(",") if s.strip()]
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        log(f"未知源: {', '.join(unknown)}；可用: {', '.join(SOURCES)}")
        return 2

    if args.rebuild_index:
        for name in names:
            md_n, old_n = rebuild_index(CONTENT_DIR / name)
            log(f"[{name}] index 重建: 旧 {old_n} 行 → {md_n} 行")
        return 0

    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    crawler = Crawler(delay=args.delay)
    results = []
    for name in names:
        results.append(
            run(
                SOURCES[name],
                crawler,
                limit=args.limit,
                dry_run=args.dry_run,
                force=args.force,
                only=only,
            )
        )

    log("\n" + "=" * 58)
    log(f"{'源':<8}{'候选':>6}{'新增':>6}{'短页保留':>8}{'跳过':>6}{'失败':>6}{'总字数':>9}")
    for r in results:
        log(f"{r['source']:<8}{r['found']:>6}{r['saved']:>6}{r['short_kept']:>8}"
            f"{r['skipped']:>6}{r['failed']:>6}{r['chars']:>9}")
    total_saved = sum(r["saved"] for r in results)
    total_chars = sum(r["chars"] for r in results)
    log(f"\n合计新增 {total_saved} 篇 / {total_chars} 字 → {CONTENT_DIR}")
    return _exit_code(results, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
