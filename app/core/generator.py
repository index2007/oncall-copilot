"""M7 生成（app/core/generator.py）：候选块 -> 结构化诊断卡（DiagnosisCard）。

三条主路径（清单 ADR-3 / ADR-4 / M7 验收）：

1. 正常：DeepSeek /chat/completions + response_format=json_object -> json.loads
   -> Pydantic 校验 DiagnosisCard -> 引用/命令后置过滤。
2. JSON 或 schema 坏：重试 1 次（settings.llm_max_retry），重试时把上次原文
   连同纠错指令一起发回去，而不是原样重发。
3. 再次失败：degraded=True，summary 用原文片段拼接（不推理、不编原因、
   不抛异常）。这是 ADR-4 的最后一档，API 层拿到卡就能直接用。

几条刻意的取舍：
- 上下文有预算（PROMPT_CHAR_BUDGET=3600 字符）且每块都必须带 id：把 20 条召回
  全塞进去的结果是模型在一堆无关段落里挑，既贵又不准；宁可按水位分配、
  给每块留可读的正文，也不让某一块独占预算。
- 引用过滤是"事后校验"而不是"信任模型"：citations / evidence 里凡是候选块
  里不存在的 chunk_id 一律丢弃（ADR-4 的不编造）。一条结论的证据被全丢光，
  这条结论就不再输出；若所有结论都被丢光，整卡转 abstained，摘要改成原文
  片段（防止"模型编了个看起来很有把握的原因"）。
- verify_commands 用只读白名单 + 写操作黑名单双重判定（见 is_read_only_command）：
  白名单保证"只跑我认识的命令"，黑名单堵住 kubectl get ... && kubectl delete ...
  这种被拼在管道后面的写操作。
- 网络/HTTP 失败不重试：重试一次 JSON 错误是修格式，重试一次 60s 超时是白烧钱。

流式（D5-A）：
open_stream() 复用同一套上下文预算与消息拼装，只把调用方式换成 stream=true，
逐段 yield 正文增量；finalize_stream() 在流结束后复用 _parse_card / _finalize
（规范化、引用过滤、限长、构造卡），所以流式与非流式的解析口径只有一份。
generate() 的行为与签名不变，非流式调用方一行都不用改。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator, Sequence, Union

from app.core.http_client import get_client

import httpx
from loguru import logger
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.core import prompts
from app.core.schemas import Cause, Citation, DiagnosisCard, Evidence, ScoredChunk

# 上下文预算：system prompt 约 1.7k 字符，加正文控制在 3600 字符内，
# 折合约 1.8k prompt tokens。实测 2357 prompt + 972 completion tokens 要 6.5s
# （≈130 token/s，清单 §6.5），所以预算与输出上限一起收紧，目标整卡 ≤4s。
# 为什么从 2600 提到 3600：D7 全量 50 条评测里，重排 Top5 只有 34/40 正样本的
# gold 文档进入上下文（85.0%）；纯重排重放证明「Top8 + 每文档最多 2 块」能到
# 39/40（97.5%，唯一漏的 e018 是 gold 文档不在召回池内，属召回问题）。
# 单块变短多少（按 1500 字符的块实测 water_fill）：原来 5 块 / 2600 预算每块
# 495 字符，现在 8 块 / 3600 每块 433 字符，-12%——与 520→450 的粗算同量级，
# 不是"挤成半句话"（MIN_TEXT_CHARS=40 的 10 倍），块本身短于 433 字符时根本不截断。
PROMPT_CHAR_BUDGET = 3600
# 一次最多喂 8 块（重排取 Top8）：Top5 时同一篇文档常占掉 2-3 个名额，缺失的
# gold 文档排在重排第 6/7/9/10 名，够不着——所以先把候选扩到 8，再用下面的
# 每文档 ≤2 块配额把名额分给更多篇文档。8 块 / 3600 字符 ≈ 450 字符/块
# （实测 433），原 5 块 / 2600 ≈ 520（实测 495）：同量级、单块短约 12%，
# 但 prompt token 数只多约 0.5k，换的是 gold 文档进上下文的比例 85.0% -> 97.5%。
MAX_CONTEXT_CHUNKS = 8
# 每块正文的下限；低于这个数就少给几块，而不是给一堆"半句话"
MIN_TEXT_CHARS = 40
# 上下文再挤也要留出的最小预算（query 很长时防止把正文挤没）
MIN_CONTEXT_BUDGET = 800
MAX_VERIFY_COMMANDS = 3

# 输出预算的硬上限：prompt 里已经写了，这里再裁一遍。
# 为什么两处都要有：prompt 是请求不是保证，实测模型在"最多 2 条"时照样吐 4 条，
# 而每条多出来的 explanation 都是几十毫秒，整卡 ≤4s 的验收线扛不住。
# 3 条而不是 2 条：Top-3 才有东西可判（D7 实测只给 1 条时 Top-3 恒等于 Top-1）。
MAX_CAUSES = 3
MAX_EVIDENCE_PER_CAUSE = 2
# citations 的硬上限：与 _fallback_citations 的默认值同源，免得两处各写一个 3
MAX_CITATIONS = 3
MAX_ACTIONS = 3
SUMMARY_MAX_CHARS = 80
EXPLANATION_MAX_CHARS = 50
ACTION_MAX_CHARS = 45


# ------------------------------------------------------------------ LLM 调用


@dataclass(frozen=True)
class LLMResponse:
    """一次 LLM 调用的结果：正文 + 计费口径 + 结束原因。

    finish_reason 要带出来：为 "length" 说明 JSON 是被 max_tokens 掐断的，
    这种输出重试一次（并明确要求更短）比硬解析后降级更划算。
    """

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""


# 注入点：messages -> LLMResponse。单测 monkeypatch default_llm_callable 即可离线跑。
LLMCallable = Callable[..., LLMResponse]


def _apply_thinking(payload: dict[str, Any], s: Any) -> None:
    """关掉推理模型的"先想一遍"。

    deepseek-flash / deepseek-v4-pro 在 /v1/models 里标了 effort(low/high/max)、默认 high，
    实测不关的话：SSE 先来几十上百个 reasoning_content 块，正文 delta 要等 6 s 才出现
    （非流式对应 completion_tokens 从 ~300 涨到 ~900，整卡 8 s）。诊断卡是"填模板"类任务，
    推理带的边际收益不值这 5 s，所以默认 disabled；需要时 LLM_THINKING=auto 打开。
    """

    if str(getattr(s, "llm_thinking", "disabled")).lower() == "disabled":
        payload["thinking"] = {"type": "disabled"}


def default_llm_callable(settings: Settings | None = None) -> LLMCallable:
    """默认实现：DeepSeek /chat/completions（OpenAI 兼容），带 json_object 模式。"""

    def _call(messages: list[dict[str, str]], *, timeout: float | None = None) -> LLMResponse:
        s = settings or get_settings()
        if not s.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置，无法生成诊断卡")
        url = s.deepseek_base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": s.llm_model,
            "messages": messages,
            "temperature": s.llm_temperature,
            "max_tokens": s.llm_max_tokens,
            "response_format": {"type": "json_object"},
        }
        _apply_thinking(payload, s)
        headers = {"Authorization": "Bearer " + s.deepseek_api_key, "Content-Type": "application/json"}
        resp = get_client().post(url, json=payload, headers=headers, timeout=timeout or s.llm_timeout_s)
        resp.raise_for_status()
        body = resp.json()
        choice = body["choices"][0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = body.get("usage") or {}
        return LLMResponse(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=str(body.get("model") or s.llm_model),
            finish_reason=str(choice.get("finish_reason") or ""),
        )

    return _call


# ------------------------------------------------------------------- 流式调用


@dataclass(frozen=True)
class StreamPiece:
    """一段流式增量：正文片段 + 可选的 usage / 结束原因。

    OpenAI 兼容的流里，usage 只在最后一个 data 块出现（需要带
    stream_options.include_usage），中间的块只有 choices[0].delta.content。
    所以计费口径随最后一个真实片段回来，而不是每块都带。
    """

    text: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""


class StreamFailed(RuntimeError):
    """流式调用失败（网络 / HTTP / 数据块解析）。

    为什么不在这里直接降级成一张卡：流可能已经发出去了，调用方需要知道
    "是哪一段坏的"，而不是一张伪装成功的卡。能降级的地方只有两处——
    finalize_stream（JSON 坏）与上层闸门 / 引用校验（ADR-4 的降级链）。
    """


# 注入点：messages -> 迭代器，逐段产出 StreamPiece 或 str。单测直接传假实现。
StreamCallable = Callable[..., Iterator[Union[StreamPiece, str]]]


def default_stream_callable(settings: Settings | None = None) -> StreamCallable:
    """默认实现：DeepSeek /chat/completions + stream=true，逐行解析 SSE。

    解析口径：`data: {json}` 每行一块，delta.content 是正文增量，`data: [DONE]` 结束。
    任何一步不合预期都抛 StreamFailed——静默跳过坏块会被上层当成"模型就这么答的"，
    那是最难查的一类故障。
    """

    def _call(
        messages: list[dict[str, str]], *, timeout: float | None = None
    ) -> Iterator[Union[StreamPiece, str]]:
        s = settings or get_settings()
        if not s.deepseek_api_key:
            raise StreamFailed("DEEPSEEK_API_KEY 未配置，无法流式生成")
        url = s.deepseek_base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": s.llm_model,
            "messages": messages,
            "temperature": s.llm_temperature,
            "max_tokens": s.llm_max_tokens,
            "response_format": {"type": "json_object"},
            "stream": True,
            # 不带这个参数，流式响应里永远没有 usage，cost_cny 就只能记 0
            "stream_options": {"include_usage": True},
        }
        _apply_thinking(payload, s)
        headers = {"Authorization": "Bearer " + s.deepseek_api_key, "Content-Type": "application/json"}
        client = get_client()
        resp: Any = None
        try:
            request = client.build_request("POST", url, json=payload, headers=headers, timeout=timeout or s.llm_timeout_s)
            resp = client.send(request, stream=True)
            resp.raise_for_status()
            for raw in resp.iter_lines():
                line = (raw or "").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    return
                try:
                    body = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise StreamFailed("流式数据块不是合法 JSON：" + str(exc)) from exc
                if body.get("error"):
                    raise StreamFailed("流式接口返回错误：" + str(body["error"])[:300])
                usage = body.get("usage") or {}
                choices = body.get("choices") or []
                text = ""
                finish = ""
                if choices:
                    choice = choices[0] or {}
                    text = (choice.get("delta") or {}).get("content") or ""
                    finish = str(choice.get("finish_reason") or "")
                if text or usage or finish:
                    yield StreamPiece(
                        text=text,
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        model=str(body.get("model") or s.llm_model),
                        finish_reason=finish,
                    )
        except StreamFailed:
            raise
        except Exception as exc:  # 网络 / 超时 / 4xx / 5xx 统一包成 StreamFailed
            raise StreamFailed("流式调用失败：" + type(exc).__name__ + ": " + str(exc)) from exc
        finally:
            # 共享连接池里的响应必须显式关闭，否则这条连接不会回到池子里
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001 - 关流失败不该覆盖真实错误
                    pass

    return _call


class StreamSession(Iterator[str]):
    """open_stream() 的返回值：既是 Iterator[str]，又带回 usage 与实际用到的块。

    为什么要一个类而不是纯生成器：finalize_stream() 需要 prompt/completion tokens
    与"这次真正喂进去的块"，普通生成器耗尽后交不出这两个数字；再让调用方按
    字符数估一遍 token，等于把计费口径写第二遍。
    """

    def __init__(
        self,
        source: Iterator[Union[StreamPiece, str]],
        *,
        used: Sequence[ScoredChunk],
        settings: Settings,
    ):
        self._source = source
        self.used: list[ScoredChunk] = list(used)
        self.settings = settings
        self.parts: list[str] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model = settings.llm_model
        self.finish_reason = ""

    @property
    def text(self) -> str:
        """已经收到的全部正文（用于流结束后一次解析）。"""
        return "".join(self.parts)

    def __iter__(self) -> "StreamSession":
        return self

    def __next__(self) -> str:
        while True:
            # 耗尽时 StopIteration 自然向上抛（不吞掉，否则调用方会死循环）
            item = next(self._source)
            if isinstance(item, StreamPiece):
                text = item.text or ""
                if item.prompt_tokens:
                    self.prompt_tokens = item.prompt_tokens
                if item.completion_tokens:
                    self.completion_tokens = item.completion_tokens
                if item.model:
                    self.model = item.model
                if item.finish_reason:
                    self.finish_reason = item.finish_reason
            else:
                text = item or ""
            if text:
                self.parts.append(text)
                return text


# ------------------------------------------------------------ 上下文（预算）


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def _truncate(text: str, limit: int) -> str:
    """按字符截断，截断时用省略号占位（保证结果长度 <= limit）。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _select_chunks(
    chunks: Sequence[ScoredChunk],
    max_chunks: int,
    max_per_doc: int = 2,
) -> list[ScoredChunk]:
    """按传入顺序（重排后的名次）取块：去空 id、去重复 chunk_id，同一 url 最多 max_per_doc 块。

    为什么要每文档配额：Top5 时同一篇长文档经常一口气占掉 2-3 个名额，把排在
    第 6-10 名的 gold 文档挤出去。D7 全量 50 条实测，重排 Top5 只有 34/40 正样本
    的 gold 文档进入上下文（85.0%）；纯重排重放证明 Top8 + 每文档 ≤2 块能到
    39/40（97.5%，唯一漏的 e018 是 gold 文档不在召回池内，属召回问题）。

    为什么要回填：配额只在"还有别的文档可补"时才划算。全池只有 1-2 篇文档的
    查询（评测里的 e036 全池只有 2 篇文档）如果死守配额，等于白丢名额——所以
    第一轮按配额选不满 max_chunks 时，把因配额跳过的块按原顺序补回来。

    url 为空视为"没有文档归属"，不参与配额：把一堆空 url 的块当成同一篇，
    会让只有空 url 的池子永远只剩 max_per_doc 块。
    """
    selected: list[ScoredChunk] = []
    overflow: list[ScoredChunk] = []  # 因配额跳过、可能回填的块（此时已去过重）
    seen: set[str] = set()
    per_doc: dict[str, int] = {}
    quota = max_per_doc if max_per_doc and max_per_doc > 0 else None
    for chunk in chunks or []:
        cid = (getattr(chunk, "chunk_id", "") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        url = (getattr(chunk, "url", "") or "").strip()
        if url and quota is not None and per_doc.get(url, 0) >= quota:
            overflow.append(chunk)
            continue
        if url and quota is not None:
            per_doc[url] = per_doc.get(url, 0) + 1
        selected.append(chunk)
        if len(selected) >= max_chunks:
            return selected
    for chunk in overflow:
        if len(selected) >= max_chunks:
            break
        selected.append(chunk)
    return selected


def _water_fill(lengths: list[int], capacity: int, floor: int) -> list[int] | None:
    """按水位给每块正文分配预算：短块吃满自己的长度，剩下的均分给长块。

    比 "capacity // n 一刀切" 好：短块不浪费额度，长块也不会被过度挤压。
    返回 None 表示连 floor 都保证不了（调用方应减少块数）。
    """
    if not lengths:
        return []
    if capacity < floor * len(lengths):
        return None
    budgets = [0] * len(lengths)
    active = list(range(len(lengths)))
    remaining = capacity
    while active:
        share = remaining // len(active)
        if share < floor:
            return None
        filled = [i for i in active if lengths[i] <= share]
        if not filled:
            for i in active:
                budgets[i] = share
            return budgets
        for i in filled:
            budgets[i] = lengths[i]
            remaining -= lengths[i]
        active = [i for i in active if i not in filled]
    return budgets


def _layout(selected: list[ScoredChunk], budget: int) -> list[str] | None:
    """把选中的块排成带 [chunk_id] 的文本块；放不下返回 None。"""
    n = len(selected)
    tags = ["[" + c.chunk_id.strip() + "]" for c in selected]
    texts = [_squash(c.text) for c in selected]
    lengths = [len(t) for t in texts]

    # heading 从长到短退让：先保正文，再保 heading，最后只留 id
    for heading_cap in (prompts.BLOCK_HEADING_MAX, 40, 0):
        headings = [_truncate(_squash(c.heading_path), heading_cap) for c in selected]
        fixed = 2 * (n - 1) + sum(
            len(tag) + len(head) + (1 if head else 0) + 1
            for tag, head in zip(tags, headings)
        )
        budgets = _water_fill(lengths, budget - fixed, MIN_TEXT_CHARS)
        if budgets is None:
            continue
        blocks: list[str] = []
        for tag, head, text, size in zip(tags, headings, texts, budgets):
            body = _truncate(text, size)
            blocks.append(tag + (" " + head if head else "") + "\n" + body)
        if sum(len(b) for b in blocks) + 2 * (n - 1) <= budget:
            return blocks
    return None


def build_context(
    chunks: Sequence[ScoredChunk],
    *,
    budget: int = PROMPT_CHAR_BUDGET,
    max_chunks: int = MAX_CONTEXT_CHUNKS,
    max_per_doc: int = 2,
) -> tuple[str, list[ScoredChunk]]:
    """候选块 -> (上下文文本, 实际用到的块)。总长 <= budget，每块都带 id。

    返回实际用到的块而不是全部：生成侧之后只允许引用这些块，调用方拿到的
    used 也就是"允许的引用集合"。预算连一块都放不下时返回空串——此时调用方
    应当走拒答，而不是把没有 id 的正文喂给模型。

    max_per_doc 透传给 _select_chunks：同一篇文档最多进 max_per_doc 块，
    选不满 max_chunks 时按重排名次回填。
    """
    selected = _select_chunks(chunks, max_chunks, max_per_doc)
    while selected:
        blocks = _layout(selected, budget)
        if blocks is not None:
            return "\n\n".join(blocks), selected
        # 极端情况（id/heading 特别长或预算特别小）：少给一块，也不给"没有 id 的正文"
        selected = selected[:-1]
    return "", []


# ------------------------------------------------- verify_commands 只读判定

# 反引号单独取：命名后规则一眼可见，也避免在源码里埋难看的转义
_TICK = chr(96)

_READONLY_PROGRAMS = frozenset(
    {
        "kubectl", "oc",
        "systemctl", "journalctl", "dmesg",
        "cat", "grep", "egrep", "rg", "head", "tail", "wc", "ls", "stat", "file",
        "ps", "top", "free", "df", "du", "uptime",
        "ss", "netstat", "lsof", "vmstat", "iostat",
        "curl",
    }
)

_KUBECTL_READ_VERBS = frozenset(
    {
        "get", "describe", "logs", "top", "events", "explain", "version",
        "cluster-info", "api-resources", "api-versions", "auth", "config",
        "diff", "wait", "rollout",
    }
)

# 写操作黑名单：即使程序在白名单里（kubectl get ... && kubectl delete ...），
# 只要某一段出现这些动词就整条拒绝。宁可漏掉一条可用的验证命令，也不能让
# "验证命令"变成把线上 Pod 删掉的入口。
_WRITE_TOKENS = frozenset(
    {
        "delete", "apply", "create", "edit", "patch", "replace", "scale",
        "drain", "cordon", "uncordon", "taint", "label", "annotate",
        "exec", "cp", "attach", "port-forward", "expose", "run", "set",
        "rm", "rmdir", "mv", "dd", "truncate", "mkfs", "fdisk",
        "parted", "tee", "chmod", "chown", "chattr", "mount", "umount",
        "swapoff", "kill", "killall", "pkill", "reboot", "shutdown", "halt",
        "poweroff", "restart", "stop", "start", "disable", "enable", "mask",
        "unmask", "install", "uninstall", "upgrade", "iptables", "ufw",
        "undo", "pause", "resume", "post", "put",
    }
)

# curl 只放行"读"：任何可能改状态或落盘的参数都直接拒绝
_CURL_DENY_FLAGS = (
    "-x", "--request", "-d", "--data", "-f", "--form", "-t", "--upload-file",
    "-o", "--output",
)

_PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
_SHELL_SEP_RE = re.compile(r"\|\||&&|\||;")


def _split_tokens(segment: str) -> list[str]:
    return [t for t in (tok.strip("\"'(),") for tok in segment.split()) if t]


def is_read_only_command(command: str) -> bool:
    """判断一条验证命令是否只读（模板里的占位符 <pod> 允许）。

    判定顺序（任一条不过即 False）：
    1. 先把 <...> 占位符替换掉，再拒绝重定向、命令替换、反引号、换行——
       这些能绕过后面所有按 token 的检查。
    2. 按 | || && ; 切成段，逐段检查：程序名必须在只读白名单里（允许绝对路径）。
    3. kubectl/oc 的第二个 token 必须是只读动词。
    4. 任何 token（去掉前导 -）命中写操作黑名单，或 "set-context" 这种
       "写动词-xxx" 形式，整条拒绝。
    """
    raw = (command or "").strip()
    if not raw:
        return False
    cleaned = _PLACEHOLDER_RE.sub("PLACEHOLDER", raw)
    if ">" in cleaned or "<" in cleaned or "$(" in cleaned or _TICK in cleaned:
        return False
    if "\n" in cleaned or "\r" in cleaned:
        return False
    for segment in _SHELL_SEP_RE.split(cleaned):
        tokens = _split_tokens(segment)
        if not tokens:
            continue
        program = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if program not in _READONLY_PROGRAMS:
            return False
        lows = [t.lower() for t in tokens]
        if program in {"kubectl", "oc"}:
            if len(tokens) < 2 or lows[1] not in _KUBECTL_READ_VERBS:
                return False
        if program == "curl" and any(t.startswith(_CURL_DENY_FLAGS) for t in lows):
            return False
        for token in lows:
            if token.startswith("-"):
                continue
            if token in _WRITE_TOKENS or token.split("-", 1)[0] in _WRITE_TOKENS:
                return False
    return True


def _clean_commands(commands: Sequence[str]) -> list[str]:
    kept: list[str] = []
    for command in commands or []:
        command = (command or "").strip()
        if not command or command in kept:
            continue
        if not is_read_only_command(command):
            logger.info("丢弃非只读 verify_command：{}", command[:120])
            continue
        kept.append(command)
        if len(kept) >= MAX_VERIFY_COMMANDS:
            break
    return kept


# -------------------------------------------------------- JSON 解析与规范化

_FENCE_RE = re.compile(r"^\s*" + _TICK * 3 + r"(?:json)?\s*(.*?)\s*" + _TICK * 3 + r"\s*$", re.S)
_KEEP_FIELDS = ("abstained", "summary", "causes", "verify_commands", "actions", "citations")


def _loads_json(text: str) -> dict[str, Any]:
    """模型输出 -> dict。容忍代码围栏和前后废话，但要求顶层是对象。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型返回空内容")
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        data, _end = json.JSONDecoder().raw_decode(raw.lstrip())
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 解析失败：" + str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象：" + type(data).__name__)
    return data


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _dict_list(value: Any, key: str) -> list[dict[str, Any]]:
    """把 "字符串列表" / "对象列表" 统一成对象列表（模型常把 citations 写成 id 数组）。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                out.append({key: item.strip()})
        elif isinstance(item, dict):
            out.append(dict(item))
    return out


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for s in (_as_str(v) for v in value) if s]


