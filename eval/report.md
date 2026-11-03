# On-call Copilot 评测报告（M10）

| 项 | 值 |
|---|---|
| 运行时间 | 2026-11-03 13:07:43 |
| 数据集 | C:\Users\chuan\Documents\deepseek-harness\default-workspace\oncall-copilot\eval\dataset.jsonl（50 条：正 40 / 负 10） |
| 变体 | BM25 only、向量 only、混合 + RRF、+ Rerank、+ Query 理解（full） |
| 生成模型 | deepseek-flash（thinking=disabled） |
| judge | llm（缓存 eval/.judge_cache.json） |
| 流式 | 是：open_stream + finalize_stream，TTFT 从请求开始量 |

## §6.3 对比表

| 变体 | Top-1 | Top-3 | recall@20 | 引用命中 | 误拒率 | 拒答准确率 | 首字 P50 (ms) | 整卡 P50 (ms) | 整卡 P95 (ms) | 成本/次 |
|---|---|---|---|---|---|---|---|---|---|---|
| BM25 only | 80.0% | 80.0% | 77.5% | 75.0% | 20.0% | 100.0% | 625 | 1841 | 2474 | ¥0.0049 |
| 向量 only | 77.5% | 77.5% | 75.0% | 72.5% | 22.5% | 90.0% | 858 | 2085 | 2656 | ¥0.0050 |
| 混合 + RRF | 85.0% | 85.0% | 82.5% | 77.5% | 15.0% | 90.0% | 610 | 1858 | 2397 | ¥0.0050 |
| + Rerank | 67.5% | 67.5% | 67.5% | 65.0% | 32.5% | 100.0% | 1139 | 2117 | 2878 | ¥0.0044 |
| + Query 理解（full） | 67.5% | 67.5% | 67.5% | 62.5% | 32.5% | 100.0% | 1083 | 2287 | 2913 | ¥0.0043 |

> Top-1/Top-3 由 LLM-as-judge（关键词先判，漏判才调模型） 判定；失败明细仍按关键词判定，两者可能有个别样本不一致。

## 失败明细（Top-1 未命中或引用未命中）

### BM25 only

