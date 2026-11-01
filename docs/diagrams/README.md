# On-call Copilot 全链路图

本目录三个文件，路径固定：

- `oncall-flow.svg` —— 手写 SVG，双击用浏览器就能看，无外部依赖
- `oncall-flow.mmd` —— 同结构的 Mermaid 源码（节点 id 英文、标签中文），可再编辑
- `README.md` —— 本文件：数字出处、终端版、30 秒话术、渲染方法

> 画图口径：**以仓库里的文件和代码为准**，不照抄任何口述版本。凡与设计清单/README/代码不一致的数字，见 §3 逐条说明。

## 1. 怎么读这张图

四条车道，颜色只有 4 个：

| 车道 | 位置 | 内容 | 颜色 |
|---|---|---|---|
| 在线主链路 | 中间竖列 | 一次请求从 HTTP 进、SSE 出，共 10 步 | 深蓝 #1F3A5F |
| 离线索引旁路 | 左侧竖列 | 语料怎么进库（M1–M3） | 灰 #6B7280 |
| 拒答出口 | 中右 | Top1 分数不足，不调 LLM | 橙 #B45309 |
| 产物 | 底部 + 注解卡 | 响应帧序 / 诊断卡 / 首字提速说明 | 绿 #15803D |

右侧 6 张虚线框是**注解卡**，用虚线引到对应环节，不参与流程：7 个接口、规则优先的判定条件、召回侧实测、重排实测、生成耗时、首字为什么能到 1.2 s。
左侧旁路底部到「向量召回 / BM25 召回」有一条灰色虚线，表示两库是离线灌好、在线只读。

## 2. 每个数字的出处

表中每行就是「数字 —— 出自 某文件 §某节」的形式。`app/...` 与 `oncall-copilot/README.md` 的路径相对于仓库根 `oncall-copilot/`；设计清单在 `oncall-copilot/docs/技术路线与设计清单.md`。

