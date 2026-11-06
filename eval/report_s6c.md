# On-call Copilot 评测报告（M10）

| 项 | 值 |
|---|---|
| 运行时间 | 2026-11-06 19:52:06 |
| 数据集 | C:\Users\chuan\Documents\deepseek-harness\default-workspace\oncall-copilot\eval\dataset.jsonl（50 条：正 40 / 负 10） |
| 变体 | + Query 理解（full） |
| 生成模型 | deepseek-flash（thinking=disabled） |
| judge | llm（缓存 eval/.judge_cache.json） |
| 流式 | 是：open_stream + finalize_stream，TTFT 从请求开始量 |

## §6.3 对比表

| 变体 | Top-1 | Top-3 | recall@20 | 引用命中 | 误拒率 | 拒答准确率 | 首字 P50 (ms) | 整卡 P50 (ms) | 整卡 P95 (ms) | 成本/次 |
|---|---|---|---|---|---|---|---|---|---|---|
| + Query 理解（full） | 87.5% | 90.0% | 97.5% | 89.2% | 7.5% | 90.0% | 1045 | 2824 | 3793 | ¥0.0065 |

> Top-1/Top-3 由 LLM-as-judge（关键词先判，漏判才调模型） 判定；失败明细仍按关键词判定，两者可能有个别样本不一致。
>
> 列口径：**recall@20 只看召回池、分母是全部正样本**（生成之前的事，拒答不该拖累它）；**引用命中只看答出来的卡、分母是作答样本数**（拒答的卡没有 citations 可核验）。两者都把拒答样本的处置交给误拒率一列。

## 失败明细（Top-1 未命中或引用未命中）

### + Query 理解（full）

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：准入 webhook 超时阻断删除
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/
- 引用未命中 gold_doc_urls
### e005（Pod）
- 输入：kubectl describe pod 的 Events 显示：Error: configmap "app-config" not found；另一个 Pod 报 Error: couldn't find key DATABASE_URL in ConfigMap default/app-config。容器状态 CreateContainerConfigError，Pod 一直起不来。
- gold：Pod 通过 envFrom / configMapKeyRef 引用的 ConfigMap 或其 key 不存在，且没有标记 optional，kubelet 无法构造容器环境变量，报 CreateContainerConfigError（ConfigMap 不存在时非 optional 引用会使 Pod 起不来）。
- 实际 Top-1：ConfigMap app-config 不存在
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/configure-redis-using-configmap/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/
- 引用未命中 gold_doc_urls
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点亲和性/选择器不匹配
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
### e007（Node）
- 输入：kubelet 日志：The node was low on resource: memory. Threshold quantity: 100Mi, available: 42Mi. Killing container with id containerd://a1b2... image: app:1.4. Pod phase 变成 Failed，Pod 被驱逐，业务在两分钟内丢了三批请求。
- gold：节点内存可用量跌破 memory.available 的硬阈值（默认 100Mi），kubelet 进入 MemoryPressure 并直接终止节点上的 Pod（Phase=Failed），不经过 PDB；应扩容节点内存或下调 Pod 内存申请。
- 实际 Top-1：内存低于硬驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/resource-management/pod-level-resource-managers/
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：imagefs 可用空间低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
### e010（Node）
- 输入：kubectl describe node node-05 显示 Taints: node.kubernetes.io/not-ready:NoExecute；节点上的 Pod 在 5 分钟内被大量驱逐，kubectl get node 里 STATUS 是 NotReady，kubelet 已停止上报心跳。
- gold：节点失联后被打上 node.kubernetes.io/not-ready 污点，该内置污点默认 effect 是 NoExecute，因此节点上的 Pod 被自动驱逐；恢复节点 Ready 状态或为 Pod 配置相应容忍。
- 实际 Top-1：kubelet 失联致节点 NotReady
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/
### e011（Node）
- 输入：新增的第三台 worker 在 kubectl get nodes 里始终看不到；节点上 systemctl status kubelet 是 activating，journalctl -u kubelet 里反复出现 failed to run Kubelet: unable to load bootstrap kubeconfig，证书签名请求也没出现在集群里。
- gold：kubelet 无法启动（bootstrap kubeconfig 加载失败），因此没有向 apiserver 注册，节点不会出现在节点列表里；需按节点注册与证书流程排查 kubelet 配置。
- 实际 Top-1：bootstrap kubeconfig 缺失或不可读
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-pods-nodes-using-node-affinity/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置与插件版本不兼容
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/containers/container-lifecycle-hooks/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/
### e017（存储）
- 输入：Pod 在 node-02 上一直 ContainerCreating；kubectl describe pod 的 Events：Multi-Attach error for volume "pvc-3f9c..." Volume is already exclusively attached to one node and can't be attached to another；该 PVC 的 accessModes 是 ReadWriteOnce，旧 Pod 还残留在 node-01 上。
- gold：ReadWriteOnce 卷只允许被单个节点以读写方式挂载，旧 Pod 还占用着该卷，新 Pod 调度到另一节点后无法再次挂载，报 Multi-Attach；需先确认旧 Pod 已终止或改用支持多节点的访问模式。
- 实际 Top-1：RWO 卷被旧 Pod 独占
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：未设临时存储请求导致驱逐
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
- 引用未命中 gold_doc_urls
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：磁盘/表空间写满
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/error-message-elements.html, https://dev.mysql.com/doc/refman/8.0/en/error-handling.html
### e020（镜像）
- 输入：Pod 状态 ImagePullBackOff，Events：Failed to pull image "registry.internal/private/api:v2": 401 Unauthorized: authentication required；Pod spec 里没有 imagePullSecrets，节点上的 containerd 也没有配置仓库凭据。
- gold：拉取私有仓库镜像缺少认证信息，仓库返回 401，kubelet 拉取失败；应创建 kubernetes.io/dockerconfigjson 类型的 Secret 并在 Pod 上通过 imagePullSecrets 引用。
- 实际 Top-1：缺少 imagePullSecrets 凭据
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/
- 引用未命中 gold_doc_urls

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 20 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