### e002（Pod）
- 输入：kubectl get pod payment-5c8b 反复重启，Last State 显示 Terminated，Reason: OOMKilled，Exit Code: 137；kubectl describe pod 里 Limits: memory: 256Mi，容器实际 RSS 在压测时涨到 600Mi 后被杀。应用日志最后一行是处理 200MB 上传文件时的 buffer 分配。
- gold：容器内存实际用量超过 limits.memory（256Mi），内核 OOM 机制触发并杀死容器进程（Exit Code 137 / OOMKilled），随后被 kubelet 按重启策略重建；需要调大 limits.memory 或修掉大对象缓存。
- 实际 Top-1：内存超限被终止
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-init-containers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/resize-container-resources/
### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：验证 Webhook 超时阻塞删除
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-pod-namespace/
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点标签不匹配亲和/选择器
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://redis.io/docs/latest/operate/rs/clusters/quorum-node/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/extended-resource-node/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/
- 引用未命中 gold_doc_urls
### e007（Node）
- 输入：kubelet 日志：The node was low on resource: memory. Threshold quantity: 100Mi, available: 42Mi. Killing container with id containerd://a1b2... image: app:1.4. Pod phase 变成 Failed，Pod 被驱逐，业务在两分钟内丢了三批请求。
- gold：节点内存可用量跌破 memory.available 的硬阈值（默认 100Mi），kubelet 进入 MemoryPressure 并直接终止节点上的 Pod（Phase=Failed），不经过 PDB；应扩容节点内存或下调 Pod 内存申请。
- 实际 Top-1：节点内存低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/concepts/containers/container-lifecycle-hooks/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：imagefs 可用空间低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置版本高于插件支持
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：未设 ephemeral-storage 请求
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/policy/resource-quotas/, https://kubernetes.io/zh-cn/docs/concepts/policy/resource-quotas/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
- 引用未命中 gold_doc_urls
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/configuration/secret/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/
### e024（资源）
- 输入：Pod 一直 Pending；kubectl describe pod 的 Events：0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod。Pod 声明 requests.memory=8Gi，而三台节点可分配的只剩 5Gi 左右。
- gold：Pod 声明的 requests.memory 超过所有节点当前可分配的内存，调度器无可调度节点，Pod 停在 Pending 并报 Insufficient memory；需降低 requests 或扩容节点。
- 实际 Top-1：内存请求超过节点可分配量
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-default-namespace/
### e025（资源）
- 输入：kubectl describe pod 的 Events：0/5 nodes are available: 5 Insufficient cpu。Pod 的 requests.cpu=4，节点总核数 4 但已有工作负载占用了大部分，节点上 Allocatable 一栏显示 cpu: 3900m。
- gold：Pod 的 requests.cpu 超过节点可分配（Allocatable）的 CPU，调度器找不到满足的节点，报 Insufficient cpu；应调整 requests.cpu 或扩容节点。
- 实际 Top-1：Pod 请求超过节点可分配 CPU
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-overhead/
### e026（资源）
- 输入：Error from server (Forbidden): error when creating "deploy.yaml": pods "web-1" is forbidden: exceeded quota: compute-resources, requested: requests.cpu=2, used: requests.cpu=7, limited: requests.cpu=8。之前同一个 Deployment 在别的命名空间能正常创建。
- gold：命名空间 ResourceQuota 的 CPU 请求额度用尽，新 Pod 超出配额被 apiserver 拒绝并返回 403 Forbidden；需要提高配额或删除不再使用的负载。
- 实际 Top-1：命名空间 CPU 配额不足
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/concepts/policy/resource-quotas/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-pod-namespace/
### e028（网络）
- 输入：nginx error.log：upstream timed out (110: Connection timed out) while reading response header from upstream, client: 10.0.1.5, request: "GET /api/report HTTP/1.1", upstream: "http://10.0.2.9:8080/api/report"；客户端侧看到 504 Gateway Time-out。
- gold：nginx 从上游读取响应头超过 proxy_read_timeout（默认 60s）仍未收到数据，判定上游超时并向客户端返回 504；根因是后端报表接口处理时间过长或上游卡住。
- 实际 Top-1：上游响应头超时
- 上下文：https://nginx.org/en/docs/http/ngx_http_proxy_module.html, https://nginx.org/en/docs/http/ngx_http_core_module.html, https://nginx.org/en/docs/http/ngx_http_upstream_module.html, https://nginx.org/en/docs/http/ngx_http_fastcgi_module.html, https://nginx.org/en/docs/http/ngx_http_upstream_module.html

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 16 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### 向量 only

