"""FastAPI 依赖：配置、SQLite、诊断器单例。

为什么诊断器要单例并且惰性构造：构造它要开 LanceDB、反序列化 BM25 索引
（显存/内存常驻），每次请求都建一遍会让 P50 直接多出 0.8 s 冷启动。这里用
"首次请求时才建 + 双检锁" 换启动速度，单测用 reset_diagnoser() 或
app.dependency_overrides 换成假实现，不必起真索引。
"""

from __future__ import annotations

import threading

from app.config import Settings, get_settings
from app.core.pipeline import Diagnoser
from app.obs.db import DB

_DIAGNOSER: Diagnoser | None = None
_DIAGNOSER_LOCK = threading.Lock()


def get_app_settings() -> Settings:
    return get_settings()


def get_db() -> DB:
    """每次调用给一个新 DB 句柄：连接在方法内开闭，线程安全靠 SQLite 自身。"""
    return DB(get_settings().data_path / "oncall.db")


def get_diagnoser() -> Diagnoser:
    global _DIAGNOSER
    if _DIAGNOSER is None:
        with _DIAGNOSER_LOCK:
            if _DIAGNOSER is None:
                _DIAGNOSER = Diagnoser.from_settings(get_settings())
    return _DIAGNOSER


def reset_diagnoser() -> None:
    """测试用：清掉单例，让下一次 get_diagnoser() 重新构造。"""
    global _DIAGNOSER
    with _DIAGNOSER_LOCK:
        _DIAGNOSER = None
