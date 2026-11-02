"""进程级共享的 httpx.Client：把 TLS 握手从每次请求里挪出去。

为什么值得单开一个模块：本项目的每一步都是 HTTP（embedding / rerank / LLM），而原来的
写法是"每次调用 with httpx.Client()"。单次 TLS 握手 + TCP 建连在硅基流动这边实测
150-300 ms，一次诊断要连三个服务，光握手就吃掉首 token 预算的一大块。改成共享连接池后
第二个请求起直接复用已建立的连接。

代价与边界：
- 客户端是长生命周期的，超时按调用点传（timeout=...），不在这里统一设死；
- 进程退出时关掉（main.py 的 lifespan 里 close_client()），否则 pytest 会留警告；
- 只做连接池，不做重试——重试语义各调用点自己管（tenacity / max_retry）。
"""

from __future__ import annotations

import threading

import httpx

_UA = "oncall-copilot/0.5 (+local)"

_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None


def get_client() -> httpx.Client:
    """返回进程级共享客户端（惰性创建，双检锁）。"""
    global _CLIENT
    if _CLIENT is None:
        with _LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.Client(
                    headers={"User-Agent": _UA},
                    limits=httpx.Limits(max_keepalive_connections=16, max_connections=32),
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
    return _CLIENT


def close_client() -> None:
    """关闭共享客户端（进程退出 / 测试收尾）。重复调用安全。"""
    global _CLIENT
    with _LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            finally:
                _CLIENT = None