### e002（Pod）
- 输入：kubectl get pod payment-5c8b 反复重启，Last State 显示 Terminated，Reason: OOMKilled，Exit Code: 137；kubectl describe pod 里 Limits: memory: 256Mi，容器实际 RSS 在压测时涨到 600Mi 后被杀。应用日志最后一行是处理 200MB 上传文件时的 buffer 分配。
- gold：容器内存实际用量超过 limits.memory（256Mi），内核 OOM 机制触发并杀死容器进程（Exit Code 137 / OOMKilled），随后被 kubelet 按重启策略重建；需要调大 limits.memory 或修掉大对象缓存。
- 实际 Top-1：内存超限被终止
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/resize-pod-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：Webhook 超时阻塞删除
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/kubectl-node-debug/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/
- 引用未命中 gold_doc_urls
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点标签不匹配亲和/选择器
- 上下文：https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/topology/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-scheduling-readiness/
- 引用未命中 gold_doc_urls
### e007（Node）
- 输入：kubelet 日志：The node was low on resource: memory. Threshold quantity: 100Mi, available: 42Mi. Killing container with id containerd://a1b2... image: app:1.4. Pod phase 变成 Failed，Pod 被驱逐，业务在两分钟内丢了三批请求。
- gold：节点内存可用量跌破 memory.available 的硬阈值（默认 100Mi），kubelet 进入 MemoryPressure 并直接终止节点上的 Pod（Phase=Failed），不经过 PDB；应扩容节点内存或下调 Pod 内存申请。
- 实际 Top-1：节点内存压力触发驱逐
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：imagefs 可用空间低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置版本高于插件支持
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：表空间/磁盘写满
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/create-logfile-group.html, https://dev.mysql.com/doc/refman/8.0/en/binary-log.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/binary-log.html
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 达阈值触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e024（资源）
- 输入：Pod 一直 Pending；kubectl describe pod 的 Events：0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod。Pod 声明 requests.memory=8Gi，而三台节点可分配的只剩 5Gi 左右。
- gold：Pod 声明的 requests.memory 超过所有节点当前可分配的内存，调度器无可调度节点，Pod 停在 Pending 并报 Insufficient memory；需降低 requests 或扩容节点。
- 实际 Top-1：节点可分配内存不足
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
### e025（资源）
- 输入：kubectl describe pod 的 Events：0/5 nodes are available: 5 Insufficient cpu。Pod 的 requests.cpu=4，节点总核数 4 但已有工作负载占用了大部分，节点上 Allocatable 一栏显示 cpu: 3900m。
- gold：Pod 的 requests.cpu 超过节点可分配（Allocatable）的 CPU，调度器找不到满足的节点，报 Insufficient cpu；应调整 requests.cpu 或扩容节点。
- 实际 Top-1：请求超过节点可分配CPU
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/resize-container-resources/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-overhead/
### e026（资源）
- 输入：Error from server (Forbidden): error when creating "deploy.yaml": pods "web-1" is forbidden: exceeded quota: compute-resources, requested: requests.cpu=2, used: requests.cpu=7, limited: requests.cpu=8。之前同一个 Deployment 在别的命名空间能正常创建。
- gold：命名空间 ResourceQuota 的 CPU 请求额度用尽，新 Pod 超出配额被 apiserver 拒绝并返回 403 Forbidden；需要提高配额或删除不再使用的负载。
- 实际 Top-1：超出命名空间 CPU 配额
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-pod-level-resources/
### e029（网络）
- 输入：nginx error.log：connect() failed (111: Connection refused) while connecting to upstream, client: 10.0.1.7, request: "POST /api/order HTTP/1.1", upstream: "http://10.0.2.9:8080/api/order"；Nginx 返回 502 Bad Gateway，后端进程刚重启过，端口还没开始监听。
- gold：nginx 与上游建立 TCP 连接时被拒绝（111 Connection refused），说明后端进程未监听该端口或已挂掉，属于 proxy_next_upstream 里 error 类型的失败；需要先确认后端进程与监听端口。
- 实际 Top-1：后端端口未监听
- 上下文：https://nginx.org/en/docs/stream/ngx_stream_core_module.html, https://nginx.org/en/docs/http/ngx_http_proxy_module.html, https://nginx.org/en/docs/http/ngx_http_core_module.html, https://nginx.org/en/docs/faq.html, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-service/

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 18 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### 混合 + RRF

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：验证 Webhook 超时阻塞删除
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/kubectl-node-debug/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点亲和/选择器不匹配
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/
- 引用未命中 gold_doc_urls
### e007（Node）
- 输入：kubelet 日志：The node was low on resource: memory. Threshold quantity: 100Mi, available: 42Mi. Killing container with id containerd://a1b2... image: app:1.4. Pod phase 变成 Failed，Pod 被驱逐，业务在两分钟内丢了三批请求。
- gold：节点内存可用量跌破 memory.available 的硬阈值（默认 100Mi），kubelet 进入 MemoryPressure 并直接终止节点上的 Pod（Phase=Failed），不经过 PDB；应扩容节点内存或下调 Pod 内存申请。
- 实际 Top-1：节点内存低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
- 引用未命中 gold_doc_urls
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：imagefs 低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置版本高于插件支持
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：未设 ephemeral-storage 限制
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-default-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/
- 引用未命中 gold_doc_urls
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：磁盘空间耗尽
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/create-logfile-group.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-redo-log.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/configuration/secret/
### e024（资源）
- 输入：Pod 一直 Pending；kubectl describe pod 的 Events：0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod。Pod 声明 requests.memory=8Gi，而三台节点可分配的只剩 5Gi 左右。
- gold：Pod 声明的 requests.memory 超过所有节点当前可分配的内存，调度器无可调度节点，Pod 停在 Pending 并报 Insufficient memory；需降低 requests 或扩容节点。
- 实际 Top-1：内存请求超过节点可分配量
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/
### e025（资源）
- 输入：kubectl describe pod 的 Events：0/5 nodes are available: 5 Insufficient cpu。Pod 的 requests.cpu=4，节点总核数 4 但已有工作负载占用了大部分，节点上 Allocatable 一栏显示 cpu: 3900m。
- gold：Pod 的 requests.cpu 超过节点可分配（Allocatable）的 CPU，调度器找不到满足的节点，报 Insufficient cpu；应调整 requests.cpu 或扩容节点。
- 实际 Top-1：请求超过节点可分配CPU
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-overhead/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/
### e026（资源）
- 输入：Error from server (Forbidden): error when creating "deploy.yaml": pods "web-1" is forbidden: exceeded quota: compute-resources, requested: requests.cpu=2, used: requests.cpu=7, limited: requests.cpu=8。之前同一个 Deployment 在别的命名空间能正常创建。
- gold：命名空间 ResourceQuota 的 CPU 请求额度用尽，新 Pod 超出配额被 apiserver 拒绝并返回 403 Forbidden；需要提高配额或删除不再使用的负载。
- 实际 Top-1：命名空间 CPU 配额不足
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/
### e028（网络）
- 输入：nginx error.log：upstream timed out (110: Connection timed out) while reading response header from upstream, client: 10.0.1.5, request: "GET /api/report HTTP/1.1", upstream: "http://10.0.2.9:8080/api/report"；客户端侧看到 504 Gateway Time-out。
- gold：nginx 从上游读取响应头超过 proxy_read_timeout（默认 60s）仍未收到数据，判定上游超时并向客户端返回 504；根因是后端报表接口处理时间过长或上游卡住。
- 实际 Top-1：上游响应头超时
- 上下文：https://nginx.org/en/docs/http/ngx_http_proxy_module.html, https://nginx.org/en/docs/http/ngx_http_upstream_module.html, https://nginx.org/en/docs/http/ngx_http_upstream_module.html, https://nginx.org/en/docs/http/ngx_http_upstream_module.html, https://nginx.org/en/docs/http/ngx_http_upstream_module.html

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 17 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### + Rerank

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：admission webhook 超时拒绝
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/
- 引用未命中 gold_doc_urls
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点亲和性强制条件无节点满足
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-pods-nodes-using-node-affinity/
### e007（Node）
- 输入：kubelet 日志：The node was low on resource: memory. Threshold quantity: 100Mi, available: 42Mi. Killing container with id containerd://a1b2... image: app:1.4. Pod phase 变成 Failed，Pod 被驱逐，业务在两分钟内丢了三批请求。
- gold：节点内存可用量跌破 memory.available 的硬阈值（默认 100Mi），kubelet 进入 MemoryPressure 并直接终止节点上的 Pod（Phase=Failed），不经过 PDB；应扩容节点内存或下调 Pod 内存申请。
- 实际 Top-1：节点内存低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/resource-management/pod-level-resource-managers/
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：imagefs 可用空间低于阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：表空间文件空间耗尽
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/error-message-elements.html, https://dev.mysql.com/doc/refman/8.0/en/error-handling.html
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 达阈值触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e024（资源）
- 输入：Pod 一直 Pending；kubectl describe pod 的 Events：0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod。Pod 声明 requests.memory=8Gi，而三台节点可分配的只剩 5Gi 左右。
- gold：Pod 声明的 requests.memory 超过所有节点当前可分配的内存，调度器无可调度节点，Pod 停在 Pending 并报 Insufficient memory；需降低 requests 或扩容节点。
- 实际 Top-1：内存请求超过节点可分配量
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
### e025（资源）
- 输入：kubectl describe pod 的 Events：0/5 nodes are available: 5 Insufficient cpu。Pod 的 requests.cpu=4，节点总核数 4 但已有工作负载占用了大部分，节点上 Allocatable 一栏显示 cpu: 3900m。
- gold：Pod 的 requests.cpu 超过节点可分配（Allocatable）的 CPU，调度器找不到满足的节点，报 Insufficient cpu；应调整 requests.cpu 或扩容节点。
- 实际 Top-1：请求超过节点可分配CPU
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-overhead/
### e026（资源）
- 输入：Error from server (Forbidden): error when creating "deploy.yaml": pods "web-1" is forbidden: exceeded quota: compute-resources, requested: requests.cpu=2, used: requests.cpu=7, limited: requests.cpu=8。之前同一个 Deployment 在别的命名空间能正常创建。
- gold：命名空间 ResourceQuota 的 CPU 请求额度用尽，新 Pod 超出配额被 apiserver 拒绝并返回 403 Forbidden；需要提高配额或删除不再使用的负载。
- 实际 Top-1：命名空间 CPU 配额不足
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/
### e036（网络）
- 输入：Redis Cluster 客户端报：CLUSTERDOWN The cluster is down；cluster info 显示 cluster_state:fail，cluster_slots_assigned:12288，cluster_require_full_coverage 保持默认值，运维刚下线了一个 master 且没有做槽位迁移。
- gold：集群有 4096 个哈希槽没有节点覆盖，且 cluster-require-full-coverage 默认是 yes，集群进入 fail 状态并停止写操作（CLUSTERDOWN）；需要补齐槽位或迁移故障 master 的槽。
- 实际 Top-1：槽位未全覆盖致集群 fail
- 上下文：https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/, https://redis.io/docs/latest/operate/oss_and_stack/management/scaling/, https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/, https://redis.io/docs/latest/operate/oss_and_stack/management/scaling/, https://redis.io/docs/latest/operate/oss_and_stack/management/scaling/
### e037（资源）
- 输入：MySQL 应用日志：ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction；SHOW ENGINE INNODB STATUS 的 LATEST DETECTED DEADLOCK 段显示两个事务按相反顺序更新 t_order 与 t_order_item。
- gold：两个事务以相反顺序加锁形成循环等待，InnoDB 检测到死锁并回滚其中一个事务（ERROR 1213）；应用应按统一顺序访问资源并对死锁错误做重试。
- 实际 Top-1：多表加锁顺序相反
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-locks-set.html, https://dev.mysql.com/doc/refman/8.0/en/internal-locking.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-locking.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html
### e038（资源）
- 输入：MySQL 报：ERROR 1205 (HY000): Lock wait timeout exceeded; try restarting transaction；事务在等 t_account 上的一行锁超过 innodb_lock_wait_timeout（默认 50 秒）后被回滚，业务侧只有这一条语句失败，整个事务并没有被杀死。
- gold：事务等待行锁超过 innodb_lock_wait_timeout 而超时，InnoDB 只回滚当前语句（ERROR 1205）；与死锁不同，锁等待超时在高并发下是正常现象，应用应重试该语句。
- 实际 Top-1：锁等待超时仅回滚语句
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-locking.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-locks-set.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-redo-log.html

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 13 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### + Query 理解（full）