| 图上位置 | 数字 | —— 出自 |
|---|---|---|
| API 注解卡（右侧第 1 张） | 7 个接口 | 技术路线与设计清单.md §7 |
| API 注解卡 | 接口名单：/v1/ask · /v1/diagnose · /v1/health · /webhook/alertmanager · /v1/sources/{chunk_id} · /v1/admin/index · /v1/metrics/summary | 技术路线与设计清单.md §7 |
| 输出节点 | 帧序 meta → citations → delta → done | 技术路线与设计清单.md §6.5 第 1 条 |
| Query 理解 | 错误码正则 6 类 | app/core/query_understanding.py（ERROR_CODE_PATTERNS，6 条） |
| Query 理解 | 资源类型 26 词 | app/core/query_understanding.py（RESOURCE_TYPES 26 个键）；设计清单 M4 已按实测改为 26 词（v1.4） |
| Query 理解 | 事件原因 40 词 | 技术路线与设计清单.md §5 M4；query_understanding.py（EVENT_REASONS 40 条） |
| Query 理解 | 未命中且 query 超 20 字才调 LLM | 技术路线与设计清单.md §5 M4；app/core/query_understanding.py:34 MIN_LLM_QUERY_CHARS = 20 |
| 注解卡 cB | need_llm 2% | oncall-copilot/README.md §5.2（2.0%，50 条里 1 条） |
| 注解卡 cB | 向量查询文本 = query + 错误码 + 强事件词 | oncall-copilot/README.md §5.2 |
| 向量召回 | bge-m3 · 1024 维 | oncall-copilot/README.md §4 |
| 向量 / BM25 召回 | Top20 + Top20 | 技术路线与设计清单.md §5 M5；app/config.py:59-60 |
| RRF 融合 | k = 60，score = Σ 1/(k + rank_i) | 技术路线与设计清单.md §5 M5 / §13.4；app/config.py:63 rrf_k = 60 |
| RRF 融合 | 输出 Top20 | app/config.py:61 top_k_fuse = 20 |
| Cross-Encoder 重排 | Top20 → Top5 | 技术路线与设计清单.md §5 M6；app/config.py:64 top_k_final = 5 |
| 注解卡 cG | 200 ms（bge-reranker-v2-m3，实测 3 条候选） | oncall-copilot/README.md §4；技术路线与设计清单.md §6.5 |
| 拒答闸门 | 0.35（ABSTAIN_THRESHOLD，Top1 低于即拒答） | 技术路线与设计清单.md §13.4 / §5 M8；app/config.py:65；README §9 第 4 条（只是初值，D7 用评测集标定） |
| 生成 | deepseek-flash | 技术路线与设计清单.md §5 M7 |
| 注解卡 cE | 约 130 token/s；400 token 约 2.7–3.3 s | oncall-copilot/README.md §4（400 token 实测 3255 / 3286 / 2702 ms）；技术路线与设计清单.md §6.5 |
| 注解卡 cE | 整卡 3–4 s，验收门槛 ≤4.0 s | oncall-copilot/README.md §9 第 3 条；技术路线与设计清单.md §6.2 |
| 引用校验 | 重生成 1 次，再失败降级 | 技术路线与设计清单.md §5 M8 |
| 注解卡 cF | 首 token ≤1.2 s | 技术路线与设计清单.md §6.2 / §6.5 第 1 条；oncall-copilot/README.md §4 |
| 注解卡 cC | embedding 303 ms（实测 2 条） | oncall-copilot/README.md §4；技术路线与设计清单.md §6.5 |
| 注解卡 cC | 检索热路径 P50 14 ms / P95 17 ms | oncall-copilot/README.md §5.2 |
| 注解卡 cC | 冷启动 0.8 s | oncall-copilot/README.md §5.2（首次查询，含模型加载） |
| 旁路 M1 采集 | 抓 4 个源官网 | oncall-copilot/README.md §5 |
| 旁路 content/ | 255 篇 · 214.8 万字（2,148,036 字）· 约 3.4 MB | oncall-copilot/README.md §5（k8s 113 / redis 59 / nginx 39 / mysql 44） |
| 旁路 chunker | 2099 块 · 单块 ≤800 token · 100 token 重叠 | oncall-copilot/README.md §5.1；技术路线与设计清单.md §5 M2；app/index/chunker.py:34-35 |
| 旁路 embedder | batch 16 | 技术路线与设计清单.md §5 M3；app/config.py:73 |
| 旁路 embedder | embed_cache 47 MB · 2081 条 | oncall-copilot/README.md §5.1、§9 第 8 条 |
| 旁路 索引落地 | LanceDB 11.5 MB · bm25.pkl 6.4 MB · SQLite 168 KB | oncall-copilot/README.md §5.1 |
| 旁路 索引成本 | 全量重建 98 s · 二次 0.2 s / 0 次 API · 改一篇只重算 1 块 | oncall-copilot/README.md §5.1（125 次 /embeddings、72.8 万 token） |
| 拒答出口节点 | 不调 LLM，该路径 1 s 内返回 | 技术路线与设计清单.md §6.5 第 2 条 |

## 3. 与口述版本不一致的 6 处（我按文件和代码改的）

1. **冷启动**：口述是「0.8–1.5 s」，文档里只有「首次查询 0.8 s（含模型加载）」（README §5.2），**图上写 0.8 s**，没有 1.5 这个数。
2. **整卡耗时**：口述是「3.5–4 s」，文档口径是「整卡 ≤4.0 s」（设计清单 §6.2）和「整卡仍 3–4 s，P95 4.5 s 量级」（README §9 第 3 条），**图上写「整卡 3–4 s（门槛 ≤4.0 s）」**。
3. **诊断卡字段**：口述是 summary / root_cause / confidence / evidence / next_steps / citations，设计清单 §7 的真实定义是 `diagnosis_id / abstained / causes[{title, confidence, evidence[chunk_id, url, score]}] / verify_commands / actions / citations / latency_ms / cost_cny / degraded`。**图上只写真实存在的 causes / verify_commands / actions**，没有编 summary、root_cause、next_steps。
4. **资源类型词典**：代码 `RESOURCE_TYPES` 是 26 个键，**图上写 26**；设计清单 M4 原写 20 词，已按实测修成 26 词（v1.4）。
5. **embedding batch**：设计清单 §3.1 原先写 32–64、§5 M3 与 `config.py:73` 是 16，**图上写 16**；§3.1 已在 v1.4 统一为 16（实测 125 次 `/embeddings` 请求）。
6. **边界要讲清楚**：M6 / M7 / M8（reranker / generator / guard）**代码还没落地**——`app/core/` 只有 `query_understanding.py`、`retriever.py`、`schemas.py`，README §11 只做到 D3。图上的重排、生成、引用校验、拒答闸门是设计基线里的接口形态，**不是已跑通的实现**，讲图时别越过这条线。

