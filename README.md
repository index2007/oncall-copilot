# On-call Copilot · 运维故障诊断助手

> 把「线上出事了」这句求助，变成一张可机读、带引用的结构化诊断卡。

**状态**：D7 评测完成——full 档 Top-1 **80.0%**（门槛 0.70）、拒答准确 90.0%、整卡 P50 2626 ms / P95 3784 ms、¥0.0051/次；Top-3 82.5% 与引用命中 84.8% 未过线，归因见 §9 第 14-17 条。D8-D10 未开工。指标数字一律来自可重跑的脚本（`eval/report_s6a.md`、`data/bench_stream.json`），不填预计值。

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

**先说结论**：延迟的大头不是检索，是模型写多长、以及它有没有在"想"。`deepseek-flash` 默认带思考（`effort=high`，`/v1/models` 里可查），思考 token 既慢又不可控；把它关掉（`LLM_THINKING=disabled`）后，同一套链路整卡从 P50 约 4.2 s 降到 2.3 s。下表是**关闭思考之后**的 9 次流式实测（`scripts/bench_stream.py`，真实 API）。

| 链路 | 实测 | 条件 |
|---|---|---|
| `bge-m3` embedding（2 条） | 303 ms | 硅基流动 `/v1/embeddings`，dim=1024，L2=1.000 |
| `bge-reranker-v2-m3`（3 条候选） | 200 ms | 相关 0.5587 / 无关 0.0000 |
| `bge-reranker-v2-m3`（20 条候选，真链路） | 330-534 ms | 9 次均值 422 ms，固定开销约 300 ms |
| `deepseek-flash` 首 content token | 约 400-600 ms | 关思考；开思考时 6 s+（先吐一堆 `reasoning_content`） |
| `deepseek-flash` 正文输出 | 约 130 token/s | 296-329 tokens 的卡片 ≈ 1.2-1.7 s |

| 验收项 | 门槛 | 现状 |
|---|---|---|
| SSE 首 token | ≤1.2 s | 9 次流式实测：**P50 1177 ms、P95 1349 ms、最慢 1349 ms**；8 次有正文的里 3 次略超（1207 / 1210 / 1349 ms） |
| 整卡（含检索 + 重排 + 生成） | P50 ≤4.5 s、P95 ≤8.0 s | 9 次：**P50 2324 ms、P95 2817 ms、最慢 2817 ms，0/9 越 4 s**；按 v1.5 口径（P50 ≤4.5 / P95 ≤8.0）是已过 |
| 分阶段 P50 / P95 | 只采集，不设门槛 | `/v1/metrics/summary`；9 次均值：Query 理解 0.3 / 检索 95 / 重排 422 / 生成 1670 ms |
| 单次成本 | `¥0.02` | `¥0.0035-0.0059`/次，均值 `¥0.0043`（9 次合计 `¥0.0385`） |

上表是 9 次手动流式压测（`bench_stream.py`），样本少、都是同一天的手挑 query。D7 又用 50 条评测集全量跑了一遍（`eval/report_s6a.md`，关思考、流式）：**首字 P50 1096 ms / P95 2150 ms / 最慢 2877 ms**，44 条有正文的里 10 条超 1.2 s；**整卡 P50 2626 ms / P95 3784 ms / 最慢 4554 ms**，0 条越 8 s；均值 **¥0.0051**。两组数字分开报，不挑好看的那组——P50 都过线，但首字 P95 与"10/44 超线"说明 1.2 s 不是稳定满足，缺陷表 §9 第 3 条记着这件事。

**思考 token 是此前"整卡 4 s 不可达"的真因**（v1.5 放宽口径的依据，现在有了更好的解法，门槛不动只补数据）：

- 关掉前：SSE 首 token 6137-6141 ms、整卡 6390 ms（同一条查询）；D4 四批 36 次 P50 约 4.2 s、P95 8003 ms（明细见 §5.3，那批就是默认开思考跑的）。
- 关掉后：首 token 1177 ms、整卡 2324 ms（上表）。
- 三种关法实测（20 token 小请求，量第一个 `delta.content`）：`thinking={"type":"disabled"}` 523 ms < `reasoning_effort="none"` 586 ms < 不关 855 ms —— 选第一种，写进 `_apply_thinking()`。
- 副作用：关思考会牺牲一点推理深度（多跳归因、跨文档串联）。D7 用评测集量过这个差值（50 条 full 档，同 prompt 同数据）：**开思考 Top-1 62.5% < 关思考 80.0%**，首字 P50 从 1096 ms 涨到 2856 ms、整卡 P50 3757 ms、成本 +49%（¥0.0064 vs ¥0.0051）。首字直接违反 ≤1.2 s 门槛，而 Top-1 还在退——所以 `LLM_THINKING` 保持 `disabled`，"只在复杂 query 上开"也不做（没有证据表明思考在这个任务上带来收益）。

