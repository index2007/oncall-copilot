# On-call Copilot · 运维故障诊断助手

> 把「线上出事了」这句求助，变成一张可机读、带引用的结构化诊断卡。

**状态**：开发中（D1 完成 / D2 起未开工）。本 README 的指标表在 D7 实测前一律留空，不填预计值。

```
报错文本 / Alertmanager 告警
        │
        ├─ Query 理解（错误码正则 + 资源类型词表 + 事件原因词表；未命中且 query>20 字才调 LLM）
        │
        ├─ 双路召回：LanceDB 向量 Top20  │  BM25（jieba 分词）Top20
        │        └──── RRF 融合（k=60，只用排名不用分数）────┐
        │                                                    │
        ├─ Cross-Encoder 重排（bge-reranker-v2-m3）→ Top5 上下文
        │        └─ Top1 < ABSTAIN_THRESHOLD → 直接拒答，不进 LLM
        │
        ├─ LLM 生成结构化诊断卡（Pydantic v2 校验，JSON mode）
        │        └─ 引用后校验：chunk_id 必须出现在本次上下文，否则丢弃重生成一次 → 再失败降级原文片段
        │
        └─ 输出：诊断卡（causes / verify_commands / actions / citations / latency / cost）
```

---

## 1. 为什么是它

线上故障处置的真实困境不是"没有文档"，而是**文档太多、检索太慢、答案不能直接用**：

| 现状 | 本项目的处理 |
|---|---|
| 关键词搜 K8s 文档，返回一堆概念页 | Query 理解先把"OOMKilled + Pod 反复重启"路由成资源类型 + 事件原因 |
| 单路向量召回漏掉 `OOMKilled` 这类精确 token | 向量 + BM25（jieba 分词保留英文 token）双路召回，RRF 融合 |
| 大模型答得像真的，但给不出出处 | 输出强制绑定 `chunk_id`，生成后再校验一遍，不在上下文就丢弃 |
| 语料里根本没有这条故障，模型硬编 | 双保险拒答：重排 Top1 低于阈值直接拒 + 引用全落空也拒 |
| 首字等 3 秒以上，值班的人已经去翻别的了 | SSE 先把 `meta`/`citations` 推出去，正文流式到达（见 §4） |

**范围**：公开运维文档（K8s / Nginx / Redis / MySQL）；**不做**：私有工单接入、自动修复、权限体系。

---

## 2. 技术栈（全部可复现，无 Docker、无外部数据库）

| 层 | 选择 | 用它的理由 |
|---|---|---|
| Web | FastAPI + uvicorn | 异步、自带 OpenAPI、SSE 友好 |
| 元数据 | SQLite | 零安装、零运维 |
| 向量库 | LanceDB（嵌入式，磁盘持久化） | 无需服务进程，Windows 友好 |
| 关键词 | `rank_bm25` + `jieba` | 中文不分词等于没做 BM25（`OOMKilled` 会被切碎） |
| Embedding | 硅基流动 `BAAI/bge-m3`（1024 维，免费） | 中英混排强，本机不装模型 |
| 重排 | 硅基流动 `BAAI/bge-reranker-v2-m3`（免费） | 同系列 cross-encoder，免 1.1 GB 本地权重 |
| LLM | DeepSeek `deepseek-flash` | 中文技术语义强、JSON mode 稳；唯一计费项 |
| 流式 | `sse-starlette` | 比手写 `StreamingResponse` 稳 |
| 配置 | pydantic-settings + `.env` | 密钥不进代码 |
| 日志 | loguru + SQLite 表 | 请求级可观测（`query_log`） |
| 测试 | pytest | 检索层必须有测试 |

> **不用**：LangChain / LlamaIndex（黑盒、层层封装、面试讲不清每一层）；本地模型（本机不装 torch / onnxruntime，全走 HTTP）。
> **模型名注意**：实测该账号下 `deepseek-chat` 是 `deepseek-flash` 的别名（响应 `model` 字段回 flash），因此配置里显式写 flash。

---

## 3. 关键设计决策