## 4. 终端 ASCII 版（60–80 列）

```
On-call Copilot 全链路：在线请求 + 离线索引
===========================================================

【在线请求主链路】
  (1) 客户端 / 告警源
      HTTP 请求 · SSE 流式 · Alertmanager v4 webhook
      │
      ▼
  (2) API 层 · FastAPI（M9）—— 7 个接口
      /v1/ask /v1/diagnose /v1/health
      /webhook/alertmanager /v1/sources/{chunk_id}
      /v1/admin/index /v1/metrics/summary
      │
      ▼
  (3) Query 理解（M4）· 规则优先
      错误码正则 6 类 · 资源类型 26 词 · 事件原因 40 词
      未命中且 query 超 20 字才调 LLM（need_llm 2%）
      │
      ├──▶ (4a) 向量召回  LanceDB · bge-m3 · 1024 维 → Top20
      └──▶ (4b) BM25 召回  jieba 分词 · rank_bm25 → Top20
      │
      ▼
  (5) RRF 融合（M5）  k = 60，Σ 1/(k + rank_i) → Top20
      │
      ▼
  (6) Cross-Encoder 重排（M6）  bge-reranker-v2-m3 · 200 ms
      Top20 → Top5；接口失败退回 RRF 排序并标 degraded
      │
      ▼
  (7) 拒答闸门（M8）  重排 Top1 分数 < 0.35 ?
      ├── 是 ──▶ abstained=true 卡片（不调 LLM，1 s 内返回）
      └── 否 ──▶ 继续往下
      │
      ▼
  (8) 生成（M7）· deepseek-flash
      结构化诊断卡 JSON：causes / verify_commands / actions
      约 130 token/s，400 token 约 2.7–3.3 s
      │
      ▼
  (9) 引用校验（M8）  chunk_id 必须在本轮上下文
      失败重生成 1 次；再失败降级返回原文片段（degraded）
      │
      ▼
 (10) 输出 · SSE / JSON  帧序 meta → citations → delta → done
      首 token ≤1.2 s（检索+重排完即开流）· 整卡 3–4 s

【离线索引旁路（M1–M3）】
  (A) 采集  scripts/fetch_corpus.py → 抓 4 个源官网
      │
      ▼
  (B) 语料 content/  255 篇 · 214.8 万字 · 约 3.4 MB
      │
      ▼
  (C) 切分 chunker  按 H2/H3 → 2099 块
      ≤800 token · 相邻块留 100 token 重叠
      │
      ▼
  (D) embedder  batch 16 · embed_cache 47 MB · 2081 条
      │
      ▼
  (E) 索引落地  LanceDB 11.5 MB · bm25.pkl 6.4 MB · SQLite
      doc_hash 增量 · 软删标记
      │
      ╰── 离线灌库 ──▶ 在线 (4a)/(4b) 读取
          全量重建 98 s · 二次运行 0.2 s / 0 次 API
          改一篇文档只重算 1 块
```

拷贝时用等宽字体；终端窗口小于 80 列会折行。

## 5. 讲这张图的 3 句话（30 秒版）