**首字怎么做到 1.2 s**：SSE 帧序固定为 `meta`（Query 理解）→ `citations`（已确定的 chunk）→ `delta`（正文增量）→ `done`（`latency_ms`/`ttft_ms`/`cost_cny`）。首字不必等 LLM——检索与重排结束即可开流；拒答路径更早，<1 s 返回完整卡片（实测 690 ms，0 元）。1.2 s 里真正花掉的：重排 330-534 ms + LLM 首字 400-600 ms + 检索 95-230 ms。

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

### 5.3 生成层与端到端实测（M6/M7/M8 + D4，thinking 默认开启时的旧测）

> 这一节的数字是 `deepseek-flash` **默认开思考**（`effort=high`）跑出来的，保留作对照：它解释了 v1.5 当初为什么把整卡门槛放宽到 P50 ≤4.5 / P95 ≤8.0。关掉思考后的数据在 §5.4，整卡直接落到 2.3 s。

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

### 5.4 流式端到端实测（D5，关闭思考）

`python scripts/bench_stream.py --out data/bench_stream.json` 一把跑完（9 条查询 × 1 次，真实 API + 真实 SSE 链路，样本覆盖 k8s / nginx / MySQL / Redis 四个源，含 2 条语料里没有的题）。机器可读结果在 `data/bench_stream.json`。

| # | 查询（缩写） | TTFT | 整卡 | 其中检索 / 重排 / 生成 | 拒答 | causes | 输出 tok | 成本 |
|---|---|---|---|---|---|---|---|---|
| 1 | Pod CrashLoopBackOff，exit 137 | 953 ms | 2324 ms | 16 / 534 / 1766 | 否 | 2 | 296 | ¥0.0058 |
| 2 | 容器 OOMKilled，节点还有 30% | 989 ms | 2817 ms | 232 / 347 / 2232 | 否 | 2 | 329 | ¥0.0059 |
| 3 | nginx upstream prematurely closed | 1210 ms | 2068 ms | 18 / 523 / 1520 | 是 | 0 | 166 | ¥0.0040 |
| 4 | nginx 502 Bad Gateway（uwsgi） | 1349 ms | 2195 ms | 140 / 446 / 1601 | 是 | 0 | 158 | ¥0.0038 |
| 5 | MySQL 1213 deadlock | 1177 ms | 2745 ms | 14 / 390 / 2334 | 否 | 2 | 320 | ¥0.0049 |
| 6 | MySQL Too many connections | —（未进 LLM） | 690 ms | 139 / 544 / 0 | 是 | 0 | 0 | ¥0 |
| 7 | redis MISCONF 快照写不进去 | 827 ms | 1638 ms | 16 / 330 / 1284 | 是 | 0 | 154 | ¥0.0035 |
| 8 | Redis OOM command not allowed | 1173 ms | 2656 ms | 135 / 338 / 2176 | 否 | 2 | 319 | ¥0.0048 |
| 9 | kubectl 0/3 nodes Insufficient cpu | 1207 ms | 2613 ms | 140 / 344 / 2121 | 否 | 2 | 316 | ¥0.0058 |

**9 次汇总：TTFT P50 1177 ms / P95 1349 ms（8 次有正文的里 3 次略超 1.2 s）；整卡 P50 2324 ms / P95 2817 ms / 最慢 2817 ms，0/9 越 4 s；拒答 4/9；平均 ¥0.0043/次，合计 ¥0.0385。**

- 第 6 条是**闸门前置拒答**：重排 Top1 低于阈值，直接返回 `abstained=true` 的完整卡片，不调 LLM，690 ms / 0 元。这是最理想的一条失败路径。
- 第 3、4、7 条是**生成侧拒答**：检索到了相关块（重排 0.3-0.9），但模型判断不足以定根因，自己给 `abstained=true`。第 3、4 条正对上 §9 第 9 条（nginx 语料缺 502 排障专页）。
- 首字三次越线的都是"重排 445-534 ms + 首字 400-600 ms"叠加：重排慢的那几次（>440 ms）就压不住 1.2 s。想再往下压只有两条路——把重排候选从 20 降到 12（省约 100 ms，但牺牲排序质量），或改用便宜的小模型做首段生成（架构复杂度上升）。当前选择如实报 3/8，不藏。
- 检索 14-18 ms 的条目是 **embedding 命中缓存**（`data/embed_cache.jsonl` 按 `sha256(model+text)` 缓存）；135-232 ms 的是首次查该 query，多一次 `/embeddings` 往返（约 250 ms）。

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

