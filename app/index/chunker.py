"""M2 切分：把抓下来的 Markdown 文档切成可检索的 Chunk。

对应设计清单 §M2 的规则与接口：

- 以 H2/H3 为边界切分，不做定长滑窗；每块带 heading_path
  （形如「Troubleshooting Pods > Pod 处于 Pending > 资源不足」）。
- 单块 ≤ 800 tokens；超长时先按段落 / 代码行切，再按 100 tokens 重叠续接。
- 代码块与表格是原子块，不在中间断开；只有单个代码块自身超长才按行切，
  且每片都重新包上原语言的围栏，保证单独看仍是合法 Markdown。
- 每块必含 chunk_id / doc_id / text / heading_path / url / anchor / order
  （清单 M2 验收项），另带 token_count / source / title / doc_hash 供索引与排查。
- chunk_id 由 sha256(doc_id | order | text) 前 10 位生成：同一文档重复切分
  得到同一批 id，评测集的 gold 引用与 SSE 的 citations 都能逐字复现。

token 数是**估算**：本机不装 tokenizer（无 torch / tiktoken），用
「CJK 字符 1 token + 其余 4 字符 1 token」近似。它对切分阈值够用，
但不要拿它当计费口径——真实用量以 API 返回的 usage 为准。

零第三方依赖，只 import stdlib，便于单测。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 参数：与设计清单 §M2 对齐，改这里就是改全部下游行为
# ---------------------------------------------------------------------------
MAX_TOKENS = 800  # 单块上限
OVERLAP_TOKENS = 100  # 超长切分时的重叠量
MIN_TOKENS = 30  # 低于此值的碎块尽量并入相邻块
JOIN_SLACK = 10  # 块间用空行拼接，估算会多出几个 token，装填时留这点余量
MAX_HEADING_LEVEL = 3  # H2/H3 才是边界，H4+ 留在正文里

FENCE = chr(96) * 3  # Markdown 代码围栏，避免源码里出现连续反引号
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*" + FENCE)
_SENTENCE_RE = re.compile(r"(?<=[。！？；!?;])\s*")
_ANCHOR_STRIP_RE = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\- ]+")
_CJK_RANGES = (
    (0x3040, 0x30FF),  # 日文假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xAC00, 0xD7AF),  # 韩文
    (0xF900, 0xFAFF),  # CJK 兼容
    (0xFF00, 0xFFEF),  # 全角字符
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Document:
    """一篇待切分的文档。text 不含 front matter。"""

    doc_id: str
    text: str
    title: str = ""
    source: str = ""
    url: str = ""
    lang: str = ""
    doc_hash: str = ""
    path: str = ""

    @property
    def display_title(self) -> str:
        return (self.title or Path(self.path).stem or self.doc_id).strip()


@dataclass(frozen=True)
class Chunk:
    """切分产物。字段顺序 = 索引与调试时的阅读顺序。"""

    chunk_id: str
    doc_id: str
    text: str
    heading_path: str
    url: str
    anchor: str
    order: int
    token_count: int
    source: str = ""
    title: str = ""
    doc_hash: str = ""

    def as_row(self) -> dict:
        """扁平 dict，便于写 LanceDB / SQLite。"""
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "text": self.text,
            "heading_path": self.heading_path,
            "url": self.url,
            "anchor": self.anchor,
            "order": self.order,
            "token_count": self.token_count,
            "source": self.source,
            "title": self.title,
            "doc_hash": self.doc_hash,
        }


@dataclass
class _Block:
    """切分中间态：一个不可再分的 Markdown 片段。"""

    kind: str  # heading | code | text
    text: str
    level: int = 0
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = estimate_tokens(self.text)


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------
def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """粗略 token 数：CJK 1 token/字，其余 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4.0)


