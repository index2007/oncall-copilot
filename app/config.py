"""全局配置：一个 Settings 对象，所有模块共享。

约定（清单 §13.4）：密钥不进项目目录。env 文件的查找顺序： 

1. 环境变量 ONCALL_ENV_FILE 指定的文件（推荐：项目外的 .secrets/oncall-copilot.env）
2. 项目根目录下的 .env（本地开发用，已在 .gitignore 里）
3. 上一级目录的 .secrets/oncall-copilot.env（本仓库的实际用法）

pydantic-settings 会把同名字段按大小写不敏感匹配到环境变量，所以
SF_API_KEY -> sf_api_key、EMBED_MODEL -> embed_model 都能直接用。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_files() -> tuple[str, ...]:
    candidates: list[str] = []
    explicit = os.environ.get("ONCALL_ENV_FILE", "").strip()
    if explicit:
        candidates.append(explicit)
    candidates.append(str(PROJECT_ROOT / ".env"))
    candidates.append(str(PROJECT_ROOT.parent / ".secrets" / "oncall-copilot.env"))
    return tuple(candidates)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- LLM（唯一计费项）--------------------------------------
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-flash"  # 该账号实际模型；deepseek-chat 只是别名
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # Query 理解的 LLM 兜底（只在规则没命中且 query 够长时触发）
    query_llm_enabled: bool = True
    query_llm_timeout_s: float = 15.0

    # -- embedding / rerank（HTTP，本机不装模型）---------------
    sf_api_key: str = ""
    sf_base_url: str = "https://api.siliconflow.cn/v1"
    embed_model: str = "BAAI/bge-m3"
    embed_dim: int = 1024
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # -- 检索参数 ----------------------------------------------
    top_k_vec: int = 20
    top_k_bm25: int = 20
    top_k_fuse: int = 20  # 融合后交给重排的条数（M5 -> M6）
    top_k_final: int = 5
    rrf_k: int = 60
    abstain_threshold: float = 0.35
    sub_index_filter: bool = True  # Query 理解命中子库时是否加元数据过滤

    # -- 生成（M7）---------------------------------------------
    llm_temperature: float = 0.2
    llm_max_tokens: int = 800
    llm_timeout_s: float = 60.0
    llm_max_retry: int = 1  # 设计清单 M7：JSON 解析失败重试 1 次
    # deepseek-flash 是推理模型（/v1/models 标了 effort low/high/max，默认 high）。请求体带
    # thinking={"type":"disabled"} 才不先"想"一遍；不关的话正文 delta 要到 6 s 后才出现。
    llm_thinking: str = "disabled"  # disabled / auto
    # 价格以官网为准（api-docs.deepseek.com/quick_start/pricing）；
    # 两项都为 0 时 cost_cny 恒为 0，只记 token 数
    llm_price_in_cny_per_mtok: float = 2.0
    llm_price_out_cny_per_mtok: float = 8.0

    # -- 重排（M6）---------------------------------------------
    rerank_timeout_s: float = 30.0
    rerank_max_docs: int = 20  # 单次 rerank 请求最多带多少条候选
    rerank_price_cny_per_mtok: float = 0.0  # 硅基流动当前免费，非 0 才计入成本

    # -- 路径 --------------------------------------------------
    # -- API / 可观测 -------------------------------------------------
    log_level: str = "INFO"
    log_file: str = ""  # 空 = 只写控制台；填路径则额外落文件
    metrics_window: int = 200  # /v1/metrics/summary 默认统计最近多少次请求

    data_dir: str = "./data"
    content_dir: str = "./content"
    table_name: str = "chunks"

    # -- 索引器 ------------------------------------------------
    embed_batch: int = 16
    embed_timeout_s: float = 60.0
    # 硅基流动 bge-m3 当前免费；配成非 0 才会算进 IndexReport.cost_cny
    embed_price_cny_per_mtok: float = 0.0

    @property
    def data_path(self) -> Path:
        return self._abs(self.data_dir)

    @property
    def content_path(self) -> Path:
        return self._abs(self.content_dir)

    @property
    def lancedb_path(self) -> Path:
        return self.data_path / "lancedb"

    @property
    def bm25_path(self) -> Path:
        return self.data_path / "bm25.pkl"

    @property
    def sqlite_path(self) -> Path:
        return self.data_path / "oncall.db"

    @property
    def embed_cache_path(self) -> Path:
        return self.data_path / "embed_cache.jsonl"

    @property
    def embed_model_key(self) -> str:
        """缓存 key 的前缀：换模型（或换维度）旧缓存自动失效。"""
        return "siliconflow:" + self.embed_model

    @staticmethod
    def _abs(p: str | Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """测试用：清掉 lru_cache，让新 env 生效。"""
    get_settings.cache_clear()
