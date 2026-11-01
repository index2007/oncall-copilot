"""Query 理解（M4）：把自然语言/报错文本变成结构化检索计划（QueryPlan）。

规则优先（清单 ADR-6）。理由：
1. 运维领域词是**封闭集合**——exit code 137 / OOMKilled / 0/3 nodes are available
   就那几种写法，正则和词典能覆盖 90%+；
2. 规则零成本、零延迟、可解释（能指出命中了哪个 span），LLM 只在
   "人话描述、一个关键词都没有" 时兜底；
3. 清单 M4 验收要求 LLM 调用占比 < 20%，规则优先是唯一能达到这个比例的路线。

两个字段要分清：
- need_llm ：规则没给出任何信号、且 query 超过 20 字 -> 该调 LLM
- llm_used ：真的调了并且解析成功
评测时两个都报，否则"没调 LLM"会被误读成"规则覆盖率高"。

LLM 路径只在 deepseek_api_key 存在时启用；调用失败不影响主流程——规则结果照常返回，
plan.llm_error 里留失败原因（清单 ADR-6 的代价：规则有覆盖盲区）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Callable

import httpx
import jieba
from loguru import logger

from app.config import Settings, get_settings
from app.core.schemas import INTENTS, QueryPlan, WEAK_EVENTS

MIN_LLM_QUERY_CHARS = 20

# ---------------------------------------------------------------- 资源类型词典
# 顺序是**同位置**的优先级（"容器组"必须排在 "容器" 前面，否则会被判成 container）；
# 不同位置时取最先出现的那个，见 extract_resource 的说明。
RESOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "pod": ("pod", "pods", "容器组", "pod 组"),
    "container": ("container", "容器", "sidecar", "init 容器"),
    "node": ("node", "nodes", "节点", "工作节点", "master 节点"),
    "deployment": ("deployment", "deploy", "无状态负载"),
    "daemonset": ("daemonset", "daemon set"),
    "statefulset": ("statefulset",),
    "job": ("job", "jobs"),
    "cronjob": ("cronjob", "cron job"),
    "service": ("service", "svc", "服务发现", "clusterip", "nodeport"),
    "ingress": ("ingress", "网关", "入口"),
    "configmap": ("configmap", "config map", "配置项"),
    "secret": ("secret", "密文"),
    "pvc": ("pvc", "persistentvolumeclaim", "存储卷声明"),
    "pv": ("pv", "persistentvolume"),
    "storageclass": ("storageclass", "storage class", "存储类"),
    "namespace": ("namespace", "命名空间", "ns"),
    "kubelet": ("kubelet",),
    "apiserver": ("apiserver", "kube-apiserver", "api server"),
    "controller-manager": ("controller-manager", "controller manager", "kube-controller-manager"),
    "scheduler": ("scheduler", "kube-scheduler", "调度器"),
    "etcd": ("etcd",),
    "coredns": ("coredns", "dns", "域名解析"),
    "redis": ("redis", "哨兵", "sentinel", "cluster 槽"),
    "mysql": ("mysql", "innodb", "innodb_lock_wait_timeout"),
    "nginx": ("nginx", "upstream"),
    "kafka": ("kafka", "broker", "分区副本"),
}

# 资源类型 -> 子库路由（content/ 下的目录名，也是 LanceDB 的 source 字段）
SUB_INDEX: dict[str, str] = {
    **{name: "k8s" for name in (
        "pod", "container", "node", "deployment", "daemonset", "statefulset", "job",
        "cronjob", "service", "ingress", "configmap", "secret", "pvc", "pv",
        "storageclass", "namespace", "kubelet", "apiserver", "controller-manager",
        "scheduler", "etcd", "coredns",
    )},
    "redis": "redis",
    "mysql": "mysql",
    "nginx": "nginx",
}

# ---------------------------------------------------------------- 错误码正则
def _fmt_exit(m: re.Match[str]) -> str:
    return "exit code " + m.group(1)


def _fmt_nodes(m: re.Match[str]) -> str:
    return re.sub(r"\s+", "", m.group(1)) + " nodes are available"


def _fmt_status(m: re.Match[str]) -> str:
    return m.group(1)


def _fmt_errno(m: re.Match[str]) -> str:
    return m.group(1)


ERROR_CODE_PATTERNS: tuple[tuple[str, re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    ("exit_code", re.compile(r"exit\s*code\s*[:：=]?\s*(-?\d+)", re.I), _fmt_exit),
    ("nodes_unavailable", re.compile(r"(\d+\s*/\s*\d+)\s*nodes?\s+(?:are\s+)?available", re.I), _fmt_nodes),
    ("http_status", re.compile(r"(?:http|status|状态码|返回码|响应码|error\s*code)\s*[:：=]?\s*([45]\d{2})\b", re.I), _fmt_status),
    ("http_status_worded", re.compile(r"\b([45]\d{2})\s+(?:bad\s+gateway|gateway\s+time-?\s*out|internal\s+server\s+error|service\s+unavailable|too\s+many\s+requests|forbidden)\b", re.I), _fmt_status),
    ("mysql_errno", re.compile(r"\bERROR\s+(\d{3,4})\b"), _fmt_errno),
    ("syscall_errno", re.compile(r"\b(?:errno|os\s*error)\s*[:=]?\s*(\d{1,4})\b", re.I), _fmt_errno),
)

# ---------------------------------------------------------------- 事件原因词典（40 条）
EVENT_REASONS: tuple[str, ...] = (
    "OOMKilled",
    "OOMKilling",
    "SystemOOM",
    "CrashLoopBackOff",
    "BackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "ErrImageNeverPull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
    "FailedScheduling",
    "Unschedulable",
    "Evicted",
    "NodeNotReady",
    "DiskPressure",
    "MemoryPressure",
    "PIDPressure",
    "NetworkUnavailable",
    "FailedMount",
    "FailedAttachVolume",
    "FailedCreatePodSandBox",
    "FailedPostStartHook",
    "Unhealthy",
    "Pending",
    "ContainerCreating",
    "Error",
    "DeadlineExceeded",
    "Terminating",
    "upstream timed out",
    "connection refused",
    "connection reset by peer",
    "too many connections",
    "Deadlock found",
    "no space left on device",
    "Cannot allocate memory",
    "out of memory",
    "maxmemory",
    "LOADING",
    "CLUSTERDOWN",
)

# ---------------------------------------------------------------- 意图正则
DIAGNOSE_RE = re.compile(
    r"报错|失败|错误|挂了|挂掉|起不来|打不开|不可用|超时|异常|崩溃|重启|被杀|被杀掉"
    r"|oom|502|503|504|crash|fail(?:ed|ure)?|unavailable|timeout|refused|panic|slow|变慢|卡住",
    re.I,
)
HOWTO_RE = re.compile(r"怎么|如何|怎样|配置|设置|调整|开启|关闭|步骤|方案|how\s+to|configure|setup|tune|enable|disable", re.I)
EXPLAIN_RE = re.compile(r"是什么|原理|区别|为什么|介绍|含义|what\s+is|what's|difference|why\s+does", re.I)

STOPWORDS: frozenset[str] = frozenset({
    "的", "了", "是", "在", "我", "我们", "你", "他", "和", "与", "及", "或", "就", "都", "也",
    "但", "而", "被", "把", "给", "让", "对", "从", "到", "上", "下", "里", "中", "后", "前",
    "一个", "这个", "那个", "什么", "为什么", "怎么", "如何", "现在", "一直", "然后", "而且",
    "the", "a", "an", "is", "are", "was", "were", "be", "and", "or", "of", "to", "in", "on",
    "for", "with", "it", "this", "that", "when", "why", "how", "not", "no",
})

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_KEEP_RE = re.compile(r"[0-9a-zA-Z\u3400-\u4dbf\u4e00-\u9fff]+")


def _ascii_only(text: str) -> bool:
    return not _CJK_RE.search(text)


def _phrase_re(phrase: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(p) for p in phrase.split())
    if _ascii_only(phrase):
        return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])", re.I)
    return re.compile(re.escape(phrase), re.I)


_RESOURCE_ASCII: dict[str, re.Pattern[str]] = {}
_RESOURCE_CJK: dict[str, tuple[str, ...]] = {}
for _name, _aliases in RESOURCE_TYPES.items():
    _ascii = [_phrase_re(a) for a in _aliases if _ascii_only(a)]
    if _ascii:
        _RESOURCE_ASCII[_name] = re.compile("|".join(p.pattern for p in _ascii), re.I)
    _cjk = tuple(a for a in _aliases if not _ascii_only(a))
    if _cjk:
        _RESOURCE_CJK[_name] = _cjk

_REASON_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((r, _phrase_re(r)) for r in EVENT_REASONS)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _lang(text: str) -> str:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return "unknown"
    cjk = len(_CJK_RE.findall(text))
    return "zh" if cjk / len(chars) >= 0.15 else "en"


def extract_error_codes(text: str) -> list[str]:
    found: list[str] = []
    for _, pattern, fmt in ERROR_CODE_PATTERNS:
        for m in pattern.finditer(text):
            value = fmt(m)
            if value and value not in found:
                found.append(value)
    return found


def extract_event_reasons(text: str) -> list[str]:
    found: list[str] = []
    for reason, pattern in _REASON_RES:
        if pattern.search(text):
            found.append(reason)
    return found


def extract_resource(text: str) -> tuple[str | None, str | None]:
    """取**最先出现**的资源关键词；同一位置按 RESOURCE_TYPES 顺序决定（长的别名占优）。

    为什么不是"按词典顺序取第一个命中"：中文报错里主语通常先出现。
    "worker 节点变成 NodeNotReady，上面跑的容器全挂了" 主语是节点，
    "DaemonSet 没有在每个节点上被创建" 主语是 DaemonSet；
    按词典顺序会分别被 container / node 抢先命中（D3 探针实测 2 条判错）。
    """
    best: tuple[int, int, str] | None = None
    for order, name in enumerate(RESOURCE_TYPES):
        pos = -1
        ascii_re = _RESOURCE_ASCII.get(name)
        if ascii_re is not None:
            match = ascii_re.search(text)
            if match is not None:
                pos = match.start()
        for token in _RESOURCE_CJK.get(name, ()):
            idx = text.find(token)
            if idx >= 0 and (pos < 0 or idx < pos):
                pos = idx
        if pos < 0:
            continue
        if best is None or pos < best[0]:
            best = (pos, order, name)
    if best is None:
        return None, None
    return best[2], SUB_INDEX.get(best[2])


def classify_intent(text: str, plan: QueryPlan) -> str:
    if plan.error_codes or plan.event_reasons or DIAGNOSE_RE.search(text):
        return "diagnose"
    if HOWTO_RE.search(text):
        return "howto"
    if EXPLAIN_RE.search(text):
        return "explain"
    return "unknown"


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    for token in jieba.lcut(text.lower()):
        for piece in _KEEP_RE.findall(token):
            if len(piece) < 2 or piece in STOPWORDS or piece.isdigit():
                continue
            if piece not in out:
                out.append(piece)
            if len(out) >= limit:
                return out
    return out


def should_use_llm(plan: QueryPlan) -> bool:
    return (not plan.rule_hit) and len(plan.clean) > MIN_LLM_QUERY_CHARS


# ---------------------------------------------------------------- LLM 兜底
LLM_SYSTEM_PROMPT = (
    "你是运维检索系统的查询理解模块。只输出一个 JSON 对象，不要解释，不要 Markdown。\n"
    "字段：intent（diagnose/howto/explain/unknown）、resource_type（pod/node/deployment/"
    "service/ingress/redis/mysql/nginx/kafka/etcd/kubelet 等，判断不出给 null）、"
    "event_reasons（数组，只能填运维事件名，如 OOMKilled/Evicted/CrashLoopBackOff，没有就空数组）、"
    "keywords（数组，最多 6 个检索关键词）。\n"
    "只依据用户给的文本判断，不要猜测不存在的错误码。"
)


def default_llm_callable(settings: Settings | None = None) -> Callable[[QueryPlan], dict[str, Any]]:
    def _call(plan: QueryPlan) -> dict[str, Any]:
        s = settings or get_settings()
        if not s.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置，跳过 LLM 分类")
        url = s.deepseek_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": s.llm_model,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": plan.clean},
            ],
            "temperature": 0,
            "max_tokens": 200,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": "Bearer " + s.deepseek_api_key, "Content-Type": "application/json"}
        with httpx.Client(timeout=s.query_llm_timeout_s) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        content = body["choices"][0]["message"]["content"]
        return json.loads(content)

    return _call


def _merge_llm(plan: QueryPlan, data: dict[str, Any]) -> QueryPlan:
    intent = str(data.get("intent") or "").strip().lower()
    updates: dict[str, Any] = {}
    if intent in INTENTS and plan.intent == "unknown":
        updates["intent"] = intent
    resource = data.get("resource_type")
    if plan.resource_type is None and isinstance(resource, str) and resource.strip():
        name = resource.strip().lower()
        canonical, sub = extract_resource(name)
        updates["resource_type"] = canonical or name
        if plan.sub_index is None:
            updates["sub_index"] = sub or SUB_INDEX.get(name)
    reasons = [str(r).strip() for r in (data.get("event_reasons") or []) if str(r).strip()]
    if reasons:
        merged = list(plan.event_reasons)
        for r in reasons:
            if r not in merged:
                merged.append(r)
        updates["event_reasons"] = merged[:10]
    keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
    if keywords:
        merged_kw = list(plan.keywords)
        for k in keywords:
            if k not in merged_kw:
                merged_kw.append(k)
        updates["keywords"] = merged_kw[:10]
    return plan.model_copy(update=updates) if updates else plan


def analyze(
    text: str,
    *,
    llm: Callable[[QueryPlan], dict[str, Any]] | None = None,
    settings: Settings | None = None,
    allow_llm: bool | None = None,
) -> QueryPlan:
    """规则优先的 Query 理解入口。llm 可注入（测试用），默认走 DeepSeek HTTP。"""
    clean = _clean(text)
    plan = QueryPlan(raw=text or "", clean=clean, language=_lang(clean))
    plan.error_codes = extract_error_codes(clean)
    plan.event_reasons = extract_event_reasons(clean)
    plan.resource_type, plan.sub_index = extract_resource(clean)
    plan.intent = classify_intent(clean, plan)  # type: ignore[assignment]
    plan.keywords = extract_keywords(clean)

    if allow_llm is None:
        allow_llm = settings.query_llm_enabled if settings is not None else True

    plan.need_llm = should_use_llm(plan)
    if plan.need_llm and allow_llm:
        call = llm or default_llm_callable(settings)
        started = time.perf_counter()
        try:
            data = call(plan)
            if not isinstance(data, dict):
                raise ValueError("LLM 返回的不是 JSON 对象")
            plan = _merge_llm(plan, data)
            plan.llm_used = True
        except Exception as exc:  # noqa: BLE001 - 任何失败都只降级，不打断主流程
            plan.llm_error = (type(exc).__name__ + ": " + str(exc))[:200]
            logger.warning("Query 理解 LLM 兜底失败，仅用规则结果：{}", plan.llm_error)
        finally:
            plan.llm_latency_ms = round((time.perf_counter() - started) * 1000, 1)

    if plan.llm_used:
        plan.source = "rule+llm"
    elif plan.llm_error:
        plan.source = "rule+llm_failed"
    else:
        plan.source = "rule"
    return plan


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Query 理解自检：看一条文本被抽成什么检索计划")
    parser.add_argument("text", nargs="+", help="报错/告警文本（可多段，空格分隔自动拼接）")
    parser.add_argument("--no-llm", action="store_true", help="禁止 LLM 兜底，只看规则")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    plan = analyze(" ".join(args.text), allow_llm=not args.no_llm)
    if args.json:
        print(json.dumps(plan.model_dump(), ensure_ascii=False, indent=2))
    else:
        print("intent        :", plan.intent)
        print("resource_type :", plan.resource_type, "/ sub_index:", plan.sub_index)
        print("error_codes   :", plan.error_codes)
        print("event_reasons :", plan.event_reasons)
        print("keywords      :", plan.keywords)
        print("search_text   :", plan.search_text)
        print("need_llm={} llm_used={} source={}".format(plan.need_llm, plan.llm_used, plan.source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