def _normalize_payload(data: dict[str, Any]) -> dict[str, Any]:
    """把模型的松散输出收紧成 DiagnosisCard 能校验的形状。

    只保留六个字段：diagnosis_id / latency_ms / cost_cny 由服务端算，
    模型给了也一律忽略（否则它会自己编耗时和价格）。
    """
    causes: list[dict[str, Any]] = []
    for raw in _dict_list(data.get("causes"), "title"):
        evidence = [
            {
                "chunk_id": _as_str(item.get("chunk_id") or item.get("id") or item.get("cite")),
                "url": _as_str(item.get("url")),
                "score": item.get("score") if isinstance(item.get("score"), (int, float)) else None,
            }
            for item in _dict_list(raw.get("evidence"), "chunk_id")
        ]
        evidence = [e for e in evidence if e["chunk_id"]]
        confidence = raw.get("confidence")
        cause: dict[str, Any] = {
            "title": _as_str(raw.get("title")) or _as_str(raw.get("cause")) or "未命名原因",
            "explanation": _as_str(raw.get("explanation")),
            "evidence": evidence,
        }
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence == confidence:
            cause["confidence"] = min(1.0, max(0.0, float(confidence)))
        causes.append(cause)

    payload = {
        "abstained": bool(data.get("abstained")),
        "summary": _as_str(data.get("summary")),
        "causes": causes,
        "verify_commands": _str_list(data.get("verify_commands")),
        "actions": _str_list(data.get("actions")),
        "citations": _dict_list(data.get("citations"), "chunk_id"),
    }
    return {key: payload[key] for key in _KEEP_FIELDS}


