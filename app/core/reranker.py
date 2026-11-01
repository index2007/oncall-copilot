"""重排器（M6）：Cross-Encoder 精排，Top20 -> Top5。

为什么走远端 rerank 而不是本地 cross-encoder：清单 §3.1 定的是全 API 方案，本机不装
torch / sentence-transformers；§6.5 实测重排只要 200 ms（3 条候选），而生成独占 3 s 上下，
用 200 ms 换 Top-1 十几个百分点是这笔账里最划算的一笔。

为什么不用 OpenAI 标准格式：硅基流动的 /v1/rerank 不是 chat/completions 那套，
body 只有 model / query / documents / top_n 四个字段，返回 results[].relevance_score（0~1）。
所以这里用 httpx 直连，不套 openai SDK。

三条硬纪律（都是"重排是锦上添花，不是必需品"的推论）：

1. **任何失败都降级，不抛异常**：密钥缺失、超时、5xx、返回畸形，一律退回 RRF 顺序 +
   degraded=True。重排挂了只能是"少赚一点精度"，不能变成"整条链路 500"。
2. **降级时不伪造分数**：rerank_score 保持 None，score 还是原来的 RRF 分。下游
   guard.should_abstain 找不到 rerank_score 会退回 score，而 RRF 分（k=60 时 1/61 量级）
   必然低于 abstain_threshold——所以 API 层必须**先看 degraded 再决定要不要走拒答**，
   否则重排一挂就等于全量拒答（ADR-5 的"双保险"就是为此准备的）。
3. **密钥只从 settings 取**：日志里只出现脱敏后的 key（前 6 + 后 4），不打印请求头。

分批：settings.rerank_max_docs 控制单次请求的候选条数（默认 20，硅基流动有上限）。
超了就按 RRF 顺序切批、逐批打分、跨批合并后统一排序。**不做"某批失败就只用剩下的批"**：
两批分数来自两次调用，混排出来的 rank 没法解释，宁可整体降级。

计数口径：api_calls / tokens / request_ms 按**最近一次** rerank 归集（一次问答一次重排，
成本按请求算），与 Embedder 的累计口径不同——索引是批量任务，问答是单次请求。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx
from loguru import logger
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import Settings, get_settings
from app.core.schemas import ScoredChunk

RERANK_PATH = "/rerank"
DEFAULT_RETRY_ATTEMPTS = 3


class FatalRerankError(RuntimeError):
    """不可重试的错误（401/403/404/400/密钥缺失）：重试只会多烧额度。"""


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, FatalRerankError):
        return False
    return isinstance(exc, (httpx.HTTPError, RuntimeError))


def _mask(key: str) -> str:
    """日志里只出现脱敏 key：够辨认是哪把，不够还原。"""
    if not key:
        return "(empty)"
    return key[:6] + "..." + key[-4:] if len(key) > 12 else "***"


def _extract_tokens(body: dict[str, Any]) -> int:
    """token 数有三处可能出现（usage / meta.billed_units / meta.tokens），都试一遍。

    硅基流动 rerank 的用量字段在不同版本里挪过位置，写死一处会在换版本时静默变 0。
    """
    candidates: list[Any] = []
    usage = body.get("usage")
    if isinstance(usage, dict):
        candidates.extend([usage.get("total_tokens"), usage.get("input_tokens"), usage.get("prompt_tokens")])
    meta = body.get("meta")
    if isinstance(meta, dict):
        for key in ("billed_units", "tokens"):
            sub = meta.get(key)
            if isinstance(sub, dict):
                candidates.extend(
                    [sub.get("total_tokens"), sub.get("input_tokens"), sub.get("prompt_tokens")]
                )
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


class Reranker:
    """硅基流动 /v1/rerank 客户端 + 降级策略。"""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "BAAI/bge-reranker-v2-m3",
        timeout: float = 30.0,
        max_docs: int = 20,
        price_cny_per_mtok: float = 0.0,
        client: Any | None = None,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_wait: Callable[[Any], float] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_docs = max(1, int(max_docs))
        self.price_cny_per_mtok = price_cny_per_mtok
        # 测试注入假 client（只要有 post(url, json=, headers=, timeout=)）；生产为 None
        self._client = client
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_wait = retry_wait or wait_exponential(multiplier=0.5, min=0.5, max=4)

        self.api_calls = 0
        self.tokens = 0
        self.request_ms = 0.0
        self.last_stats: dict[str, Any] = {}

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Reranker":
        s = settings or get_settings()
        return cls(
            api_key=s.sf_api_key,
            base_url=s.sf_base_url,
            model=s.rerank_model,
            timeout=s.rerank_timeout_s,
            max_docs=s.rerank_max_docs,
            price_cny_per_mtok=s.rerank_price_cny_per_mtok,
        )

    # -- 对外接口 ----------------------------------------------
    def rerank(self, query: str, chunks: list[ScoredChunk], top_n: int = 5) -> list[ScoredChunk]:
        """重排；失败则退回 RRF 顺序并打 degraded=True。**不抛异常**。

        返回条数 = min(top_n, len(chunks))，rank 从 1 开始连续编号。
        成功路径写回 rerank_score + score（同值）；降级路径 rerank_score 保持 None。
        """
        started = time.perf_counter()
        top_n = max(0, int(top_n))
        ordered = sorted(list(chunks or []), key=lambda c: (-float(c.score or 0.0), c.chunk_id))
        self.api_calls = 0
        self.tokens = 0
        self.request_ms = 0.0

        if not ordered:
            return self._done([], started, degraded=False, reason="no_candidates", batches=0, candidates=0)
        if top_n == 0:
            return self._done([], started, degraded=False, reason="top_n=0", batches=0, candidates=len(ordered))

        if not (query or "").strip():
            # 空 query 打分没有意义，直接降级，别浪费一次调用
            logger.warning("rerank 跳过：query 为空，回退 RRF 顺序")
            return self._done(
                self._degrade(ordered, top_n), started, degraded=True, reason="empty_query", batches=0,
                candidates=len(ordered),
            )

        scores: dict[int, float] = {}
        batches = 0
        degraded = False
        reason = "ok"

        for start in range(0, len(ordered), self.max_docs):
            batch = ordered[start : start + self.max_docs]
            documents = [self._document(c) for c in batch]
            try:
                got, tokens = self._post_batch(query, documents)
            except FatalRerankError as exc:
                degraded, reason = True, "fatal: " + str(exc)[:200]
                break
            except Exception as exc:  # noqa: BLE001 —— 重排失败必须降级，不能让上层 500
                degraded, reason = True, "failed: {}: {}".format(type(exc).__name__, str(exc)[:200])
                break
            batches += 1
            self.tokens += tokens
            for offset, score in got.items():
                scores[start + offset] = score

        if degraded:
            logger.warning("rerank 降级（{}），回退 RRF 顺序（{} 条候选）", reason, len(ordered))
            return self._done(
                self._degrade(ordered, top_n), started, degraded=True, reason=reason, batches=batches,
                candidates=len(ordered),
            )

        ranked: list[ScoredChunk] = []
        for index, chunk in enumerate(ordered):
            score = scores.get(index)
            if score is None:
                continue
            ranked.append(chunk.model_copy(update={"rerank_score": score, "score": score, "degraded": False}))
        # 分数相同按 chunk_id 兜底，保证同样输入两次跑出同样的顺序（评测要可复现）
        ranked.sort(key=lambda c: (-float(c.rerank_score or 0.0), c.chunk_id))
        out = [c.model_copy(update={"rank": i}) for i, c in enumerate(ranked[:top_n], start=1)]
        return self._done(out, started, degraded=False, reason="ok", batches=batches, candidates=len(ordered))

    # -- 内部 --------------------------------------------------
    @staticmethod
    def _degrade(ordered: list[ScoredChunk], top_n: int) -> list[ScoredChunk]:
        """降级：调用方已按 RRF 分排好序，这里只打标记与 rank。

        不写 rerank_score —— 降级时没有重排分，写 0 或留着 RRF 分都会让下游误以为
        "这是重排结果"。degraded=True 是给下游的唯一信号。
        """
        return [
            c.model_copy(update={"degraded": True, "rank": i})
            for i, c in enumerate(ordered[: max(0, top_n)], start=1)
        ]

    @staticmethod
    def _document(chunk: ScoredChunk) -> str:
        """送进 rerank 的正文：正文为空时退到标题路径 / 标题 / chunk_id。

        M2 保证单块 ≤800 tokens，落在 bge-reranker-v2-m3 的上下文里，不做额外截断；
        但空文档会让整批 400，所以必须有兜底。
        """
        text = (chunk.text or "").strip()
        if text:
            return text
        heading = " / ".join(p for p in (chunk.title, chunk.heading_path) if p).strip(" /")
        return heading or chunk.chunk_id or "empty"

    def _post_batch(self, query: str, documents: list[str]) -> tuple[dict[int, float], int]:
        """单批请求，带 tenacity 指数退避；重试耗尽后抛异常，由 rerank 决定是否降级。"""
        payload = {"model": self.model, "query": query, "documents": documents, "top_n": len(documents)}
        retrying = Retrying(
            stop=stop_after_attempt(self.retry_attempts),
            wait=self.retry_wait,
            retry=retry_if_exception(_should_retry),
            reraise=True,
        )
        return retrying(self._post_once, payload, documents)

    def _post_once(
        self, payload: dict[str, Any], documents: list[str]
    ) -> tuple[dict[int, float], int]:
        if not self.api_key:
            raise FatalRerankError("SF_API_KEY 未配置（ONCALL_ENV_FILE 指向的 env 文件里缺 SF_API_KEY）")
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        url = self.base_url + RERANK_PATH

        started = time.perf_counter()
        if self._client is not None:
            resp = self._client.post(url, json=payload, headers=headers, timeout=self.timeout)
        else:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
        self.request_ms += (time.perf_counter() - started) * 1000
        self.api_calls += 1

        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            detail = (getattr(resp, "text", "") or "")[:300]
            # 4xx 里只有 429 值得重试，其余重试也是白费额度
            if status == 429 or status >= 500:
                raise RuntimeError("rerank HTTP {}: {}".format(status, detail))
            raise FatalRerankError(
                "rerank HTTP {}（不重试，检查 key/模型名 {}）: {}".format(status, _mask(self.api_key), detail)
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError("rerank 返回不是合法 JSON: {}".format(exc)) from exc
        if not isinstance(body, dict):
            raise RuntimeError("rerank 返回体不是对象: {}".format(type(body).__name__))

        results = body.get("results")
        if not isinstance(results, list) or not results:
            raise RuntimeError("rerank 返回 results 为空")
        scores: dict[int, float] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            raw = item.get("relevance_score", item.get("score"))
            if isinstance(index, bool) or not isinstance(index, int):
                continue
            if index < 0 or index >= len(documents) or raw is None:
                continue
            try:
                scores[index] = float(raw)
            except (TypeError, ValueError):
                continue
        if len(scores) != len(documents):
            # 少一条就没法解释 rank，按失败处理（重试后仍失败则整批降级）
            raise RuntimeError(
                "rerank 返回条数不匹配: 请求 {} 条，可用 {} 条".format(len(documents), len(scores))
            )
        return scores, _extract_tokens(body)

    def _done(
        self,
        result: list[ScoredChunk],
        started: float,
        *,
        degraded: bool,
        reason: str,
        batches: int,
        candidates: int,
    ) -> list[ScoredChunk]:
        self.last_stats = {
            "model": self.model,
            "degraded": degraded,
            "reason": reason,
            "candidates": candidates,
            "returned": len(result),
            "batches": batches,
            "api_calls": self.api_calls,
            "tokens": self.tokens,
            "cost_cny": self.cost_cny,
            "request_ms": round(self.request_ms, 1),
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return result

    @property
    def cost_cny(self) -> float:
        return round(self.tokens / 1_000_000 * self.price_cny_per_mtok, 6)


# ---------------------------------------------------------------------------
# 模块级单例：清单 §5 M6 的接口是函数形状，调用方不必自己管生命周期。
# ---------------------------------------------------------------------------

_DEFAULT_RERANKER: Reranker | None = None


def get_reranker(settings: Settings | None = None) -> Reranker:
    global _DEFAULT_RERANKER
    if _DEFAULT_RERANKER is None:
        _DEFAULT_RERANKER = Reranker.from_settings(settings)
    return _DEFAULT_RERANKER


def reset_reranker() -> None:
    """测试用：丢掉模块级单例。"""
    global _DEFAULT_RERANKER
    _DEFAULT_RERANKER = None


def rerank(query: str, chunks: list[ScoredChunk], top_n: int = 5) -> list[ScoredChunk]:
    """清单 §5 M6 的接口形状：模块级函数 + 复用单例 client。"""
    return get_reranker().rerank(query, chunks, top_n=top_n)


# ---------------------------------------------------------------------------
# 自检 CLI：3 条候选跑一次真实 rerank（只 1 次请求，够验证分数分布与耗时）
# ---------------------------------------------------------------------------

PROBE_QUERY = "Pod 一直 Pending，kubectl describe 显示 0/3 nodes are available: 3 Insufficient memory"

PROBE_DOCUMENTS: tuple[tuple[str, str, str], ...] = (
    (
        "c_probe_hit",
        "k8s",
        "节点可分配内存不足（Insufficient memory）。调度器找不到满足 Pod requests 的节点时，"
        "Pod 会保持 Pending。用 kubectl describe node 查看 Allocated resources 与 Allocatable，"
        "确认剩余可分配内存；再核对 Pod 的 requests.memory 是否超过单节点可分配量。"
        "处理方向：调低 requests、驱逐无关负载或扩容节点池。",
    ),
    (
        "c_probe_mysql",
        "mysql",
        "InnoDB 死锁检测：当两个事务互相等待对方持有的行锁时，InnoDB 会回滚代价较小的事务并报 "
        "ER_LOCK_DEADLOCK。用 SHOW ENGINE INNODB STATUS 的 LATEST DETECTED DEADLOCK 段查看两个事务"
        "各自持有的锁与等待的锁，再按主键顺序访问、缩短事务、加合适索引来规避。",
    ),
    (
        "c_probe_nginx",
        "nginx",
        "ngx_http_log_module 的 log_format 指令定义日志字段，access_log 指定输出路径、缓冲与格式；"
        "open_log_file_cache 控制文件句柄缓存。字段里可用 $upstream_response_time 观察上游耗时，"
        "用 $request_time 看整请求耗时。",
    ),
)


def _probe_chunks() -> list[ScoredChunk]:
    # 用 RRF 量级的分数（1/61 上下的量级），模拟 M5 的真实输出
    base = [0.0164, 0.0159, 0.0154]
    return [
        ScoredChunk(
            chunk_id=cid,
            doc_id="d-" + cid,
            text=text,
            source=source,
            heading_path="probe > " + source,
            title="probe",
            score=score,
            rrf_score=score,
        )
        for (cid, source, text), score in zip(PROBE_DOCUMENTS, base)
    ]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="重排自检：3 条内置候选跑一次真实 /rerank")
    parser.add_argument("--query", default=PROBE_QUERY, help="自定义 query")
    parser.add_argument("--top-n", type=int, default=3, help="返回条数（默认 3）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    reranker = Reranker.from_settings()
    chunks = _probe_chunks()
    out = reranker.rerank(args.query, chunks, top_n=args.top_n)
    stats = dict(reranker.last_stats)

    if args.json:
        print(json.dumps(
            {"stats": stats, "results": [c.model_dump() for c in out]}, ensure_ascii=False, indent=2,
        ))
        return 0

    print("query:", args.query)
    print("model:", stats["model"], "| degraded:", stats["degraded"], "| reason:", stats["reason"])
    print("candidates:", stats["candidates"], "batches:", stats["batches"],
          "tokens:", stats["tokens"], "cost_cny:", stats["cost_cny"])
    print("timing: request_ms=%.1f total_ms=%.1f" % (stats["request_ms"], stats["total_ms"]))
    header = "{:>3}  {:<14} {:<7} {:>8}  {}"
    print(header.format("rnk", "chunk_id", "source", "score", "degraded"))
    for c in out:
        print(header.format(
            str(c.rank), c.chunk_id, c.source or "-",
            "-" if c.rerank_score is None else "{:.4f}".format(c.rerank_score), str(c.degraded),
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
