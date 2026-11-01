"""Embedding 客户端：硅基流动 bge-m3，HTTP 调用，本地落盘缓存。

清单 §5 的两条硬约束在这里落地：

1. **本机不装模型**：只有 httpx，没有 torch / onnxruntime / sentence-transformers。
2. **索引要反复重建，不能让 API 重复计费、触发限流**：每次请求前先查
   data/embed_cache.jsonl（key = sha256(model + 正文)），命中就不发请求；
   没命中的按 batch=16 合并成一次 /embeddings 调用，拿到结果立刻追加落盘。
   所以重跑索引的增量成本≈0，中途失败也能断点续跑。

分词/切块变化会让正文变化 -> key 变化 -> 自然失效，不需要额外的版本号。
换模型同理：key 里带了模型名。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

class FatalEmbeddingError(RuntimeError):
    """不可重试的错误（401/403/404/400）：重试只会多烧额度。"""


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, FatalEmbeddingError):
        return False
    return isinstance(exc, (httpx.HTTPError, RuntimeError))


def _mask(key: str) -> str:
    if not key:
        return "(empty)"
    return key[:6] + "..." + key[-4:] if len(key) > 12 else "***"


def normalize(vec: list[float]) -> list[float]:
    """L2 归一化：LanceDB 用平方 L2 距离，向量归一化后 cos = 1 - d/2，可解释。"""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


class Embedder:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dim: int,
        cache_path: Path | str,
        batch_size: int = 16,
        timeout: float = 60.0,
        price_cny_per_mtok: float = 0.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.cache_path = Path(cache_path)
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.price_cny_per_mtok = price_cny_per_mtok

        self._cache: dict[str, list[float]] | None = None
        self.api_calls = 0
        self.cache_hits = 0
        self.tokens = 0
        self.request_ms = 0.0

    # -- 缓存 --------------------------------------------------
    @property
    def model_key(self) -> str:
        return "siliconflow:" + self.model

    def _key(self, text: str) -> str:
        return hashlib.sha256((self.model_key + "\n" + text).encode("utf-8")).hexdigest()

    def _load_cache(self) -> dict[str, list[float]]:
        if self._cache is not None:
            return self._cache
        cache: dict[str, list[float]] = {}
        if self.cache_path.exists():
            bad = 0
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    cache[row["k"]] = row["v"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    bad += 1
            if bad:
                logger.warning("embed_cache: 跳过 {} 行损坏记录 ({})", bad, self.cache_path)
        self._cache = cache
        return cache

    def _append_cache(self, items: list[tuple[str, list[float]]]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("a", encoding="utf-8") as fh:
            for key, vec in items:
                fh.write(json.dumps({"k": key, "m": self.model, "v": vec}, ensure_ascii=False) + "\n")
            fh.flush()

    @property
    def cache_size(self) -> int:
        return len(self._load_cache())

    # -- API ---------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception(_should_retry),
        reraise=True,
    )
    def _post_batch(self, texts: list[str]) -> tuple[list[list[float]], int]:
        if not self.api_key:
            raise FatalEmbeddingError("SF_API_KEY 未配置（ONCALL_ENV_FILE 指向的 env 文件里缺 SF_API_KEY）")
        payload = {"model": self.model, "input": texts}
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        started = time.perf_counter()
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.base_url + "/embeddings", json=payload, headers=headers)
        self.request_ms += (time.perf_counter() - started) * 1000
        if resp.status_code != 200:
            detail = resp.text[:300]
            # 4xx 里只有 429 值得重试，其余重试也是白费额度
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RuntimeError("embeddings HTTP {}: {}".format(resp.status_code, detail))
            raise FatalEmbeddingError(
                "embeddings HTTP {} (不重试，检查 key/模型名 {}): {}".format(
                    resp.status_code, _mask(self.api_key), detail
                )
            )
        body = resp.json()
        data = body.get("data") or []
        if len(data) != len(texts):
            raise RuntimeError("embeddings 返回条数不匹配: 请求 {} 条，返回 {} 条".format(len(texts), len(data)))
        ordered: list[list[float]] = [[] for _ in texts]
        for i, item in enumerate(data):
            idx = item.get("index", i)
            vec = item.get("embedding") or []
            if len(vec) != self.dim:
                raise RuntimeError("embedding 维度不符: 期望 {}，实际 {}".format(self.dim, len(vec)))
            ordered[idx] = normalize([float(x) for x in vec])
        tokens = int((body.get("usage") or {}).get("prompt_tokens", 0))
        self.api_calls += 1
        self.tokens += tokens
        return ordered, tokens

    # -- 对外接口 ----------------------------------------------
    def embed(self, texts: list[str], *, progress: bool = False) -> list[list[float]]:
        """批量取向量：先查缓存，缺失的按 batch 打 API，顺序与输入一致。"""
        cache = self._load_cache()
        keys = [self._key(t) for t in texts]

        # 同一批里的重复文本只请求一次
        pending: list[int] = []
        seen: set[str] = set()
        for i, key in enumerate(keys):
            if key in cache:
                self.cache_hits += 1
                continue
            if key in seen:
                continue
            seen.add(key)
            pending.append(i)

        total = len(pending)
        for start in range(0, total, self.batch_size):
            batch_idx = pending[start : start + self.batch_size]
            batch_texts = [texts[i] for i in batch_idx]
            vectors, _ = self._post_batch(batch_texts)
            fresh: list[tuple[str, list[float]]] = []
            for i, vec in zip(batch_idx, vectors):
                cache[keys[i]] = vec
                fresh.append((keys[i], vec))
            self._append_cache(fresh)
            if progress:
                done = min(start + self.batch_size, total)
                logger.info("embedding {}/{} (api_calls={})", done, total, self.api_calls)

        missing = [i for i, k in enumerate(keys) if len(cache.get(k, ())) != self.dim]
        if missing:
            raise RuntimeError("embedding 缺失 {} 条（缓存与返回值都对不上）".format(len(missing)))
        return [cache[k] for k in keys]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @property
    def cost_cny(self) -> float:
        return round(self.tokens / 1_000_000 * self.price_cny_per_mtok, 6)
