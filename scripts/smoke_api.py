#!/usr/bin/env python
"""密钥冒烟：验证 bge-m3 / bge-reranker-v2-m3 / deepseek-chat 三个外部调用可用。

密钥不落在项目目录里：
    ONCALL_ENV_FILE 指定 env 文件路径（默认 ../.secrets/oncall-copilot.env）

用法：
    python scripts/smoke_api.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = ROOT.parent / ".secrets" / "oncall-copilot.env"


def load_env(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"找不到 env 文件: {path}\n用 ONCALL_ENV_FILE 指定路径")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def mask(s: str) -> str:
    return s[:6] + "***" + s[-4:] if len(s) > 12 else "***"


def head(title: str) -> None:
    print(f"\n{'─' * 60}\n{title}\n{'─' * 60}")


def check_embed() -> bool:
    base = os.environ["SF_BASE_URL"].rstrip("/")
    model = os.environ["EMBED_MODEL"]
    dim = int(os.environ.get("EMBED_DIM", "1024"))
    texts = [
        "Kubernetes Pod 一直处于 Pending 状态，事件显示 0/3 nodes are available: 3 Insufficient memory。",
        "nginx worker 进程内存持续上涨最终被 OOMKilled，error.log 出现 worker process exited on signal 9。",
    ]
    t0 = time.perf_counter()
    r = httpx.post(
        f"{base}/embeddings",
        headers={"Authorization": f"Bearer {os.environ['SF_API_KEY']}", "Content-Type": "application/json"},
        json={"model": model, "input": texts},
        timeout=60.0,
    )
    dt = (time.perf_counter() - t0) * 1000
    print(f"POST {base}/embeddings  -> HTTP {r.status_code}  {dt:.0f} ms")
    if r.status_code != 200:
        print(r.text[:400])
        return False
    data = r.json()["data"]
    vec = data[0]["embedding"]
    norm = sum(x * x for x in vec) ** 0.5
    ok = len(vec) == dim and len(data) == len(texts)
    print(f"  model={model} n={len(data)} dim={len(vec)} (期望 {dim}) L2={norm:.3f}  {'OK' if ok else 'MISMATCH'}")
    return ok


def check_rerank() -> bool:
    base = os.environ["SF_BASE_URL"].rstrip("/")
    model = os.environ["RERANK_MODEL"]
    query = "Pod 报 OOMKilled 的原因"
    docs = [
        "容器内存使用超过 limits 时被内核 OOM Killer 杀掉，kubelet 将容器状态记为 OOMKilled。",
        "本文介绍如何用 nginx 配置 HTTPS 证书与 TLS 版本。",
        "节点的 ephemeral-storage 不足会触发 kubelet 的驱逐策略。",
    ]
    t0 = time.perf_counter()
    r = httpx.post(
        f"{base}/rerank",
        headers={"Authorization": f"Bearer {os.environ['SF_API_KEY']}", "Content-Type": "application/json"},
        json={"model": model, "query": query, "documents": docs, "top_n": 3, "return_documents": False},
        timeout=60.0,
    )
    dt = (time.perf_counter() - t0) * 1000
    print(f"POST {base}/rerank  -> HTTP {r.status_code}  {dt:.0f} ms")
    if r.status_code != 200:
        print(r.text[:400])
        return False
    res = r.json()["results"]
    scores = [x["relevance_score"] for x in res]
    top = max(res, key=lambda x: x["relevance_score"])
    print(f"  model={model} n={len(res)} top_score={top['relevance_score']:.4f} idx={top['index']}")
    print(f"  分数分布 min={min(scores):.4f} max={max(scores):.4f} mean={statistics.mean(scores):.4f}")
    print(f"  Top1 是否为相关文档(应 idx=0): {'OK' if top['index'] == 0 else 'WRONG'}")
    print(f"  提示: ABSTAIN_THRESHOLD=0.35 的阈值需要按这个分数量纲标定（当前最高分 {max(scores):.3f}）")
    return top["index"] == 0


def check_chat() -> bool:
    base = os.environ["DEEPSEEK_BASE_URL"].rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    system = (
        "你是 Kubernetes 运维专家。输出严格的 JSON，字段："
        '{"summary": str, "root_cause": str, "confidence": number, "evidence": [str], "next_steps": [str], "citations": [str]}'
    )
    user = (
        "报错：Pod 反复重启，kubectl describe 显示 Last State: Terminated, Reason: OOMKilled, Exit Code: 137。"
        "上下文片段 id: k8s-0001, k8s-0007。"
    )
    t0 = time.perf_counter()
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 600,
        },
        timeout=90.0,
    )
    dt = (time.perf_counter() - t0) * 1000
    print(f"POST {base}/chat/completions  -> HTTP {r.status_code}  {dt:.0f} ms")
    if r.status_code != 200:
        print(r.text[:400])
        return False
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    print(f"  model={body.get('model')} tokens: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")
    try:
        card = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"  JSON 解析失败: {exc}\n  原文: {content[:200]}")
        return False
    need = {"summary", "root_cause", "confidence", "evidence", "next_steps", "citations"}
    missing = need - set(card)
    print(f"  字段齐全: {'OK' if not missing else '缺 ' + str(missing)}")
    print(f"  root_cause: {str(card.get('root_cause'))[:100]}")
    print(f"  confidence: {card.get('confidence')}  citations: {card.get('citations')}")
    return not missing


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    env_path = Path(os.environ.get("ONCALL_ENV_FILE") or DEFAULT_ENV).expanduser()
    load_env(env_path)
    print(f"env: {env_path}")
    print(f"SF_API_KEY      = {mask(os.environ.get('SF_API_KEY', ''))}")
    print(f"DEEPSEEK_API_KEY= {mask(os.environ.get('DEEPSEEK_API_KEY', ''))}")

    results: dict[str, bool] = {}
    head("A. SiliconFlow embeddings (BAAI/bge-m3)")
    results["embed"] = check_embed()
    head("B. SiliconFlow rerank (BAAI/bge-reranker-v2-m3)")
    results["rerank"] = check_rerank()
    head("C. DeepSeek chat (JSON mode)")
    results["chat"] = check_chat()

    head("结果")
    for k, v in results.items():
        print(f"  {k:8s} {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