| # | 决策 | 理由 | 代价 / 已知限制 |
|---|---|---|---|
| 1 | 向量 + BM25 各取 Top20 再做 RRF，不混用分数 | 余弦相似度与 BM25 分数量纲不可比，只信排名 | 两路都要维护，索引成本翻倍 |
| 2 | RRF `score = Σ 1/(k + rank_i)`，k=60 | 免调权、对单路噪声鲁棒 | 极端情况下两路都弱时无法区分强弱 |
| 3 | 诊断卡是 Pydantic 模型，不是自由文本 | 可机读、可回写工单、可校验 | JSON 生成失败需要重试 1 次再降级纯文本 |
| 4 | 每条结论标 `[chunk_id]`，生成后**再验一遍** | Prompt 约束不保证模型不编，必须后置校验 | 校验不通过要多花一次 LLM 调用 |
| 5 | 拒答双保险（重排阈值 + 引用全落空） | 宁可说"没查到"，不能瞎编处置步骤 | 阈值定高了误拒率上升，必须同时报两个数 |
| 6 | Query 理解规则优先，未命中才调 LLM | 领域词表封闭，规则零成本、零延迟、可解释 | 超出词表的新故障类型会漏 |
| 7 | 索引与查询分离，`doc_hash` 增量，删除标 `is_deleted` | 索引可重跑、幂等、支持替换文档 | 软删要定期压实 |
| 8 | `VectorStore` 抽象接口 | 生产可换 pgvector / ES，而不改上层 | 多一层抽象，单人项目里要克制别过度设计 |

---

## 4. 性能：实测数据与验收口径

**先说结论**：带 LLM 生成的架构下，"P95 ≤2.0s"和"整卡 ≤4s"都不可达（本机不许跑模型，且输出长度由模型自己决定），因此把延迟拆成两段验收：首 token 是主口径，整卡按 P50/P95 报，并如实写在这里。

| 链路 | 实测 | 条件 |
|---|---|---|
| `bge-m3` embedding（2 条） | 303 ms | 硅基流动 `/v1/embeddings`，dim=1024，L2=1.000 |
| `bge-reranker-v2-m3`（3 条候选） | 200 ms | 相关 0.5587 / 无关 0.0000 |
| `deepseek-flash` 生成 400 tokens | 2702 / 3255 / 3286 ms | 约 130 token/s，连跑三次 |

| 验收项 | 门槛 | 现状 |
|---|---|---|
| SSE 首 token | ≤1.2 s | 未实现（D5） |
| 整卡（含检索 + 重排 + 生成） | P50 ≤4.5 s、P95 ≤8.0 s | 36 次实测（四批 `--repeat 3`）：**P50 约 4.2 s，P95 8003 ms，最慢 8306 ms，2/36 次贴线越 8 s**；P50 稳定达标、P95 贴线未过，见 §5.3 |
| 分阶段 P50 / P95 | 只采集，不设门槛 | `/v1/metrics/summary` 按段输出（Query 理解 / 检索 / 重排 / 生成） |
| 单次成本 | ≤¥0.02 | 实测 ¥0.0046-0.0129/次（9 次均值 ¥0.0074） |

**首字怎么做到 1.2 s**：SSE 帧序固定为 `meta`（Query 理解）→ `citations`（已确定的 chunk）→ `delta`（正文增量）→ `done`（`latency_ms`/`ttft_ms`/`cost_cny`）。首字不必等 LLM——检索与重排结束即可开流；拒答路径更早，<1 s 返回完整卡片。

---

## 5. 语料

| 来源 | 篇数 | 正文字数 | 说明 |
|---|---|---|---|
| Kubernetes 官方文档（`/zh-cn/docs`，概念/任务/生产环境） | 113 | 772,706 | 主力语料 |
| Redis 官方文档（operate / reference） | 59 | 640,018 | 含持久化、内存、集群 |
| Nginx 官方文档 | 39 | 431,183 | `/en/docs` |
| MySQL 8.0 参考手册 | 44 | 304,129 | 中英混排 |
| **合计** | **255** | **2,148,036** | 磁盘约 3.4 MB，`content/` 已 gitignore |

抓取脚本 `scripts/fetch_corpus.py`：sitemap / 索引页发现 → 关键词优先级排序截断配额 → 1 req/s 节流 + 显式 UA + robots 检查 → bs4 预剪 nav/侧栏/页脚 → trafilatura 转 Markdown（保留 fenced code）→ 落 `content/<source>/<slug>.md` 带 YAML front matter + 同级 `index.jsonl`。按 `doc_hash` 去重、已存在跳过，可断点续跑；`--only` 重抓指定页，`--force` 重抓时 `index.jsonl` 按 slug 覆盖写（不再堆重复行）。