def _sanitize(card: DiagnosisCard, used: Sequence[ScoredChunk]) -> tuple[list[Cause], list[Citation], int]:
    """后置校验：citations / evidence 只能引用 used 里的 chunk_id。

    校验完补一刀：模型给了结论、evidence 也全指向真实块，却漏写 citations 时，
    用这份已校验过的 evidence 回填出处（D7 实测 e040 就是"有结论、零出处"）。
    这不是替模型挑块——块是它自己指的，只是把它漏列的引用补上；evidence 被丢光的
    结论仍然整条不输出，所以回填不会救活没有依据的编造。
    """
    by_id = {c.chunk_id.strip(): c for c in used if (c.chunk_id or "").strip()}

    citations: list[Citation] = []
    seen: set[str] = set()
    for cite in card.citations:
        cid = (cite.chunk_id or "").strip()
        if cid not in by_id or cid in seen:
            continue
        seen.add(cid)
        chunk = by_id[cid]
        citations.append(
            Citation(
                chunk_id=cid,
                title=cite.title or chunk.title,
                heading_path=cite.heading_path or chunk.heading_path,
                url=cite.url or chunk.url,
            )
        )

    causes: list[Cause] = []
    dropped = 0
    for cause in card.causes:
        evidence: list[Evidence] = []
        for item in cause.evidence:
            cid = (item.chunk_id or "").strip()
            chunk = by_id.get(cid)
            if chunk is None:
                continue
            evidence.append(
                Evidence(
                    chunk_id=cid,
                    url=item.url or chunk.url,
                    score=item.score if item.score is not None else round(float(chunk.score or 0.0), 6),
                )
            )
        if not evidence:
            # 证据全被丢光的结论就是"编造的原因"，整条不输出
            dropped += 1
            continue
        causes.append(cause.model_copy(update={"evidence": evidence}))

    if causes and not citations:
        filled: set[str] = set()
        for cause in causes:
            for item in cause.evidence:
                cid = (item.chunk_id or "").strip()
                chunk = by_id.get(cid)
                if chunk is None or cid in filled:
                    continue
                filled.add(cid)
                citations.append(
                    Citation(
                        chunk_id=cid,
                        title=chunk.title,
                        heading_path=chunk.heading_path,
                        url=chunk.url,
                    )
                )
                if len(citations) >= MAX_CITATIONS:
                    break
            if len(citations) >= MAX_CITATIONS:
                break
    return causes, citations, dropped


