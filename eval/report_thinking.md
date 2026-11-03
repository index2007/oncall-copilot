# On-call Copilot 评测报告（M10）

| 项 | 值 |
|---|---|
| 运行时间 | 2026-11-03 13:13:39 |
| 数据集 | C:\Users\chuan\Documents\deepseek-harness\default-workspace\oncall-copilot\eval\dataset.jsonl（50 条：正 40 / 负 10） |
| 变体 | + Query 理解（full） |
| 生成模型 | deepseek-flash（thinking=auto） |
| judge | llm（缓存 eval/.judge_cache.json） |
| 流式 | 是：open_stream + finalize_stream，TTFT 从请求开始量 |

## §6.3 对比表

| 变体 | Top-1 | Top-3 | recall@20 | 引用命中 | 误拒率 | 拒答准确率 | 首字 P50 (ms) | 整卡 P50 (ms) | 整卡 P95 (ms) | 成本/次 |
|---|---|---|---|---|---|---|---|---|---|---|
| + Query 理解（full） | 62.5% | 62.5% | 72.5% | 65.0% | 25.0% | 90.0% | 2856 | 3757 | 5853 | ¥0.0064 |

> Top-1/Top-3 由 LLM-as-judge（关键词先判，漏判才调模型） 判定；失败明细仍按关键词判定，两者可能有个别样本不一致。

## 失败明细（Top-1 未命中或引用未命中）

### + Query 理解（full）

### e002（Pod）
- 输入：kubectl get pod payment-5c8b 反复重启，Last State 显示 Terminated，Reason: OOMKilled，Exit Code: 137；kubectl describe pod 里 Limits: memory: 256Mi，容器实际 RSS 在压测时涨到 600Mi 后被杀。应用日志最后一行是处理 200MB 上传文件时的 buffer 分配。
- gold：容器内存实际用量超过 limits.memory（256Mi），内核 OOM 机制触发并杀死容器进程（Exit Code 137 / OOMKilled），随后被 kubelet 按重启策略重建；需要调大 limits.memory 或修掉大对象缓存。
- 实际 Top-1：内存超限被 kubelet 终止
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-default-namespace/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点标签不匹配亲和性
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
- 引用未命中 gold_doc_urls
### e007（Node）
- 输入：kubelet 日志：The node was low on resource: memory. Threshold quantity: 100Mi, available: 42Mi. Killing container with id containerd://a1b2... image: app:1.4. Pod phase 变成 Failed，Pod 被驱逐，业务在两分钟内丢了三批请求。
- gold：节点内存可用量跌破 memory.available 的硬阈值（默认 100Mi），kubelet 进入 MemoryPressure 并直接终止节点上的 Pod（Phase=Failed），不经过 PDB；应扩容节点内存或下调 Pod 内存申请。
- 实际 Top-1：节点内存低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/resource-management/pod-level-resource-managers/
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：（无 causes）
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e010（Node）
- 输入：kubectl describe node node-05 显示 Taints: node.kubernetes.io/not-ready:NoExecute；节点上的 Pod 在 5 分钟内被大量驱逐，kubectl get node 里 STATUS 是 NotReady，kubelet 已停止上报心跳。
- gold：节点失联后被打上 node.kubernetes.io/not-ready 污点，该内置污点默认 effect 是 NoExecute，因此节点上的 Pod 被自动驱逐；恢复节点 Ready 状态或为 Pod 配置相应容忍。
- 实际 Top-1：（无 causes）
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：nodefs 可用量触及驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
- 引用未命中 gold_doc_urls
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：表空间磁盘空间耗尽
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/error-message-elements.html, https://dev.mysql.com/doc/refman/8.0/en/error-handling.html
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 空间耗尽
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e023（镜像）
- 输入：Pod 事件：MountVolume.SetUp failed for volume "image-vol" : rpc error: failed to mount image volume: runtime does not support image volumes；Pod 卡在 ContainerCreating。
- gold：该 Pod 使用了镜像卷（volume 来源于 OCI 镜像内容），但节点容器运行时未启用/不支持镜像卷特性，卷挂载失败；需换支持镜像卷的运行时或改用普通卷。
- 实际 Top-1：运行时版本不支持 image 卷
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/windows/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/containers/container-lifecycle-hooks/
- 引用未命中 gold_doc_urls
### e024（资源）
- 输入：Pod 一直 Pending；kubectl describe pod 的 Events：0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod。Pod 声明 requests.memory=8Gi，而三台节点可分配的只剩 5Gi 左右。
- gold：Pod 声明的 requests.memory 超过所有节点当前可分配的内存，调度器无可调度节点，Pod 停在 Pending 并报 Insufficient memory；需降低 requests 或扩容节点。
- 实际 Top-1：内存请求超过节点可分配量
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
### e025（资源）
- 输入：kubectl describe pod 的 Events：0/5 nodes are available: 5 Insufficient cpu。Pod 的 requests.cpu=4，节点总核数 4 但已有工作负载占用了大部分，节点上 Allocatable 一栏显示 cpu: 3900m。
- gold：Pod 的 requests.cpu 超过节点可分配（Allocatable）的 CPU，调度器找不到满足的节点，报 Insufficient cpu；应调整 requests.cpu 或扩容节点。
- 实际 Top-1：CPU 请求超出节点可分配量
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-overhead/
### e026（资源）
- 输入：Error from server (Forbidden): error when creating "deploy.yaml": pods "web-1" is forbidden: exceeded quota: compute-resources, requested: requests.cpu=2, used: requests.cpu=7, limited: requests.cpu=8。之前同一个 Deployment 在别的命名空间能正常创建。
- gold：命名空间 ResourceQuota 的 CPU 请求额度用尽，新 Pod 超出配额被 apiserver 拒绝并返回 403 Forbidden；需要提高配额或删除不再使用的负载。
- 实际 Top-1：命名空间 CPU 配额耗尽
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 14 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

## 成本与耗时

| 项 | 值 |
|---|---|
| 生成成本合计 | ¥0.322214 |
| judge 成本合计 | ¥0.036376（调用 20 次，缓存命中 4 次） |
| **总花费** | **¥0.358590** |

| 变体 | 条数 | 单条均值 | 单条 P50 | 单条 P95 | 总耗时 |
|---|---|---|---|---|---|
| + Query 理解（full） | 50 | ¥0.006444 | ¥0.007102 | ¥0.009624 | 222.3s |

## 口径与已知差异（读表前必看）

- 五档只在检索链上不同，生成侧是同一套 prompt、同一个模型：
  - + Query 理解（full）：analyze（子库过滤 + 查询扩展）-> retrieve(use_sub_index=True) -> rerank -> 5 -> generate
- 非 full 变体不调 analyze()（QueryPlan 只填 raw/clean），所以基线里没有错误码抽取与查询扩展；这是为了把「Query 理解」的增量隔离出来。
- 无重排的三档（bm25/vector/hybrid）没有与 abstain_threshold 同量纲的分数，不套检索侧闸门，只靠生成侧兜底：它们的误拒率天然偏低，负样本拒答准确率也偏低，对比 full 时要带上这条。
- bm25/vector 的召回池也是 20 条（只把前 5 条喂生成），否则 recall@20 对这两档退化成 recall@5。
- error 样本只计入 errors，不进 Top-1/recall/成本的分母（scoring.summarize 口径）。
- full 的 Query 理解若走 LLM，其 token 不计入 card.cost_cny（analyze 不返回 usage），所以 full 成本略低估；严格对齐请设 QUERY_LLM_ENABLED=0 再跑。