> **缺口（如实写）**：阿里云 ACK 文档站是客户端渲染，抓不到正文（拿到的只是 43 KB 页面骨架），ACK 的 20-30 篇配额用 K8s 语料上沿补齐。要补需要给定可直接下载的 URL 列表。

### 5.1 索引结果（M3 实测）

| 项 | 数值 |
|---|---|
| 文档 / chunk | 255 篇 / 2099 块（8.2 块/篇，最长 794 tokens，0 个超限块） |
| chunk 分布 | k8s 1246 / redis 540 / nginx 179 / mysql 134 |
| 全量重建 | 98 s（125 次 `/embeddings`、72.8 万 tokens、¥0；小样本跑过一遍后 87 次命中缓存） |
| 二次运行 | `added=0 updated=0 deleted=0`、0 次 API、0.2 s |
| 改一篇 | `updated=1 skipped=254`，只重算 1 块、1 次 API；改回原文后 0 次 API（缓存命中） |
| 删一篇再放回 | `deleted=1` 三方存储同步降到 2098；放回后 `added=1` 回到 2099 |
| 存储占用 | LanceDB 11.5 MB / `bm25.pkl` 6.4 MB（含 token 缓存）/ SQLite 168 KB / embedding 缓存 47 MB |

chunk 数 2099，落在设计清单 v1.4 的目标区间 1800-2200 内（v1.3 及更早写的是 2500-3500，已按实测修正）。不能按「token 总量 ÷ 800」推算——那是 917 的理论下限；实际块数由标题密度决定，k8s 是 H2/H3 密集型文档，平均块只有 349 tokens（上限 800）。要更细的粒度把 `chunker.MAX_TOKENS` 调到 600 再重建，块数约 2600-2800。

---

### 5.2 检索层实测（M4 Query 理解 + M5 双路召回）

`python scripts/eval_retrieval_probe.py` 一把跑完；样本在 `eval/probe_query_understanding.jsonl`（50 条报错文本）与 `eval/probe_recall.jsonl`（12 条）。

| 指标 | 门槛 | 实测 | 说明 |
|---|---|---|---|
| resource_type 准确率（纯规则） | ≥0.90 | **1.000**（50/50） | 不调 LLM |
| resource_type 准确率（允许调 LLM） | ≥0.90 | **0.980**（49/50） | 全程只有 1 条触发 LLM |
| need_llm 占比 | <20% | **2.0%**（1/50） | 规则无信号且 query > 20 字才置位 |
| recall@20（子库过滤，默认路径） | ≥0.95 | **1.000**（12/12） | 平均覆盖 0.901，Top1 命中 0.833 |
| recall@20（不过滤，fallback 路径） | ≥0.95 | **0.917**（11/12） | 唯一漏的是 `Redis connection refused` |

- 检索热路径 P50 14 ms / P95 17 ms（embedding 命中缓存后，只剩 LanceDB 检索 + BM25 + RRF）；冷启动第一次查询 0.8 s（建连、开 LanceDB、首次 embedding），单列不混进 P50。
- 查询文本 = 原始 query + 错误码 + 强事件词（`OOMKilled` / `FailedScheduling` 这类），弱事件词（`error` / `pending`）不拼，避免把噪声塞进向量查询。
- **这张表是乐观估计，别按 1.000 讲**：50 条报错文本是我照语料自拟的，不是线上工单；`extract_resource` 的"取最先出现的资源词"规则，是在看到 2 条错判后改的（`worker 节点变成 NodeNotReady，上面跑的容器全挂了` 曾被判成 container、`DaemonSet 没有在每个节点上被创建` 曾被判成 node）。没留独立测试集，真实分布下按 0.90-0.95 预期。
- 开 LLM 后那条判错的样本是 `这台机器负载很高，负载值一直在 30 以上`：模型给出 `node`，标注里算"无资源"，属标注口径差异，不是模型答错。
- 子库过滤不是锦上添花：`Redis 连接被拒绝 connection refused` 在不过滤时 Top-20 里一条 redis 文档都没有，过滤后才命中。命中不了时 `retrieve()` 会补跑不限源两路（`stats.fallback=true`），所以过滤只影响排序起点，不影响召回底线。

---

### 5.3 生成层与端到端实测（M6/M7/M8 + D4）

`python scripts/d4_e2e.py --repeat 3` 一把跑完（3 条样例 × 3 次，真实 API，不是 mock）。样例是 k8s `OOMKilled/exit 137`、nginx `502 upstream prematurely closed`、MySQL `1213 deadlock`。