# ---------------------------------------------------------------- 卡片构造


def _diagnosis_id() -> str:
    return "dg_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + format(random.getrandbits(16), "04x")


def _cost_cny(settings: Settings, prompt_tokens: int, completion_tokens: int) -> float:
    """按 settings 的单价折算成本（元）。单价单位是 元/百万 token。"""
    cost = (
        prompt_tokens * float(settings.llm_price_in_cny_per_mtok or 0.0)
        + completion_tokens * float(settings.llm_price_out_cny_per_mtok or 0.0)
    ) / 1_000_000
    return round(cost, 6)


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _preview_lines(used: Sequence[ScoredChunk], limit: int = 3, width: int = 120) -> str:
    return "\n".join(
        "- " + chunk.cite() + " " + _truncate(_squash(chunk.text), width) for chunk in used[:limit]
    )


def _fallback_citations(
    used: Sequence[ScoredChunk], limit: int = MAX_CITATIONS
) -> list[Citation]:
    return [
        Citation(chunk_id=c.chunk_id.strip(), title=c.title, heading_path=c.heading_path, url=c.url)
        for c in used[:limit]
        if (c.chunk_id or "").strip()
    ]


def _build_card(
    *,
    settings: Settings,
    started: float,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    summary: str,
    causes: list[Cause],
    verify_commands: list[str],
    actions: list[str],
    citations: list[Citation],
    abstained: bool,
    degraded: bool,
    abort_reason: str | None,
) -> DiagnosisCard:
    return DiagnosisCard(
        diagnosis_id=_diagnosis_id(),
        abstained=abstained,
        summary=summary,
        causes=causes,
        verify_commands=verify_commands,
        actions=actions,
        citations=citations,
        latency_ms=_elapsed_ms(started),
        cost_cny=_cost_cny(settings, prompt_tokens, completion_tokens),
        degraded=degraded,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        abort_reason=abort_reason,
    )