### e002（Pod）
- 输入：kubectl get pod payment-5c8b 反复重启，Last State 显示 Terminated，Reason: OOMKilled，Exit Code: 137；kubectl describe pod 里 Limits: memory: 256Mi，容器实际 RSS 在压测时涨到 600Mi 后被杀。应用日志最后一行是处理 200MB 上传文件时的 buffer 分配。
- gold：容器内存实际用量超过 limits.memory（256Mi），内核 OOM 机制触发并杀死容器进程（Exit Code 137 / OOMKilled），随后被 kubelet 按重启策略重建；需要调大 limits.memory 或修掉大对象缓存。
- 实际 Top-1：内存超限被终止
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-default-namespace/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/
### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：Webhook 超时阻断删除
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/
- 引用未命中 gold_doc_urls
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点亲和/选择器不匹配
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
- 实际 Top-1：imagefs 可用空间低于阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：磁盘空间耗尽
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/error-message-elements.html, https://dev.mysql.com/doc/refman/8.0/en/error-handling.html
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 达阈值触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e024（资源）
- 输入：Pod 一直 Pending；kubectl describe pod 的 Events：0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod。Pod 声明 requests.memory=8Gi，而三台节点可分配的只剩 5Gi 左右。
- gold：Pod 声明的 requests.memory 超过所有节点当前可分配的内存，调度器无可调度节点，Pod 停在 Pending 并报 Insufficient memory；需降低 requests 或扩容节点。
- 实际 Top-1：节点内存不足无法满足请求
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
### e025（资源）
- 输入：kubectl describe pod 的 Events：0/5 nodes are available: 5 Insufficient cpu。Pod 的 requests.cpu=4，节点总核数 4 但已有工作负载占用了大部分，节点上 Allocatable 一栏显示 cpu: 3900m。
- gold：Pod 的 requests.cpu 超过节点可分配（Allocatable）的 CPU，调度器找不到满足的节点，报 Insufficient cpu；应调整 requests.cpu 或扩容节点。
- 实际 Top-1：请求超过节点可分配CPU
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-overhead/
### e026（资源）
- 输入：Error from server (Forbidden): error when creating "deploy.yaml": pods "web-1" is forbidden: exceeded quota: compute-resources, requested: requests.cpu=2, used: requests.cpu=7, limited: requests.cpu=8。之前同一个 Deployment 在别的命名空间能正常创建。
- gold：命名空间 ResourceQuota 的 CPU 请求额度用尽，新 Pod 超出配额被 apiserver 拒绝并返回 403 Forbidden；需要提高配额或删除不再使用的负载。
- 实际 Top-1：命名空间 CPU 配额不足
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/
### e036（网络）
- 输入：Redis Cluster 客户端报：CLUSTERDOWN The cluster is down；cluster info 显示 cluster_state:fail，cluster_slots_assigned:12288，cluster_require_full_coverage 保持默认值，运维刚下线了一个 master 且没有做槽位迁移。
- gold：集群有 4096 个哈希槽没有节点覆盖，且 cluster-require-full-coverage 默认是 yes，集群进入 fail 状态并停止写操作（CLUSTERDOWN）；需要补齐槽位或迁移故障 master 的槽。
- 实际 Top-1：槽位未全覆盖导致 fail
- 上下文：https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/, https://redis.io/docs/latest/operate/oss_and_stack/management/scaling/, https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/, https://redis.io/docs/latest/operate/oss_and_stack/management/scaling/, https://redis.io/docs/latest/operate/oss_and_stack/management/scaling/
### e037（资源）
- 输入：MySQL 应用日志：ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction；SHOW ENGINE INNODB STATUS 的 LATEST DETECTED DEADLOCK 段显示两个事务按相反顺序更新 t_order 与 t_order_item。
- gold：两个事务以相反顺序加锁形成循环等待，InnoDB 检测到死锁并回滚其中一个事务（ERROR 1213）；应用应按统一顺序访问资源并对死锁错误做重试。
- 实际 Top-1：多表加锁顺序相反致循环等待
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-locks-set.html, https://dev.mysql.com/doc/refman/8.0/en/internal-locking.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-locking.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 14 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