| # | 样例 | 拒答 | causes | 整卡 | 其中生成 | 输出 tokens | 单次成本 |
|---|---|---|---|---|---|---|---|
| 1 | k8s | 否 | 2 | 4826 ms | 3141 ms | 472 | ¥0.00698 |
| 2 | k8s | 否 | 2 | 4754 ms | 4270 ms | 662 | ¥0.00850 |
| 3 | k8s | 否 | 2 | 3189 ms | 2696 ms | 434 | ¥0.00668 |
| 4 | nginx | 是 | 0 | 2825 ms | 2031 ms | 243 | ¥0.00456 |
| 5 | nginx | 是 | 0 | 7089 ms | 6477 ms | 906 | ¥0.01286 |
| 6 | nginx | 是 | 0 | 3033 ms | 2397 ms | 256 | ¥0.00467 |
| 7 | MySQL | 否 | 2 | 4611 ms | 3959 ms | 665 | ¥0.00778 |
| 8 | MySQL | 否 | 2 | 4314 ms | 3704 ms | 616 | ¥0.00739 |
| 9 | MySQL | 否 | 2 | 4480 ms | 3812 ms | 596 | ¥0.00723 |

**首批（A 批）9 次：整卡 P50 4480 ms / P95 7089 ms / worst 7089 ms，合计 ¥0.0666。** 对原来那句「整卡 ≤4.0 s」是不达标——口径为什么放宽，写在下面，不美化：

另三批复跑（同脚本 `--repeat 3`，每批 9 次）：

| 批次 | P50 | P95 | worst | 花费 |
|---|---|---|---|---|
| A（上表） | 4480 ms | 7089 ms | 7089 ms | ¥0.0666 |
| B | 4346 ms | 8003 ms | 8003 ms | ¥0.0678 |
| C | 4175 ms | 8306 ms | 8306 ms | ¥0.0648 |
| D（逐次：5504 / 2782 / 3336 / 2752 / 3355 / 4463 / 4494 / 3499 / 4040 ms） | 3499 ms | 5504 ms | 5504 ms | ¥0.0589 |

四批 36 次合并：**P50 约 4.2 s、P95 8003 ms、worst 8306 ms，2/36 次贴线越 8 s（8003、8306）**。按 v1.5 门槛算，P50 稳定达标、P95 贴线未过：越线的两次都是 MySQL 样例模型把 `verify_commands` 写满（1074-1079 tokens，折合约 8.3 s）。降 P95 只有两条路——更硬的输出裁剪，或改流式交付（D5 的首 token 口径）；本批次不动代码硬凑。

**但 P95 不是稳定的数**：整卡 ≈ 输出 token ÷ 130 tok/s + 0.8 s，而同一 prompt 下模型输出在 243-906 tokens 之间摆动，输出写到 900+ tokens 就是 7 s+，写到 1000+ tokens 必然破 8 s（本机无法控制）。所以别把「P95 达标」当成可承诺的 SLA，可承诺的只有 D5 的首 token ≤1.2 s。

- 延迟由输出 token 数决定，不由检索决定：生成占总耗时 72%-91%，deepseek-flash 实测约 130 tok/s（443 tok ≈ 3.4 s、906 tok ≈ 7.0 s）；检索 250 ms + 重排 498 ms + 校验 <10 ms 合计不到 0.8 s，没有多少可压。
- 已压缩两轮：上下文预算 6000→2600 字符（最多 5 块）；prompt 与生成侧双写死上限（summary ≤80 字、causes ≤2、explanation ≤50 字、verify_commands ≤3、actions ≤3 条且每条 ≤45 字）；`finish_reason=length` 走"更短重试"。第一轮（972 completion tokens）实测 8274 / 8129 / 14028 ms，第二轮降到上表的 P50 4480 ms。
- 剩余方差来自模型自己写多长：prompt 写了"≤420 tokens"，实测仍有 906 tokens 的输出。生成侧硬截断只能裁字段，裁不掉已经花掉的 token，所以截断不省时间。
- 重排候选数不是瓶颈：20 条 498 ms / 12 条 365 ms / 8 条 376 ms（固定开销约 300 ms），砍候选只省 0.13 s，不值得牺牲排序质量。
- 9 次里 0 次 JSON 解析失败、0 次重试；引用校验全部 ok；nginx 3 次全由生成侧 abstain（语料只有官方模块参考页，没有 502/504 排障页，见 §9 第 9 条），不是链路故障。