# ---------------------------------------------------------------------------
# 读文档
# ---------------------------------------------------------------------------
def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """拆出 YAML front matter（fetch_corpus.py 写的扁平 key: value 格式）。"""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end < 0:
        return {}, raw
    head = raw[3:end].strip("\n")
    body = raw[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        meta[key.strip()] = value
    return meta, body


def load_document(path: Path | str) -> Document:
    """从 content/<source>/<slug>.md 读一篇文档。"""
    path = Path(path)
    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    source = meta.get("source") or path.parent.name
    return Document(
        doc_id=f"{source}/{path.stem}",
        text=body,
        title=meta.get("title", ""),
        source=source,
        url=meta.get("url", ""),
        lang=meta.get("lang", ""),
        doc_hash=meta.get("doc_hash", ""),
        path=str(path),
    )


# ---------------------------------------------------------------------------
# Markdown → 原子块
# ---------------------------------------------------------------------------
def _blocks(text: str) -> list[_Block]:
    """按行为单位收集原子块：标题、代码围栏、段落（含表格 / 列表）。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[_Block] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            joined = "\n".join(para).strip("\n")
            if joined.strip():
                blocks.append(_Block("text", joined))
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        if _FENCE_RE.match(line):
            flush_para()
            end = next((j for j in range(i + 1, n) if _FENCE_RE.match(lines[j])), None)
            if end is None:
                # 落单的围栏（上面那个块没闭合，或抓取把闭合行弄丢）：当普通文本，
                # 绝不能让它一路吞到文末，否则整篇剩下的内容都变成"代码"。
                para.append(line)
                i += 1
                continue
            blocks.append(_Block("code", "\n".join(lines[i : end + 1])))
            i = end + 1
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            level = len(m.group(1))
            blocks.append(_Block("heading", f"{m.group(1)} {m.group(2)}", level=level))
            i += 1
            continue

        if not line.strip():
            flush_para()
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    return blocks


# ---------------------------------------------------------------------------
# 超长块拆分
# ---------------------------------------------------------------------------
def _split_code(block: _Block, max_tokens: int) -> list[_Block]:
    """代码块按行切；每片重新包上围栏，单独看仍是合法 Markdown。"""
    lines = block.text.split("\n")
    opening = lines[0] if lines and _FENCE_RE.match(lines[0]) else FENCE
    body = lines[1:]
    if body and _FENCE_RE.match(body[-1]):
        body = body[:-1]

    pieces: list[list[str]] = []
    cur: list[str] = []
    cur_tokens = 0
    budget = max_tokens - 4  # 留出两行围栏
    for line in body:
        t = estimate_tokens(line) + 1
        if cur and cur_tokens + t > budget:
            pieces.append(cur)
            cur, cur_tokens = [], 0
        cur.append(line)
        cur_tokens += t
    if cur:
        pieces.append(cur)
    if len(pieces) <= 1:
        return [block]
    return [_Block("code", "\n".join([opening, *piece, FENCE])) for piece in pieces]


def _split_text(block: _Block, max_tokens: int) -> list[_Block]:
    """长段落切分，两条路径按内容选：

    1. 有硬换行（≥4 行，抓取时被按列宽折行的英文文档）→ 按行装填；
    2. 否则按句末标点切；句子本身还超长（无标点长串、表格行）→ 按字符硬切。
    """
    if block.text.count("\n") >= 4:
        pieces: list[_Block] = []
        cur: list[str] = []
        cur_tokens = 0
        for line in block.text.split("\n"):
            t = estimate_tokens(line) + 1
            if t > max_tokens:
                if cur:
                    pieces.append(_Block("text", "\n".join(cur)))
                    cur, cur_tokens = [], 0
                pieces.append(_Block("text", line[:max_tokens]))
                continue
            if cur and cur_tokens + t > max_tokens:
                pieces.append(_Block("text", "\n".join(cur)))
                cur, cur_tokens = [], 0
            cur.append(line)
            cur_tokens += t
        if cur:
            pieces.append(_Block("text", "\n".join(cur)))
        if pieces:
            return pieces

    sentences = [s for s in _SENTENCE_RE.split(block.text) if s.strip()]
    if len(sentences) <= 1:
        sentences = [block.text[i : i + max_tokens] for i in range(0, len(block.text), max_tokens)]

    pieces = []
    cur = []
    cur_tokens = 0
    for sent in sentences:
        t = estimate_tokens(sent)
        if t > max_tokens:
            if cur:
                pieces.append(_Block("text", "\n".join(cur)))
                cur, cur_tokens = [], 0
            pieces.extend(_split_text(_Block("text", sent), max_tokens))
            continue
        if cur and cur_tokens + t > max_tokens:
            pieces.append(_Block("text", "\n".join(cur)))
            cur, cur_tokens = [], 0
        cur.append(sent)
        cur_tokens += t
    if cur:
        pieces.append(_Block("text", "\n".join(cur)))
    return pieces or [block]


def _expand(block: _Block, max_tokens: int) -> list[_Block]:
    """把超过 max_tokens 的块拆成若干不超限的子块。"""
    if block.tokens <= max_tokens:
        return [block]
    if block.kind == "code":
        return _split_code(block, max_tokens)
    return _split_text(block, max_tokens)


# ---------------------------------------------------------------------------
# 打包成 Chunk
# ---------------------------------------------------------------------------
def _slugify_anchor(text: str) -> str:
    """标题 → 锚点。保留中文，去掉标点，空格转连字符。"""
    s = _ANCHOR_STRIP_RE.sub("", text.strip().lower())
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


@dataclass
class _Section:
    heading_path: list[str]
    blocks: list[_Block] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(b.tokens for b in self.blocks)


def chunk(doc: Document, *, max_tokens: int = MAX_TOKENS, overlap: int = OVERLAP_TOKENS) -> list[Chunk]:
    """chunk(doc: Document) -> list[Chunk]（清单 M2 接口）。

    算法：顺序扫描原子块 → 遇 H2/H3 开新节 → 节内贪心装填，装满即封块 →
    封块时从尾部按整块回取 ≤ overlap tokens 作为下一块的开头（不在
    句子中间切）。最后一遍合并过碎的块，再统一编号、算 id。

    边界处不重叠：新的 H2/H3 是新的语义单元，回取上一节尾巴只会污染它。
    """
    blocks = _blocks(doc.text)
    title = doc.display_title
    stack: list[str] = [title]

    sections: list[_Section] = []
    cur = _Section(heading_path=list(stack))

    def tail_overlap(blocks_: list[_Block]) -> list[_Block]:
        """从尾部回取整块作为下块的开头，累计不超过 overlap tokens。

        装不下一整个块就放弃重叠——为了重叠塞进一个大块会把下块顶穿上限。
        """
        picked: list[_Block] = []
        total = 0
        for b in reversed(blocks_):
            if b.kind == "heading":
                continue
            if total + b.tokens > overlap:
                break
            picked.append(b)
            total += b.tokens
        return list(reversed(picked))

    def start_new(overlap_blocks: list[_Block] | None = None) -> None:
        nonlocal cur
        sections.append(cur)
        cur = _Section(heading_path=list(stack))
        if overlap_blocks:
            cur.blocks.extend(overlap_blocks)

    for raw_block in blocks:
        for block in _expand(raw_block, max_tokens):
            if block.kind == "heading" and block.level <= MAX_HEADING_LEVEL:
                text = _HEADING_RE.sub(r"\2", block.text).strip()
                if block.level == 1:
                    stack = [text] if text and text != title else [title]
                else:
                    stack = stack[: block.level - 1] + [text]
                    if len(stack) >= 2 and stack[-1] == stack[-2]:
                        stack = stack[:-1]  # 与上级同名的标题（如页标题重复）不重复计入路径
                start_new()
                cur.blocks.append(block)
                continue

            if cur.blocks and cur.tokens + block.tokens + JOIN_SLACK > max_tokens:
                carry = tail_overlap(cur.blocks)
                while carry and sum(b.tokens for b in carry) + block.tokens + JOIN_SLACK > max_tokens:
                    carry.pop(0)  # 丢最早的那块，保住更近的上下文
                start_new(carry)
            cur.blocks.append(block)

    if cur.blocks:
        sections.append(cur)

    # 合并碎块：小于 MIN_TOKENS 的并在前一块里（前一块放得下的话）
    merged: list[_Section] = []
    for sec in sections:
        if merged and sec.tokens < MIN_TOKENS and merged[-1].tokens + sec.tokens + JOIN_SLACK <= max_tokens:
            merged[-1].blocks.extend(sec.blocks)
            continue
        merged.append(sec)

    out: list[Chunk] = []
    for sec in merged:
        text = "\n\n".join(b.text.strip("\n") for b in sec.blocks if b.text.strip()).strip()
        if not text:
            continue
        if text.count(FENCE) % 2:
            text = text + "\n" + FENCE  # 兜底：每块单独渲染时围栏必须闭合
        order = len(out)
        path = " > ".join(p for p in sec.heading_path if p)
        chunk_id = "c_" + hashlib.sha256(f"{doc.doc_id}|{order}|{text}".encode("utf-8")).hexdigest()[:10]
        out.append(
            Chunk(
                chunk_id=chunk_id,
                doc_id=doc.doc_id,
                text=text,
                heading_path=path,
                url=doc.url,
                anchor=_slugify_anchor(sec.heading_path[-1] if sec.heading_path else ""),
                order=order,
                token_count=estimate_tokens(text),
                source=doc.source,
                title=doc.title,
                doc_hash=doc.doc_hash,
            )
        )
    return out


def chunk_file(path: Path | str, **kwargs) -> list[Chunk]:
    """便捷入口：读文件后切分。"""
    return chunk(load_document(path), **kwargs)


# ---------------------------------------------------------------------------
# 自检 CLI
# ---------------------------------------------------------------------------
def _iter_corpus(root: Path) -> list[Path]:
    return sorted(root.glob("*/*.md"))


def _cmd_stats(root: Path) -> int:
    files = _iter_corpus(root)
    if not files:
        print(f"no corpus under {root}")
        return 1
    total_chunks = 0
    oversize = 0
    longest = 0
    per_source: dict[str, list[int]] = {}
    for path in files:
        chunks = chunk_file(path)
        total_chunks += len(chunks)
        for c in chunks:
            longest = max(longest, c.token_count)
            if c.token_count > MAX_TOKENS:
                oversize += 1
        per_source.setdefault(path.parent.name, []).append(len(chunks))
    print(f"docs={len(files)} chunks={total_chunks} avg={total_chunks / len(files):.1f} chunks/doc")
    print(f"max_tokens={longest} oversize={oversize} (limit {MAX_TOKENS})")
    for src, counts in sorted(per_source.items()):
        print(f"  {src:6s} docs={len(counts):4d} chunks={sum(counts):5d} avg={sum(counts) / len(counts):5.1f}")
    return 0


def _cmd_check(root: Path) -> int:
    """校验不变式：每块 ≤ 上限、id 唯一、必填字段齐、文本非空、切分可复现。"""
    problems: list[str] = []
    seen: dict[str, str] = {}
    files = _iter_corpus(root)
    for path in files:
        doc = load_document(path)
        chunks = chunk(doc)
        if not chunks:
            problems.append(f"{path}: 切出 0 块")
            continue
        for c in chunks:
            if c.token_count > MAX_TOKENS:
                problems.append(f"{path}#{c.order}: {c.token_count} tokens > {MAX_TOKENS}")
            if not c.text.strip():
                problems.append(f"{path}#{c.order}: 空正文")
            if not (c.heading_path and c.url and c.anchor and c.doc_id):
                problems.append(f"{path}#{c.order}: 必填字段缺失")
            if c.chunk_id in seen:
                problems.append(f"{path}#{c.order}: chunk_id 重复 {seen[c.chunk_id]}")
            seen[c.chunk_id] = f"{path}#{c.order}"
        if [c.chunk_id for c in chunk(doc)] != [c.chunk_id for c in chunks]:
            problems.append(f"{path}: 切分不稳定（两次 id 不一致）")
    total = len(seen)
    if problems:
        print(f"FAIL {len(problems)} problems / {total} chunks")
        for p in problems[:40]:
            print(" -", p)
        return 1
    print(f"OK docs={len(files)} chunks={total} 唯一 id、≤{MAX_TOKENS} tokens、字段齐、可复现")
    return 0


def _cmd_show(path: Path, limit: int) -> int:
    doc = load_document(path)
    chunks = chunk(doc)
    print(f"{doc.doc_id}  title={doc.display_title!r}  chunks={len(chunks)}")
    for c in chunks[:limit]:
        print(f"\n-- [{c.order}] {c.chunk_id}  {c.token_count} tok  anchor=#{c.anchor}")
        print(f"   path: {c.heading_path}")
        body = c.text if len(c.text) <= 400 else c.text[:400] + " ..."
        print("   " + body.replace("\n", "\n   "))
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m app.index.chunker", description="M2 Markdown 切分")
    ap.add_argument("--root", default="content", help="语料根目录（默认 content）")
    ap.add_argument("--file", help="只看一篇：打印切分结果")
    ap.add_argument("--limit", type=int, default=3, help="--file 时打印几块")
    ap.add_argument("--stats", action="store_true", help="全语料切分统计")
    ap.add_argument("--check", action="store_true", help="全语料不变式校验")
    args = ap.parse_args(argv)

    if args.file:
        return _cmd_show(Path(args.file), args.limit)
    root = Path(args.root)
    if args.check:
        return _cmd_check(root)
    return _cmd_stats(root)


if __name__ == "__main__":
    raise SystemExit(main())