## 成本与耗时

| 项 | 值 |
|---|---|
| 生成成本合计 | ¥1.176976 |
| judge 成本合计 | ¥0.031522（调用 79 次，缓存命中 53 次） |
| **总花费** | **¥1.208498** |

| 变体 | 条数 | 单条均值 | 单条 P50 | 单条 P95 | 总耗时 |
|---|---|---|---|---|---|
| BM25 only | 50 | ¥0.004927 | ¥0.004916 | ¥0.006306 | 113.5s |
| 向量 only | 50 | ¥0.004975 | ¥0.004982 | ¥0.006448 | 114.1s |
| 混合 + RRF | 50 | ¥0.004975 | ¥0.005188 | ¥0.006172 | 98.6s |
| + Rerank | 50 | ¥0.004353 | ¥0.004662 | ¥0.006214 | 112.1s |
| + Query 理解（full） | 50 | ¥0.004309 | ¥0.004508 | ¥0.006146 | 113.9s |

## 口径与已知差异（读表前必看）

- 五档只在检索链上不同，生成侧是同一套 prompt、同一个模型：
  - BM25 only：bm25_search(k=20) -> 前 5 条 -> generate（无向量、无 RRF、无重排、无 Query 理解）
  - 向量 only：vector_search(k=20) -> 前 5 条 -> generate（无 BM25、无 RRF、无重排、无 Query 理解）
  - 混合 + RRF：retrieve(use_sub_index=False)：向量 20 + BM25 20 -> RRF -> 20 -> 前 5 条 -> generate
  - + Rerank：hybrid 的 20 条 -> rerank -> 5 -> generate（不做子库过滤）
  - + Query 理解（full）：analyze（子库过滤 + 查询扩展）-> retrieve(use_sub_index=True) -> rerank -> 5 -> generate
- 非 full 变体不调 analyze()（QueryPlan 只填 raw/clean），所以基线里没有错误码抽取与查询扩展；这是为了把「Query 理解」的增量隔离出来。
- 无重排的三档（bm25/vector/hybrid）没有与 abstain_threshold 同量纲的分数，不套检索侧闸门，只靠生成侧兜底：它们的误拒率天然偏低，负样本拒答准确率也偏低，对比 full 时要带上这条。
- bm25/vector 的召回池也是 20 条（只把前 5 条喂生成），否则 recall@20 对这两档退化成 recall@5。
- error 样本只计入 errors，不进 Top-1/recall/成本的分母（scoring.summarize 口径）。
- full 的 Query 理解若走 LLM，其 token 不计入 card.cost_cny（analyze 不返回 usage），所以 full 成本略低估；严格对齐请设 QUERY_LLM_ENABLED=0 再跑。