---

## 6. 快速开始

```bash
# 0. 前提：Python 3.12（本机用 python-build-standalone 便携版，venv 已建在 oncall-copilot/.venv）
# 1. 依赖
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 配置（密钥放项目目录之外，避免误提交）
export ONCALL_ENV_FILE=<workspace>/.secrets/oncall-copilot.env
cp .env.example .env            # 仅作模板参考，真实值读 ONCALL_ENV_FILE

# 3. 冒烟三个外部服务（embedding / rerank / LLM）
.venv/Scripts/python.exe scripts/smoke_api.py

# 4. 抓语料（已抓完，重跑会跳过已有文件）
.venv/Scripts/python.exe scripts/fetch_corpus.py --source all

# 5. 建索引（--full 首次全量，之后不加 --full 就是增量；--dry-run 先看要做什么）
.venv/Scripts/python.exe -m app.index.indexer --full
.venv/Scripts/python.exe -m app.index.indexer --stats
.venv/Scripts/python.exe -m app.index.indexer --search "Pod OOMKilled exit code 137"

# 6. 起服务
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
# → http://127.0.0.1:8000/docs
```

---

## 7. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/ask` | 通用问答，`stream=true` 走 SSE |
| POST | `/v1/diagnose` | 结构化诊断，返回 `DiagnosisCard` |
| POST | `/webhook/alertmanager` | Alertmanager v4 payload 兼容，内部转 `/v1/diagnose` |
| GET | `/v1/sources/{chunk_id}` | 原文片段 + url + heading_path |
| POST | `/v1/admin/index` | 触发增量索引 |
| GET | `/v1/metrics/summary` | TTFT / 整卡 P50-P95、拒答率、平均成本 |
| GET | `/v1/health` | 存活 + 索引版本 |

---

## 8. 评测

评测集：40 条正样本 + 10 条负样本，一条命令出 Markdown 报告。

| 指标 | 定义 | 门槛 | 实测 |
|---|---|---|---|
| Top-1 根因命中 | `causes[0].title` 与 `gold_root_cause` 语义一致（LLM-as-judge + 人工抽检 20%） | ≥0.70 | 待 D7 |
| Top-3 根因命中 | `causes[0..2]` 任一命中 | ≥0.88 | 待 D7 |
| 引用命中率 | 引用中至少一条 url ∈ `gold_doc_urls` | ≥0.90 | 待 D7 |
| 拒答准确率 | 负样本中正确拒答的比例 | ≥0.85 | 待 D7 |
| 误拒率 | 正样本被错误拒答的比例（必须与拒答率一起看） | 待标定 | 待 D7 |
| 首字 / 整卡 | 见 §4 | 1.2 s / P50 4.5 s·P95 8.0 s | 整卡已测（§5.3）；首字待 D5 |
| 单次成本 | prompt + completion token 折算 | ≤¥0.02 | 待 D7 |

### 8.1 基线对比（占位，D7 填满后这张表就是核心资产）

| 变体 | Top-1 | Top-3 | recall@20 | 引用命中 | 误拒率 | 首字 P50 (ms) | 整卡 P50 (ms) | 整卡 P95 (ms) | 成本/次 |
|---|---|---|---|---|---|---|---|---|---|
| BM25 only | | | | | | | | | |
| 向量 only | | | | | | | | | |
| 混合 + RRF | | | | | | | | | |
| + Rerank | | | | | | | | | |
| + Query 理解（full） | | | | | | | | | |

---

## 9. 已知缺陷与限制