7 个接口，`/docs` 里全部可直接试。除 webhook 外都吃 JSON、回 JSON；错误统一 `{"detail": "..."}`（422 是 Pydantic 校验失败，字段名在 `loc` 里）。每个请求都带 `X-Request-ID`（中间件生成或透传），并落一行 `query_log`（SQLite `data/oncall.db`）。

| 方法 | 路径 | 用途 | 同步/流式 | 单次成本 |
|---|---|---|---|---|
| POST | `/v1/ask` | 通用问答；`stream=true` 走 SSE | 两者都支持 | 有 LLM 调用就计费 |
| POST | `/v1/diagnose` | 结构化诊断（只非流式） | 同步 | 同上 |
| POST | `/webhook/alertmanager` | Alertmanager v4 告警接入，内部转诊断 | 同步（逐条串行） | 同上 |
| GET | `/v1/sources/{chunk_id}` | 按 chunk_id 取原文片段 + 出处 | 同步 | 免费 |
| POST | `/v1/admin/index` | 增量 / 全量重建索引 | 同步（阻塞） | 免费（命中 embedding 缓存） |
| GET | `/v1/metrics/summary` | 近 N 次的 TTFT / 整卡 / 拒答率 / 成本 | 同步 | 免费 |
| GET | `/v1/health` | 存活探活 + 五项依赖自检 | 同步 | 免费 |

### 7.1 POST /v1/ask

请求体（与 `/v1/diagnose` 共用）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 报错文本 / 告警文本，1-4000 字 |
| `stream` | bool | 否 | 默认 `false`；`true` 返回 `text/event-stream` |
| `top_n` | int 1-20 | 否 | 最终上下文条数，缺省取 `TOP_K_FINAL=5` |
| `allow_llm` | bool | 否 | Query 理解是否允许调 LLM；缺省按"规则没命中且 query>20 字"自动决定 |

非流式响应见 7.2（两者同构）。流式时按下面的帧序推送，`event:` 与 `data:` 两行一组：

| 序 | event | data 关键字段 | 何时发 |
|---|---|---|---|
| 1 | `meta` | `plan`（`intent` / `resource_type` / `error_codes` / `event_reasons` / `keywords` / `sub_index` / `need_llm`）、`request_id` | Query 理解完成（通常 <5 ms） |
| 2 | `citations` | `citations[]`（`chunk_id` / `title` / `heading_path` / `url`）、`context[]`（多带 `score` / `rerank_score` / `preview`） | 重排完成、开流之前——客户端先把"依据了哪几段"画出来 |
| 3 | `delta` ×N | `text`（正文增量，可能是半截 JSON） | 模型每吐一段 |
| 4 | `done` | `card`（完整诊断卡）、`ttft_ms`、`latency_ms`、`cost_cny`、`timings`、`report`、`regenerated`、`hit_chunk_ids` | 收尾，**以这一帧为准** |
| — | `error` | `stage`（retrieve/rerank/generate/verify）、`message` | 任一阶段抛错；HTTP 200 已发出，错误只能走事件 |

```
# 流式：-N 关掉 curl 的缓冲，才能看到逐帧到达
curl -N -X POST http://127.0.0.1:8000/v1/ask \
  -H 'Content-Type: application/json' \
  -d '{"query":"Pod 一直重启，kubectl describe 显示 OOMKilled，exit code 137","stream":true}'

event: meta
data: {"plan":{"intent":"diagnose","resource_type":"container","error_codes":["137"],...},"request_id":"6f3c1a2b"}

event: citations
data: {"citations":[{"chunk_id":"c_9484839c6f","title":"Pod 的生命周期","heading_path":"...", "url":"...","score":0.0328,"rerank_score":0.9865,"preview":"..."}],...}

event: delta
data: {"text":"{\"summary\": \"容器超出内存限制被 cgroup 杀掉"}

event: done
data: {"card":{"diagnosis_id":"dg_...","abstained":false,"causes":[...],"verify_commands":[...],"actions":[...],"citations":[...],"latency_ms":2317,"ttft_ms":948,"cost_cny":0.005756,"degraded":false},"ttft_ms":948,"latency_ms":2317,"cost_cny":0.005756,"timings":{...},"report":{"total":3,"valid":3,...},"regenerated":false,"hit_chunk_ids":["c_..."]}
```