def _degraded_head(reason: str) -> str:
    """降级卡的摘要头按失败类别分档（D8/F4）。

    以前不分档，HTTP 401 / 超时 / 长度截断全写"输出无法解析为结构化诊断卡"，
    排查时会把"模型连不上"误读成"模型不会写 JSON"——措辞必须等于事实。
    """
    reason = reason or ""
    if reason.startswith("LLM 调用失败"):
        return "模型调用失败（网络/鉴权/超时），已降级为原文片段（不做推理）："
    if reason.startswith("finish_reason=length"):
        return "模型输出被 max_tokens 截断且重试仍超长，已降级为原文片段（不做推理）："
    if reason.startswith("json_invalid"):
        return "模型输出无法解析为结构化诊断卡，已降级为原文片段（不做推理）："
    return "模型输出不可用，已降级为原文片段（不做推理）："


def _degraded_card(
    used: Sequence[ScoredChunk],
    reason: str,
    *,
    settings: Settings,
    started: float,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
) -> DiagnosisCard:
    """降级卡：不推理、不编原因，只把原文片段摆出来。"""
    head = _degraded_head(reason)
    body = _preview_lines(used)
    summary = head + ("\n" + body if body else "")
    return _build_card(
        settings=settings,
        started=started,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
        summary=summary,
        causes=[],
        verify_commands=[],
        actions=[],
        citations=_fallback_citations(used),
        abstained=False,
        degraded=True,
        abort_reason=(reason or "unknown")[:200],
    )