1. **理解很便宜**：错误码、资源类型、事件原因都是词典 + 正则，命中就走，规则没命中且 query 超过 20 字才调一次 LLM 做意图分类——实测只有 2% 的 query 会走 LLM，其余直接路由到子库。
2. **召回只信排名**：向量 Top20 和 BM25 Top20 并行召回，RRF 用 k=60 只融合排名不融合分数，再交给 Cross-Encoder 从 20 条压到 5 条；Top1 分数低于 0.35 就直接返回 abstained 卡片，一个 token 都不生成。
3. **首字快是因为不等 LLM**：检索 + 重排一结束就开 SSE，先推 meta / citations，正文用 delta 跟上，所以首 token 能压到 1.2 s 以内，整卡落在 3–4 s。

## 6. 渲染与编辑

### SVG

- 双击用浏览器打开；Inkscape、VS Code 预览也能看。不依赖任何字体文件、图片、CDN、脚本、渐变、阴影。
- 颜色只有 4 个 hex：`#1F3A5F` 主链路、`#6B7280` 旁路、`#B45309` 拒答、`#15803D` 产物；盒子里是 `fill="none"`，穿白底。
- **改图的坐标常量**（都在一棵「行」的网格上）：画布 1400×1000；主链路 x=360、宽 400、盒高 66、行距 90，10 行的 y = 90 / 180 / 270 / 360 / 450 / 540 / 630 / 720 / 810 / 900；双路召回是两个宽 188 的盒子，x=360 与 x=572；左侧旁路 x=40、宽 240；右侧注解卡 x=800、宽 530，纵向对齐它指向那一行的中心。
- **文字上限 = 盒宽 − 40**（左右各留 20），超了就缩句，不要压字号——字号最小 14。
- 改完必须重跑校验（命令见 §7），四条：XML 能解析、`<text>` 数量对得上、没有未转义 `&`、颜色仍然是 4 个。

### Mermaid

- `oncall-flow.mmd` 是**裸 Mermaid 源码**（没有 mermaid 代码围栏，整段就是图源），整段贴到 mermaid.live 就能出图。
- 本地出图：`mmdc -i oncall-flow.mmd -o oncall-flow-mmd.svg -w 1600 -b white`（mermaid-cli）；VS Code 装 Markdown Preview Mermaid Support 也能直接看。
- 语义以 SVG 为准，Mermaid 是同一结构的可编辑副本：子图 MAIN = 在线主链路，子图 IDX = 离线旁路，`store -. 离线灌库 .-> vec/bm25` 对应图里那条灰虚线，`gate -- 拒答 --> abst` 对应橙色出口。

### ASCII

- 直接复制文本框内容；要改就改文本本身，宽度别超过 80 列（当前最宽 61 列）。

## 7. 自检记录

执行时间 2026-11-01，Python 3.14.0：

```python
import xml.dom.minidom as m
d = m.parse(r"oncall-flow.svg")   # 解析通过
```

| 检查项 | 结果 |
|---|---|
| `xml.dom.minidom.parse` | 通过（根节点 svg） |
| viewBox / 尺寸 | `0 0 1400 1000`，width/height = 1400×1000 |
| `<text>` 元素 | 88 |
| `<rect>` / `<path>` / `<marker>` | 24 / 30 / 4 |
| 未转义 `&` | 0（全文共 0 个 `&` 字符，`<` `>` 用「低于」等中文替代表述） |
| 字号 | 14 / 16 / 22，最小 14 |
| 颜色 | 恰好 4 个 hex |
| 外部引用 | 仅 `xmlns` 里的 `http://www.w3.org/2000/svg`；`<image>` `<script>` `<style>` 渐变 filter 均为 0 |
| 文字与连线重叠 | 生成时对每条线段逐点采样、与每段文字的包围盒求交，0 处 |
| 盒子相互重叠 | 0 处 |
| 盒内文字超宽 | 0 处（按盒宽 − 40 逐行量过） |
| PNG | 未出：本机 Python 3.14 没有 cairosvg / matplotlib / Pillow，且 cairosvg 对中文字形不可靠，宁可不出错图 |