三个必须知道的点：

- **`delta` 是过程，`done.card` 是结论**。引用校验不过时会在同一条流里重生成一次（不发明新事件），客户端会看到两段 JSON 拼在一起；解析以 `done` 为准，不要边收边解析。
- **拒答也是 200 + 正常帧**：`citations` 之后直接 `done`，`card.abstained=true`、`card.abort_reason=low_confidence|no_hits`，整个响应 690 ms 左右、不调 LLM、0 元。
- 响应头带 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`（后者给 Nginx 反向代理用，不加会被缓冲成一坨）。

### 7.2 POST /v1/diagnose

请求体同 7.1；**`stream=true` 会被 400 拒绝**，提示改用 `/v1/ask`。响应：

响应是**信封式**的（卡放在 `card` 字段里，同时带上 `plan` / `report` / `timings`）。设计清单 §7 只写了「结构化诊断卡」，没写信封；取舍是结论与过程证据分开摆：要卡就取 `card`，要可观测就取 `timings`。代价是客户端多解一层，好处是与 `/v1/ask` 流式 `done` 帧（同构）共用一套解析代码。

| 字段 | 说明 |
|---|---|
| `diagnosis_id` | 本次诊断 id（`dg_YYYYMMDD_xxxx`） |
| `card` | 诊断卡，字段见下表 |
| `plan` | Query 理解产物（与 `meta` 帧同构） |
| `citations` | 上下文里被引用到的 chunk（窄对象） |
| `report` | 引用校验报告：`total` / `valid` / `invalid_ids` / `orphan_causes` / `coverage` |
| `abstained_at` | 拒答发生在哪一档：`gate`（重排分数低，未进 LLM）/ `generator` / `null` |
| `regenerated` | 是否因引用校验失败重生成过一次 |
| `timings` | 分段耗时：`query_understanding_ms` / `retrieve_ms` / `rerank_ms` / `generate_ms` / `verify_ms` / `total_ms` |

`card` 的字段（Pydantic 模型，不是自由文本）：

| 字段 | 说明 |
|---|---|
| `summary` | 一句话结论（≤80 字） |
| `causes[]` | 根因列表（≤2 条），每条 `title` / `confidence`(0-1) / `explanation` / `evidence[]` |
| `verify_commands[]` | 只读核验命令（≤3 条，写操作会被白名单过滤掉） |
| `actions[]` | 处置建议（≤3 条） |
| `citations[]` | `chunk_id` / `title` / `heading_path` / `url`，**只可能出现在本次上下文里** |
| `abstained` / `abort_reason` | 是否拒答、拒答原因 |
| `latency_ms` / `ttft_ms` / `cost_cny` | 整卡耗时（端点自己盖，不信模型）、首字耗时、成本 |
| `degraded` | 走降级路径（重排失败退回 RRF / JSON 解析失败 / 引用两次都不过） |
| `model` / `prompt_tokens` / `completion_tokens` | 模型与 token 用量 |

### 7.3 POST /webhook/alertmanager

吃 Alertmanager v4 payload，只读 `alerts[]` 里的 `status` / `labels` / `annotations` / `fingerprint`，其余字段忽略。规则：

- 只处理 `status="firing"`；`resolved` 计入 `skipped`（不诊断）。
- 一次最多 `MAX_ALERTS=5` 条，超出的也计入 `skipped`；逐条**串行**诊断（并发打 LLM 会更慢）。
- 告警被拼成自然语言送进诊断链路（`alertname` + `severity` + `summary` + `description` + 关键 labels）。
- 失败的一条只在 `results[]` 里 `status="error"`，**所以 `accepted + skipped` 可能小于 `alerts` 总数**——这是有意的：部分失败不该让整次调用 500。
- 成功与失败的每一条都落 `query_log`（`endpoint=/webhook/alertmanager`）。

```
curl -X POST http://127.0.0.1:8000/webhook/alertmanager \
  -H 'Content-Type: application/json' \
  -d '{"version":"4","status":"firing","alerts":[
       {"status":"firing","labels":{"alertname":"PodCrashLooping","severity":"critical","pod":"order-api-7d9f"},
         "annotations":{"summary":"Pod 反复重启","description":"exit code 137, OOMKilled"},
         "fingerprint":"a1b2c3"}]}'

{"accepted":1,"skipped":0,"latency_ms":3430,
 "results":[{"fingerprint":"a1b2c3","alertname":"PodCrashLooping","status":"firing","severity":"critical",
             "diagnosis_id":"dg_20261102_ab12","abstained":false,"causes":["容器超出自身内存 limit"],
             "summary":"容器反复 OOM 重启","latency_ms":3402,"error":""}]}