def _abstain_card(
    used: Sequence[ScoredChunk],
    reason: str,
    *,
    settings: Settings,
    started: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: str,
    summary: str = "",
) -> DiagnosisCard:
    """拒答卡：abstained=True 且 causes 必须为空（清单 ADR-5 的双保险之一）。"""
    return _build_card(
        settings=settings,
        started=started,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
        summary=summary or "现有资料不足以给出确定结论，按设计拒绝猜测。",
        causes=[],
        verify_commands=[],
        actions=[],
        citations=_fallback_citations(used),
        abstained=True,
        degraded=False,
        abort_reason=reason,
    )


def _enforce_limits(card: DiagnosisCard) -> DiagnosisCard:
    """按 prompt 里的契约硬裁模型输出——prompt 说了不算，裁了才算。

    只裁数量与长度，不重排内容：causes/actions 的先后是模型给的优先级。
    这一步必须在 _sanitize 之前，否则删掉的 evidence 还会占配额判断。
    verify_commands 不在这里裁：先过只读白名单再截断，否则"前 3 条都是写命令"
    会把后面真正可用的只读命令挤掉。
    """
    causes = [
        cause.model_copy(
            update={
                # title 不硬裁：截断会把根因截成半句（"内存超过 limit 被 OOMK…"读不出结论），
                # 它只是展示用的短语，长度由 prompt 约束即可
                "explanation": _truncate(cause.explanation, EXPLANATION_MAX_CHARS),
                "evidence": list(cause.evidence[:MAX_EVIDENCE_PER_CAUSE]),
            }
        )
        for cause in card.causes[:MAX_CAUSES]
    ]
    return card.model_copy(
        update={
            "summary": _truncate(card.summary, SUMMARY_MAX_CHARS),
            "causes": causes,
            "actions": [_truncate(action, ACTION_MAX_CHARS) for action in card.actions[:MAX_ACTIONS]],
        }
    )