1. **语料不含 ACK**：阿里云 ACK 文档客户端渲染，抓不到正文；K8s 为主的上限在此。
2. **默认单机、单进程**：SQLite + LanceDB 嵌入式，不适合多副本并发写；生产化需换 `VectorStore` 实现（pgvector / ES）+ 独立索引 worker。
3. **整卡压不进 4 s，P95 还贴线越线**：四批 36 次真实调用 P50 约 4.2 s、P95 8003 ms、最慢 8306 ms（§5.3），按 v1.5 口径（P50 ≤4.5 s、P95 ≤8 s）是 P50 达标、**P95 未过**（2/36 越线）。根因是输出 token 数：整卡 ≈ 输出 tokens ÷ 130 + 0.8 s，模型某次把 `verify_commands` 写满（1074 tokens）就必然破 8 s，本机控制不了；检索与重排合计不到 0.8 s，没有多少可压空间。**不承诺 2 s，也不承诺 4 s**；真正的体验保障是 D5 的 SSE 首 token ≤1.2 s。
4. **重排阈值需实测标定**：`bge-reranker-v2-m3` 输出区间 0-1，无关内容压到 0.0000、真相关仅 0.5587，`ABSTAIN_THRESHOLD=0.35` 只是初值，D7 按真实语料画 ROC 再定。
5. **`index.jsonl` 有 7 行历史重复**（k8s 1 / nginx 3 / mysql 3，续抓累积）：114/42/47 行 vs 113/39/44 个 `.md`；索引器只扫 `.md`，不受影响，新的 `--force` 已按 slug 覆盖写，不会再累积。
6. **没有鉴权**：接口对本地开放，网络暴露前必须加 token。
7. **LLM 生成失败降级**：JSON 解析失败或 `finish_reason=length` 重试 1 次（长度截断时追加"更短"指令），再失败降级为纯文本片段（`degraded=true`），此时没有结构化字段。重试会让整卡翻倍，是尾延迟的主要来源。
8. **`embed_cache.jsonl` 是明文 JSONL**（47 MB / 2081 条，浮点全精度）：换 float32 二进制能压到 1/4，但重跑索引省下的 API 调用远比磁盘值钱，暂不做。
9. **nginx 语料只有官方模块参考页**：没有 502/504 排障专页，nginx 类问题 Top-1 常落在 `ngx_http_upstream_module` 的参数说明上——召回对、根因浅，D7 评测时要单独看 nginx 子集。
10. **Redis 语料有版本变体近似重复**（`operate-rs-7.22-*` / `7.8-*` / `7.4-*` 同一篇按版本重复收录）：`doc_hash` 精确去重识别不了，会抬升同质块竞争，D7 前按 URL 去版本段或模糊去重。

---

## 10. 目录结构

```
oncall-copilot/
├── app/
│   ├── api/          # ask / diagnose / webhook / sources / admin
│   ├── core/         # query_understanding / retriever / reranker / generator / guard / schemas
│   ├── index/        # chunker / embedder / vector_store / bm25_store / indexer
│   └── obs/          # db / logging
├── content/          # 255 篇抓取的 Markdown（gitignore）
├── data/             # lancedb / bm25.pkl / oncall.db / embed_cache.jsonl（gitignore）
├── eval/             # dataset.jsonl / run_eval.py / report.md + 探针样本 probe_*.jsonl
├── scripts/          # fetch_corpus.py / smoke_api.py / eval_retrieval_probe.py / simulate_alert.py / demo.py
├── tests/
├── .env.example
├── requirements.txt
└── README.md
```

---

## 11. 开发进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| D1 | 环境 + 抓取脚本 + 语料 | ✅ 完成（255 篇 / 214.8 万字） |
| D2 | 切分 + 索引（LanceDB / BM25） | ✅ 完成（2099 块；全量 98 s、二次运行 0 API / 0.2 s） |
| D3 | Query 理解 + 双路召回 + RRF | ✅ 完成（50 条探针：规则 1.000 / need_llm 2%；recall@20 1.000；35 个单测） |
| D4 | 重排 + 生成 + 引用校验 / 拒答 | ✅ 完成（142 个单测；端到端 36 次真实调用：P50 约 4.2 s、P95 8003 ms、最慢 8306 ms、¥0.0069/次；P50 达标、P95 贴线未过，见 §5.3 与 §9 第 3 条） |
| D5 | API 全量（SSE / webhook / admin）+ 日志 | 未开工 |
| D6 | 评测集 + 评测脚本 | 未开工 |
| D7 | 5 变体基线 + 调参（唯一止损点） | 未开工 |
| D8 | 降级路径测试 + pytest | 未开工 |
| D9-D10 | 演示脚本 + 录屏 + README 定稿 | 未开工 |

设计文档：[技术路线与设计清单.md](../技术路线与设计清单.md)（v1.4）

---

## 12. 一句话讲给面试官

> 自建 255 篇公开运维文档的检索语料，做了一条"报错文本 → Query 理解 → 向量+BM25 双路召回 → RRF → Cross-Encoder 重排 → 带引用结构化诊断卡"的链路；重点不在接模型，而在**引用可校验、不知道就拒答、首字靠流式架构而非堆硬件**。

（指标数字在 D7 实测后回填到 §8，不提前写。）