```

### 7.4 GET /v1/sources/{chunk_id}

按 `chunk_id` 直查 LanceDB，返回一段原文与可点开的出处；不存在返回 `404 {"detail":"chunk not found"}`。

响应字段：`chunk_id` / `doc_id` / `source`（k8s|nginx|redis|mysql）/ `title` / `heading_path` / `url` / `anchor` / `order` / `token_count` / `text`（**不截断**，最多约 800 tokens）。

```
curl 'http://127.0.0.1:8000/v1/sources/c_c97fd0c9d2'
# 200，正文 381 字，32 ms
```

### 7.5 POST /v1/admin/index

| 字段 | 类型 | 说明 |
|---|---|---|
| `full` | bool | `true` 全量重建；`false`（默认）按 `doc_hash` 增量 |
| `dry_run` | bool | 只算要做什么，不写索引 |
| `limit` | int | 只处理前 N 篇（调试用） |

响应：`docs_scanned` / `added` / `updated` / `deleted` / `skipped` / `chunks_total` / `chunks_written` / `api_calls` / `cache_hits` / `tokens` / `cost_cny` / `elapsed_s` / `note`。

**同步执行，会阻塞到索引跑完**（全量约 98 s，二次运行 0.2 s）；生产环境应改成后台任务 + 进度查询，`note` 里也固定写了这句。`/docs` 里点 `Try it out` 不要拿 `full=true` 试。

### 7.6 GET /v1/metrics/summary

| 参数 | 范围 | 说明 |
|---|---|---|
| `window` | 1-2000 | 统计最近 N 次请求；缺省取 `METRICS_WINDOW=200`，超出会被夹紧 |

响应：`requests` / `ttft_p50_ms` / `ttft_p95_ms` / `latency_p50_ms` / `latency_p95_ms` / `abstain_rate` / `degraded_rate` / `error_rate` / `avg_cost_cny` / `total_cost_cny` / `avg_completion_tokens` / `by_endpoint`（按端点计数）/ `stages`（分阶段耗时）/ `abstain_sources`（拒答来源计数）/ `index`（文档与块数）。

`stages` 的键是 `query_understanding_ms` / `retrieve_ms` / `rerank_ms` / `generate_ms` / `verify_ms` / `total_ms`，每项给 `p50` / `p95` / `count`。数据来自 `query_log.timings_json`（每次请求落库），所以「整卡 P50 里检索占多少」可复现，不是事后估算。`abstain_sources` 形如 `{"gate": 3, "generator": 1, "none": 20}`：`gate` 是没调 LLM 就拒了，`generator` 是调了但证据/引用不过——混在一起会把「阈值太严」误读成「模型不会答」。

百分位用**最近秩法，不插值**：窗口内取排序后第 `⌈p×n⌉` 个值，样本少时不要按正态分布解读。`ttft_p50_ms` 为 `null` 表示窗口内全是拒答请求（没进 LLM，没有首字）。

### 7.7 GET /v1/health

`checks` 五项，全部是**关键项**：`db`（SQLite 可查）/ `vector_store`（LanceDB 有表且有行）/ `bm25`（pickle 可载）/ `content`（`content/` 下有 Markdown）/ `provider_keys`（三个密钥都非空）。

- 五项全过 → `status="ok"`，HTTP 200。
- 任一不过 → `status="degraded"`，**HTTP 仍是 200**：探活不该因为一个依赖挂了就把 Pod 重启，具体哪项没过看 `checks[name].ok` 与 `.detail`。
- `index` 里给 `docs` / `chunks` / `lancedb_path`，方便确认"服务起来了，索引也是新的"。

### 7.8 错误码与通用约定

| 状态码 | 场景 | 响应体 |
|---|---|---|
| 200 | 成功（含拒答、退化为纯文本片段） | 业务对象 |
| 400 | `/v1/diagnose` 传了 `stream=true`；参数语义冲突 | `{"detail":"..."}` |
| 404 | `chunk_id` 不存在 | `{"detail":"chunk not found"}` |
| 422 | Pydantic 校验失败（`query` 为空或 >4000 字、`top_n` 越界） | `{"detail":[{...}]}` |
| 500 | 未捕获异常 | `{"detail":"internal error"}` + 完整堆栈进日志 |

- 每个响应都带 `X-Request-ID`，与 `query_log.request_id`、日志里的 `rid=` 对应，排障时按这个 id 串。
- 所有端点都会被 `query_log` 记录（`/v1/admin/index` 与 `/v1/sources` 只记成功/失败，不记 token 与成本）。

## 8. 评测

评测集：40 条正样本 + 10 条负样本，一条命令出 Markdown 报告（`eval/run_eval.py`）。下表的实测列全部来自 `eval/report_s6a.md`——full 档、`deepseek-flash`、`LLM_THINKING=disabled`、流式、50 条全量。

| 指标 | 定义 | 门槛 | 实测 |
|---|---|---|---|
| Top-1 根因命中 | `causes[0].title` 与 `gold_root_cause` 语义一致（LLM-as-judge：关键词先判，漏判才调模型） | ≥0.70 | **80.0%**（32/40）✅ |
| Top-3 根因命中 | `causes[0..2]` 任一命中 | ≥0.88 | **82.5%**（33/40）❌ 差 5.5 pp |
| recall@20 | 重排前 20 条候选含任一 gold URL；**分母是全部 40 条正样本** | 观测项 | **97.5%**（39/40） |
| 引用命中率 | 引用中至少一条 url ∈ `gold_doc_urls`；**分母是作答样本数** | ≥0.90 | **84.8%**（28/33）❌ |
| 拒答准确率 | 负样本中正确拒答的比例 | ≥0.85 | **90.0%**（9/10）✅ |
| 误拒率 | 正样本被错误拒答的比例（必须与拒答率一起看） | 并报，不设门槛 | **17.5%**（7/40） |
| 首字 / 整卡 | 见 §4 | 首字 ≤1.2 s；整卡 P50 ≤4.5 s、P95 ≤8.0 s | 50 条评测：首字 P50 **1096 ms**；整卡 P50 **2626 ms**、P95 **3784 ms** ✅（9 次 `bench_stream` 压测见 §4） |
| 单次成本 | prompt + completion token 折算 | ≤¥0.02 | **¥0.0051** ✅ |

两处口径是 D7 才校准的，读表前先看：

- **recall@20 的分母是全部正样本，不排除拒答样本。** 召回发生在生成之前，模型拒答不该被算成"召回没找到"。旧口径（跳过拒答样本）会把 full 的召回算成 67.5%，看起来像召回崩了，实际池级召回 97.5%——两者差 30 个点，全是拒答造成的。
- **引用命中率的分母是作答样本数。** 拒答卡没有 citations 可核验，把它算进分母等于对同一次拒答重复扣分。
- 两条一改，"拒答到底是对是错"就只剩误拒率与拒答准确率两列来回答，不会再污染召回与引用。

**Top-1 从 67.5% 到 80.0% 是怎么来的**（D7 调优，只改了生成侧的拒答规则，检索链一行没动）：旧 prompt 写"资料不足以支撑结论就 abstain"，模型把"只覆盖故障链的一部分"也当成不足，13/40 正样本拒答；改成"候选块里出现与报错对应的原文（错误串/状态值/配置项名）就必须作答，只有零对应或讲别的组件才拒"之后，拒答降到 7/40，其中 6 条由拒答转作答、0 条反向退化。代价是多吐一条 cause，整卡 P50 从 2287 ms 涨到 2626 ms。

### 8.1 基线对比（待同口径重跑，暂不填）

> **为什么空着**：D7 跑过一版五档（`eval/report.md`），但那一版的生成侧是**旧 prompt**（拒答规则更严、`causes` 上限 2）。改完 prompt 后 full 一档从 67.5% 升到 80.0%（§8），四档基线若照抄旧数字，同一张表里就是两套口径——旧版里「混合 + RRF 85% 反而高于 full 67.5%」这个结论，主要就是被误拒率 15% vs 32.5% 拉出来的，不能当最终结论讲。补齐需按新 prompt 重跑 bm25 / vector / hybrid / hybrid_rerank 四档（50 条 ×4 ≈ ¥0.95），跑完直接替换下表。

| 变体 | Top-1 | Top-3 | recall@20 | 引用命中 | 误拒率 | 拒答准确率 | 首字 P50 (ms) | 整卡 P50 (ms) | 整卡 P95 (ms) | 成本/次 |
|---|---|---|---|---|---|---|---|---|---|---|
| BM25 only | | | | | | | | | | |
| 向量 only | | | | | | | | | | |
| 混合 + RRF | | | | | | | | | | |
| + Rerank | | | | | | | | | | |
| + Query 理解（full） | | | | | | | | | | |

**为什么误拒率和拒答准确率必须并排**：只报拒答准确率，把 `ABSTAIN_THRESHOLD` 调到 0.9 就能刷到 100%——正样本全被误杀，指标却更好看。两个数一起看才知道阈值是不是在「该拒的拒了、不该拒的没拒」。列顺序与 `eval/scoring.py` 的 `TABLE_HEADER` 逐字一致（11 列）。

---

## 9. 已知缺陷与限制

1. **语料不含 ACK**：阿里云 ACK 文档客户端渲染，抓不到正文；K8s 为主的上限在此。
2. **默认单机、单进程**：SQLite + LanceDB 嵌入式，不适合多副本并发写；生产化需换 `VectorStore` 实现（pgvector / ES）+ 独立索引 worker。
3. **首 token 不是稳定满足 1.2 s**：9 次流式实测 P50 1177 ms、P95 1349 ms、最慢 1349 ms（§5.4），8 次里 3 次超；D7 的 50 条评测集更明显——44 条有正文的里 **10 条超 1.2 s**，P50 1096 ms、P95 2150 ms、最慢 2877 ms。超的那几次都是「重排 445-534 ms + LLM 首字 400-600 ms」叠加，检索只占 14-232 ms。要压进 1.2 s 只有两条路：重排候选 20→12（约省 100 ms，牺牲排序质量），或首段换更小的模型（架构复杂化）；当前选择如实报。历史：thinking 默认开启时四批 36 次 P50 约 4.2 s、P95 8003 ms（§5.3）；关闭思考后整卡 P50 2324 ms，v1.5 门槛（P50 ≤4.5 s / P95 ≤8.0 s）已过。
4. **重排阈值标定过，结论是"不动"**：用 50 条评测集的 `rerank_top1` 画闸门 ROC（正样本 40 条，分值 0.496-0.990、均值 0.903；负样本 10 条，0.0087-0.944、均值 0.368）。t=0.30/0.35/0.40/0.45 都只抓到 6/10 负样本且误拒 0%；t=0.50-0.65 误拒升到 3%，t=0.70 误拒 5%，t=0.80 误拒 17%、也只多抓 1 条负样本。剩下 4 条负样本由生成侧引用校验兜住（负样本拒答准确率 100%）。`ABSTAIN_THRESHOLD=0.35` 已在平台区，上调只增加误拒，维持原值。
5. **`index.jsonl` 有 7 行历史重复**（k8s 1 / nginx 3 / mysql 3，续抓累积）：114/42/47 行 vs 113/39/44 个 `.md`；索引器只扫 `.md`，不受影响，新的 `--force` 已按 slug 覆盖写，不会再累积。
6. **没有鉴权**：接口对本地开放，网络暴露前必须加 token。
7. **LLM 生成失败降级**：JSON 解析失败或 `finish_reason=length` 重试 1 次（长度截断时追加"更短"指令），再失败降级为纯文本片段（`degraded=true`），此时没有结构化字段。重试会让整卡翻倍，是尾延迟的主要来源。**引用两次都不过**的收尾是 `abstained=true` + `degraded=true` + `abort_reason=citations_invalid`（ADR-5② 要求拒答，同时留标记把它与「检索不到」分开统计）。
8. **`embed_cache.jsonl` 是明文 JSONL**（47 MB / 2081 条，浮点全精度）：换 float32 二进制能压到 1/4，但重跑索引省下的 API 调用远比磁盘值钱，暂不做。
9. **nginx 语料只有官方模块参考页**：没有 502/504 排障专页，nginx 类问题 Top-1 常落在 `ngx_http_upstream_module` 的参数说明上——召回对、根因浅。D7 的报告只出整体指标，nginx 子集数字还没单列（需按 domain 过滤 dataset 重算）。
10. **Redis 语料有版本变体近似重复**（`operate-rs-7.22-*` / `7.8-*` / `7.4-*` 同一篇按版本重复收录）：`doc_hash` 精确去重识别不了，会抬升同质块竞争，D7 未处理（改语料会牵动召回与索引重建，属 S6b 之后的活）。
11. **流式与同步两条编排没有合并**（D6 代码审核记录，已留在代码注释里）：`app/core/streaming.py` 与 `app/core/pipeline.py` 各写了一遍「闸门一 → 生成 → 引用校验 → 降级」，靠三处 `model_copy` 对齐口径；正解是抽 `core/orchestrate.py`，让流式只负责转帧。D6 只做审核与修复，重构要配回归，留到 D8。
12. **测试 fixture 有两套同名副本**：`tests/test_api_ask.py` 与 `tests/test_streaming.py` 各自定义 `make_settings` / `make_chunk` / `FakeRetriever`，改 `ScoredChunk` 字段要改四处；应上移 `conftest.py`，同理留到 D8。
13. **`eval/scoring.py` 反向 import `app.obs.stats`**（只为共用 `percentile`）：app 不 import eval，无循环，但评测层并不完全独立；要把 stats.py 挪到公共包才能彻底解耦。
14. **重排选块有覆盖损失（S6b 待做，最影响引用命中）**：重排按块打分、不按"文档与问题整体相关"打分，同文档可占多个名额。实测 e015（`incompatible CNI versions`）：hybrid 档喂进生成的前 5 块里有 3 块含答案原文，经 rerank 后换成 container-lifecycle-hooks / cpu-constraint-namespace / memory-constraint-namespace（分 0.78-0.88）——gold 关键词覆盖 3/3 掉到 0/3；e018 同类。e008 更直接：前 5 块全来自 node-pressure-eviction 一篇。修法：块去重 + 每文档限额（如 ≤2 块），需配 §8 回归。
15. **引用命中 84.8% 未过 0.90**：失败样本不是引用格式错，而是喂进去的 5 块与故障无关（e006 问 nodeAffinity，喂的是 memory / cpu constraint 文档），模型只能引到不相关的 chunk——与第 14 条同源，单改 prompt 修不到。
16. **7 条正样本仍误拒**（e012 / e015 / e017 / e019 / e020 / e029 / e033）：前四条是选块没覆盖答案（第 14 条），e033 是模型即便有字面对应仍判"证据不足"。放宽拒答规则已经把误拒从 13 条压到 7 条，再放宽会开始伤负样本，收益要靠选块。
17. **Top-3 82.5% 未过 0.88 有一部分是拒答造成的**：卡一旦 abstain 就没有 `causes`，这 7 条在 Top-1 与 Top-3 上是同一批未命中；剩下未命中的多数是只吐 1-2 条 cause 且主因讲偏。`MAX_CAUSES` 从 2 调到 3 后，causes 条数分布是 2 条 21 行 / 3 条 12 行 / 0 条 7 行，说明上限不是主要瓶颈。

---

## 10. 目录结构

```
oncall-copilot/
├── app/
│   ├── api/          # ask / diagnose / webhook / sources / admin
│   ├── core/         # query_understanding / retriever / reranker / generator / guard / streaming / pipeline / prompts / schemas
│   ├── index/        # chunker / embedder / vector_store / bm25_store / indexer
│   └── obs/          # db / logging / stats（percentile 的单一实现）
├── content/          # 255 篇抓取的 Markdown（gitignore）
├── data/             # lancedb / bm25.pkl / oncall.db / embed_cache.jsonl（gitignore）
├── eval/             # dataset.jsonl（50 条）/ scoring.py（评测契约）/ run_eval.py / 探针样本 probe_*.jsonl
├── scripts/          # fetch_corpus.py / smoke_api.py / eval_retrieval_probe.py / bench_stream.py / d5_probe.py / audit_corpus.py / simulate_alert.py / demo.py
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
| D5 | API 全量（SSE / webhook / admin）+ 日志 | ✅ 完成（7 接口 + SSE 帧序 + query_log + metrics；9 次流式实测：首字 P50 1177 ms、整卡 P50 2324 ms、¥0.0043/次；171 个单测） |
| D6 | 评测集 + 评测脚本 | ✅ 完成（50 条：40 正 + 10 负，47 个 gold url 逐条核对命中语料；五档变体 + LLM judge 缓存；11 列基线表） |
| D7 | 5 变体基线 + 调参（唯一止损点） | ✅ 主指标过线（full 档 Top-1 **80.0%**、拒答准确 90.0%、成本 ¥0.0051、整卡 P50 2626 ms / P95 3784 ms）；Top-3 82.5%、引用命中 84.8% 未过线，归因见 §9 第 14-17 条；四档基线待同口径重跑（§8.1） |
| D8 | 降级路径测试 + pytest | 未开工 |
| D9-D10 | 演示脚本 + 录屏 + README 定稿 | 未开工 |

设计文档：[技术路线与设计清单.md](../技术路线与设计清单.md)（v1.8）

---

## 12. 一句话讲给面试官

> 自建 255 篇公开运维文档的检索语料，做了一条"报错文本 → Query 理解 → 向量+BM25 双路召回 → RRF → Cross-Encoder 重排 → 带引用结构化诊断卡"的链路；重点不在接模型，而在**引用可校验、不知道就拒答、首字靠流式架构而非堆硬件**。

（指标数字已按 D7 实测回填 §8：Top-1 80.0%、整卡 P50 2626 ms、¥0.0051/次。）