def sanitize_card(card: DiagnosisCard, used: Sequence[ScoredChunk]) -> DiagnosisCard:
    """后置过滤的公开入口：丢掉指向本次上下文之外资料的 cause / citation。

    给编排层用——它必须先校验模型原始输出、重生成之后再净化，否则"校验恒通过"。
    只改 causes / citations，summary、成本、耗时原样保留。
    """
    causes, citations, dropped = _sanitize(card, used)
    if dropped:
        logger.info("丢弃 {} 条引用了不存在资料的结论", dropped)
    return card.model_copy(update={"causes": causes, "citations": citations})


def _finalize(
    card: DiagnosisCard,
    used: Sequence[ScoredChunk],
    *,
    settings: Settings,
    started: float,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    sanitize: bool = True,
) -> DiagnosisCard:
    card = _enforce_limits(card)
    if not sanitize:
        # 编排层要拿"模型原始输出"去跑引用校验（ADR-4）：先净化再校验等于自己问自己，
        # 恒通过。true 的原样返回未净化的 causes/citations，净化留给调用方在重生成之后做。
        return _build_card(
            summary=card.summary,
            causes=list(card.causes),
            verify_commands=_clean_commands(card.verify_commands),
            actions=list(card.actions),
            citations=list(card.citations),
            abstained=bool(card.abstained),
            degraded=False,
            abort_reason=card.abort_reason,
            settings=settings,
            started=started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
        )
    causes, citations, dropped = _sanitize(card, used)
    commands = _clean_commands(card.verify_commands)
    if dropped:
        logger.info("M7 丢弃 {} 条引用了不存在资料的结论", dropped)

    common = dict(
        settings=settings,
        started=started,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
    )

    if card.abstained:
        return _abstain_card(used, "insufficient_evidence", summary=card.summary, **common)

    if card.causes and not causes:
        # 模型给了结论，但证据全指向不存在的 chunk_id：宁可拒答也不把编造的原因发出去
        summary = "模型给出的结论引用了不存在的资料，已全部丢弃（不编造原因）。可参考的原文片段：\n" + _preview_lines(used)
        return _abstain_card(used, "citations_invalid", summary=summary, **common)

    return _build_card(
        summary=card.summary or (causes[0].title if causes else ""),
        causes=causes,
        verify_commands=commands,
        actions=list(card.actions),
        citations=citations,
        abstained=False,
        degraded=False,
        abort_reason=None,
        **common,
    )


# ------------------------------------------------------------ 流式（D5-A）


def _parse_card(content: str) -> DiagnosisCard:
    """模型输出 -> DiagnosisCard：规范化（_normalize_payload）+ Pydantic 校验。

    抽成一个函数是为了让非流式的 generate() 与流式的 finalize_stream() 只有一处
    解析口径：两处各写一遍，改一次字段规范就会出现"同步接口过了、流式接口还按
    老口径"这种最难发现的分叉。
    """
    return DiagnosisCard.model_validate(_normalize_payload(_loads_json(content)))


def open_stream(
    query: str,
    chunks: Sequence[ScoredChunk],
    *,
    settings: Settings | None = None,
    llm: StreamCallable | None = None,
) -> StreamSession:
    """开一条流：准备上下文与 messages，返回逐段产出正文增量的 StreamSession。

    与 generate() 共用 build_context + prompts.render_user_message 之前的所有逻辑
    （上下文预算、每块必带 id、模型参数），差别只在调用方式：stream=true，逐段
    yield 正文，首 token 时刻由调用方（stream_diagnose）记成 TTFT。

    构造阶段的失败（没有候选块 / 预算放不下 / 没配密钥 / 连接失败）直接抛
    StreamFailed：此时一个 delta 都还没发出去，上层可以干净地回 error 帧。
    """
    s = settings or get_settings()
    query = (query or "").strip()
    selected = list(chunks or [])
    if not selected:
        raise StreamFailed("没有候选块，无法流式生成")

    # 固定开销（head/tail 模板）先从预算里扣掉，与 generate() 完全一致
    overhead = len(prompts.render_user_message(query, []))
    context_budget = max(MIN_CONTEXT_BUDGET, PROMPT_CHAR_BUDGET - overhead)
    context_text, used = build_context(selected, budget=context_budget)
    if not used:
        raise StreamFailed("上下文预算不足：一块都放不下，应改走拒答")

    messages: list[dict[str, str]] = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": prompts.render_user_message(query, [context_text])},
    ]
    call = llm or default_stream_callable(s)
    try:
        source = call(messages, timeout=s.llm_timeout_s)
    except StreamFailed:
        raise
    except Exception as exc:
        raise StreamFailed("流式调用失败：" + type(exc).__name__ + ": " + str(exc)) from exc
    return StreamSession(iter(source), used=used, settings=s)