## 成本与耗时

| 项 | 值 |
|---|---|
| 生成成本合计 | ¥0.323362 |
| judge 成本合计 | ¥0.011564（调用 29 次，缓存命中 20 次） |
| **总花费** | **¥0.334926** |

| 变体 | 条数 | 单条均值 | 单条 P50 | 单条 P95 | 总耗时 |
|---|---|---|---|---|---|
| + Query 理解（full） | 50 | ¥0.006467 | ¥0.007370 | ¥0.008566 | 150.6s |

## 口径与已知差异（读表前必看）

- 五档只在检索链上不同，生成侧是同一套 prompt、同一个模型：
  - + Query 理解（full）：analyze（子库过滤 + 查询扩展）-> retrieve(use_sub_index=True) -> rerank -> top_k_final -> generate
- 非 full 变体不调 analyze()（QueryPlan 只填 raw/clean），所以基线里没有错误码抽取与查询扩展；这是为了把「Query 理解」的增量隔离出来。
- 无重排的三档（bm25/vector/hybrid）没有与 abstain_threshold 同量纲的分数，不套检索侧闸门，只靠生成侧兜底：它们的误拒率天然偏低，负样本拒答准确率也偏低，对比 full 时要带上这条。
- bm25/vector 的召回池也是 20 条（只把前 top_k_final 条喂生成），否则 recall@20 对这两档退化成 recall@top_k_final。
- error 样本只计入 errors，不进 Top-1/recall/成本的分母（scoring.summarize 口径）。
- full 的 Query 理解若走 LLM，其 token 不计入 card.cost_cny（analyze 不返回 usage），所以 full 成本略低估；严格对齐请设 QUERY_LLM_ENABLED=0 再跑。