def finalize_stream(
    text: str,
    chunks: Sequence[ScoredChunk],
    *,
    settings: Settings | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    started: float | None = None,
    model: str = "",
) -> DiagnosisCard:
    """流结束后的收尾：完整正文 -> 一张卡（解析 / 规范化 / 引用过滤 / 限长 / 构造）。

    复用非流式的同一条收尾链（_parse_card -> _finalize -> _enforce_limits /
    _sanitize / _build_card），所以两条路径对"什么算合法引用、卡长什么样"的口径一致。

    两个刻意的取舍：
    - JSON 坏（半截、围栏、前后废话）-> degraded 卡，abort_reason 以 json_invalid
      开头，不抛异常：流都发完了，客户端要的是能展示的卡，不是一个 500。
    - chunks 为空 -> abstain 卡：没有候选块时模型的一切结论都无从校验。
    """
    s = settings or get_settings()
    started = time.perf_counter() if started is None else started
    used = list(chunks or [])
    model = model or s.llm_model
    if not used:
        return _abstain_card([], "no_chunks", settings=s, started=started, model=model)
    try:
        card = _parse_card(text)
    except (ValueError, ValidationError) as exc:
        reason = "json_invalid: " + type(exc).__name__ + ": " + str(exc)
        logger.warning("流式输出无法解析为诊断卡，降级：{}", reason)
        return _degraded_card(
            used,
            reason,
            settings=s,
            started=started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
        )
    return _finalize(
        card,
        used,
        settings=s,
        started=started,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
    )


# ------------------------------------------------------------------ 主入口


def generate(
    query: str,
    chunks: Sequence[ScoredChunk],
    *,
    settings: Settings | None = None,
    llm: LLMCallable | None = None,
    sanitize: bool = True,
) -> DiagnosisCard:
    """把候选块生成一张诊断卡。

    不抛异常是硬约束：调用方（pipeline / API）拿到的一定是可序列化的卡。
    失败分两档——JSON/schema 坏且重试后仍坏 -> degraded 卡；资料不足以支撑
    结论 -> abstained 卡。两者都不编造原因。
    """
    started = time.perf_counter()
    s = settings or get_settings()
    call = llm or default_llm_callable(s)
    query = (query or "").strip()
    model = s.llm_model

    if not list(chunks or []):
        return _abstain_card([], "no_chunks", settings=s, started=started, model=model)

    # 固定开销（head/tail 模板 + system prompt 之外的部分）先从预算里扣掉
    overhead = len(prompts.render_user_message(query, []))
    context_budget = max(MIN_CONTEXT_BUDGET, PROMPT_CHAR_BUDGET - overhead)
    context_text, used = build_context(chunks, budget=context_budget)
    if not used:
        return _abstain_card([], "context_too_large", settings=s, started=started, model=model)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": prompts.render_user_message(query, [context_text])},
    ]
    attempts = 1 + max(0, int(s.llm_max_retry or 0))
    prompt_tokens = 0
    completion_tokens = 0
    last_error = ""

    for attempt in range(attempts):
        try:
            response = call(messages, timeout=s.llm_timeout_s)
        except Exception as exc:  # 网络/HTTP 失败重试没有意义，直接降级
            last_error = "LLM 调用失败：" + type(exc).__name__ + ": " + str(exc)
            logger.warning("M7 调用失败，降级：{}", exc)
            break

        prompt_tokens += int(getattr(response, "prompt_tokens", 0) or 0)
        completion_tokens += int(getattr(response, "completion_tokens", 0) or 0)
        model = getattr(response, "model", "") or model

        if (getattr(response, "finish_reason", "") or "") == "length":
            # JSON 被 max_tokens 掐断：不是格式问题，是写太长，要求砍条数而不是重排版
            last_error = "finish_reason=length：输出被 max_tokens 截断"
            logger.warning("M7 第 {} 次输出被长度截断", attempt + 1)
            if attempt + 1 < attempts:
                messages = messages + [
                    {"role": "assistant", "content": (response.content or "")[:4000]},
                    {"role": "user", "content": prompts.LENGTH_RETRY_HINT},
                ]
                continue
            break

        try:
            card = _parse_card(response.content)
        except (ValueError, ValidationError) as exc:
            # 与非流式 parse_diagnosis 的 reason 前缀保持一致：降级卡摘要头靠它分档（D8/F4）
            last_error = "json_invalid: " + type(exc).__name__ + ": " + str(exc)
            logger.warning("M7 第 {} 次输出不可用：{}", attempt + 1, last_error)
            if attempt + 1 < attempts:
                messages = messages + [
                    {"role": "assistant", "content": (response.content or "")[:4000]},
                    {"role": "user", "content": prompts.RETRY_HINT},
                ]
                continue
            break

        return _finalize(
            card,
            used,
            settings=s,
            started=started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            sanitize=sanitize,
        )

    return _degraded_card(
        used,
        last_error,
        settings=s,
        started=started,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
    )
