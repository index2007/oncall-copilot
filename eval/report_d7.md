# On-call Copilot 评测报告（M10）

| 项 | 值 |
|---|---|
| 运行时间 | 2026-11-07 10:38:23 |
| 数据集 | C:\Users\chuan\Documents\deepseek-harness\default-workspace\oncall-copilot\eval\dataset.jsonl（50 条：正 40 / 负 10） |
| 变体 | BM25 only、向量 only、混合 + RRF、+ Rerank、+ Query 理解（full） |
| 生成模型 | deepseek-flash（thinking=disabled，temperature=0.0） |
| judge | llm（缓存 eval/.judge_cache.json） |
| 流式 | 是：open_stream + finalize_stream，TTFT 从请求开始量 |

## §6.3 对比表

| 变体 | Top-1 | Top-3 | recall@20 | 引用命中 | 误拒率 | 拒答准确率 | 首字 P50 (ms) | 整卡 P50 (ms) | 整卡 P95 (ms) | 成本/次 |
|---|---|---|---|---|---|---|---|---|---|---|
| BM25 only | 97.5% | 100.0% | 95.0% | 90.0% | 0.0% | 90.0% | 740 | 2657 | 3377 | ¥0.0073 |
| 向量 only | 95.0% | 95.0% | 95.0% | 84.2% | 5.0% | 90.0% | 680 | 2550 | 3329 | ¥0.0072 |
| 混合 + RRF | 97.5% | 97.5% | 97.5% | 92.3% | 2.5% | 90.0% | 738 | 2948 | 3350 | ¥0.0074 |
| + Rerank | 90.0% | 90.0% | 97.5% | 89.2% | 7.5% | 90.0% | 1183 | 3258 | 4040 | ¥0.0065 |
| + Query 理解（full） | 92.5% | 92.5% | 97.5% | 89.2% | 7.5% | 90.0% | 1233 | 3251 | 4230 | ¥0.0065 |

> Top-1/Top-3 由 LLM-as-judge（关键词先判，漏判才调模型） 判定；失败明细仍按关键词判定，两者可能有个别样本不一致。
>
> 列口径：**recall@20 只看召回池、分母是全部正样本**（生成之前的事，拒答不该拖累它）；**引用命中只看答出来的卡、分母是作答样本数**（拒答的卡没有 citations 可核验）。两者都把拒答样本的处置交给误拒率一列。

## 失败明细（Top-1 未命中或引用未命中）

### BM25 only

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：验证 Webhook 超时阻塞删除
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-pod-namespace/
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点标签不匹配选择器
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
- 实际 Top-1：imagefs 可用空间低于硬阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
### e011（Node）
- 输入：新增的第三台 worker 在 kubectl get nodes 里始终看不到；节点上 systemctl status kubelet 是 activating，journalctl -u kubelet 里反复出现 failed to run Kubelet: unable to load bootstrap kubeconfig，证书签名请求也没出现在集群里。
- gold：kubelet 无法启动（bootstrap kubeconfig 加载失败），因此没有向 apiserver 注册，节点不会出现在节点列表里；需按节点注册与证书流程排查 kubelet 配置。
- 实际 Top-1：bootstrap kubeconfig 缺失或无效
- 上下文：https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/concepts/cluster-administration/logging/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/dns-debugging-resolution/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/topology/
- 引用未命中 gold_doc_urls
### e012（网络）
- 输入：kubectl get pods 报：Unable to connect to the server: dial tcp 10.0.0.12:6443: i/o timeout；换一台机器执行同样的命令是正常的，这台机器能 ping 通 10.0.0.12，但没有配代理。
- gold：客户端到 apiserver 的 6443 端口连不通（dial tcp i/o timeout），常见原因是 kubeconfig 指向的地址错误、本机到集群的网络/VPN 不通或代理设置拦截；需用 kubectl 连接排查流程逐项检查。
- 实际 Top-1：本机 kubectl 配置或版本异常
- 上下文：https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/windows/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/dns-debugging-resolution/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-service/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置版本高于插件支持
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://dev.mysql.com/doc/refman/8.0/en/create-logfile-group.html, https://nginx.org/en/docs/http/ngx_http_ssl_module.html
### e017（存储）
- 输入：Pod 在 node-02 上一直 ContainerCreating；kubectl describe pod 的 Events：Multi-Attach error for volume "pvc-3f9c..." Volume is already exclusively attached to one node and can't be attached to another；该 PVC 的 accessModes 是 ReadWriteOnce，旧 Pod 还残留在 node-01 上。
- gold：ReadWriteOnce 卷只允许被单个节点以读写方式挂载，旧 Pod 还占用着该卷，新 Pod 调度到另一节点后无法再次挂载，报 Multi-Attach；需先确认旧 Pod 已终止或改用支持多节点的访问模式。
- 实际 Top-1：RWO 卷被旧 Pod 独占
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/, https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/, https://kubernetes.io/zh-cn/docs/concepts/storage/volume-health-monitoring/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/volume-populators-and-data-sources/
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：未设 ephemeral-storage 请求/限制
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/policy/resource-quotas/, https://kubernetes.io/zh-cn/docs/concepts/policy/resource-quotas/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
- 引用未命中 gold_doc_urls
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：磁盘空间耗尽导致建表失败
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/binary-log.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-redo-log.html, https://dev.mysql.com/doc/refman/8.0/en/error-message-elements.html, https://dev.mysql.com/doc/refman/8.0/en/create-logfile-group.html
### e019（镜像）
- 输入：kubectl describe pod 的 Events：Failed to pull image "registry.internal/app:1.4.2": rpc error: code = NotFound desc = failed to pull and unpack image: failed to resolve reference: manifest unknown；随后 Back-off pulling image，Pod 状态 ImagePullBackOff，而同镜像的 1.4.1 能拉下来。
- gold：镜像 tag 在仓库里不存在（manifest unknown），kubelet 无法拉取，Pod 进入 ImagePullBackOff；应确认镜像名与 tag 是否真的推送过，必要时在节点上手动拉一次验证。
- 实际 Top-1：镜像 tag 1.4.2 不存在
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/containers/container-lifecycle-hooks/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/
- 引用未命中 gold_doc_urls
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 满触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/configuration/secret/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 22 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### 向量 only

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：准入 webhook 超时阻塞删除
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/kubectl-node-debug/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/
- 引用未命中 gold_doc_urls
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点亲和/选择器无匹配节点
- 上下文：https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/topology/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-scheduling-readiness/
- 引用未命中 gold_doc_urls
### e007（Node）
- 输入：kubelet 日志：The node was low on resource: memory. Threshold quantity: 100Mi, available: 42Mi. Killing container with id containerd://a1b2... image: app:1.4. Pod phase 变成 Failed，Pod 被驱逐，业务在两分钟内丢了三批请求。
- gold：节点内存可用量跌破 memory.available 的硬阈值（默认 100Mi），kubelet 进入 MemoryPressure 并直接终止节点上的 Pod（Phase=Failed），不经过 PDB；应扩容节点内存或下调 Pod 内存申请。
- 实际 Top-1：内存低于驱逐阈值被驱逐
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：imagefs 低于硬驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e011（Node）
- 输入：新增的第三台 worker 在 kubectl get nodes 里始终看不到；节点上 systemctl status kubelet 是 activating，journalctl -u kubelet 里反复出现 failed to run Kubelet: unable to load bootstrap kubeconfig，证书签名请求也没出现在集群里。
- gold：kubelet 无法启动（bootstrap kubeconfig 加载失败），因此没有向 apiserver 注册，节点不会出现在节点列表里；需按节点注册与证书流程排查 kubelet 配置。
- 实际 Top-1：bootstrap kubeconfig 缺失或不可用
- 上下文：https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/monitor-node-health/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/
- 引用未命中 gold_doc_urls
### e012（网络）
- 输入：kubectl get pods 报：Unable to connect to the server: dial tcp 10.0.0.12:6443: i/o timeout；换一台机器执行同样的命令是正常的，这台机器能 ping 通 10.0.0.12，但没有配代理。
- gold：客户端到 apiserver 的 6443 端口连不通（dial tcp i/o timeout），常见原因是 kubeconfig 指向的地址错误、本机到集群的网络/VPN 不通或代理设置拦截；需用 kubectl 连接排查流程逐项检查。
- 实际 Top-1：本机 kubectl 配置异常
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-service/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-service/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置版本高于插件支持
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/
### e017（存储）
- 输入：Pod 在 node-02 上一直 ContainerCreating；kubectl describe pod 的 Events：Multi-Attach error for volume "pvc-3f9c..." Volume is already exclusively attached to one node and can't be attached to another；该 PVC 的 accessModes 是 ReadWriteOnce，旧 Pod 还残留在 node-01 上。
- gold：ReadWriteOnce 卷只允许被单个节点以读写方式挂载，旧 Pod 还占用着该卷，新 Pod 调度到另一节点后无法再次挂载，报 Multi-Attach；需先确认旧 Pod 已终止或改用支持多节点的访问模式。
- 实际 Top-1：RWO 卷被旧 Pod 独占
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/concepts/storage/ephemeral-volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：表空间/磁盘写满
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/create-logfile-group.html, https://dev.mysql.com/doc/refman/8.0/en/binary-log.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/binary-log.html
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 满触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/
### e024（资源）
- 输入：Pod 一直 Pending；kubectl describe pod 的 Events：0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found for incoming pod。Pod 声明 requests.memory=8Gi，而三台节点可分配的只剩 5Gi 左右。
- gold：Pod 声明的 requests.memory 超过所有节点当前可分配的内存，调度器无可调度节点，Pod 停在 Pending 并报 Insufficient memory；需降低 requests 或扩容节点。
- 实际 Top-1：内存请求超过节点可分配量
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
### e025（资源）
- 输入：kubectl describe pod 的 Events：0/5 nodes are available: 5 Insufficient cpu。Pod 的 requests.cpu=4，节点总核数 4 但已有工作负载占用了大部分，节点上 Allocatable 一栏显示 cpu: 3900m。
- gold：Pod 的 requests.cpu 超过节点可分配（Allocatable）的 CPU，调度器找不到满足的节点，报 Insufficient cpu；应调整 requests.cpu 或扩容节点。
- 实际 Top-1：请求超过节点可分配CPU
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/resize-container-resources/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/pod-overhead/
- 引用未命中 gold_doc_urls

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 20 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### 混合 + RRF

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：验证 webhook 超时阻塞删除
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
- 实际 Top-1：内存低于硬驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/
### e008（Node）
- 输入：node-07 上 kubelet 持续报：Image garbage collection failed once. Possible ... imagefs.available 8% (< 15%)；kubectl describe node 里 Conditions: DiskPressure=True，新 Pod 调度到该节点后马上被驱逐。
- gold：节点 imagefs 可用空间低于默认硬阈值 15%，触发 DiskPressure，kubelet 开始回收镜像并驱逐 Pod；根因是容器镜像与容器可写层占满数据盘。
- 实际 Top-1：imagefs 低于驱逐阈值
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/
### e010（Node）
- 输入：kubectl describe node node-05 显示 Taints: node.kubernetes.io/not-ready:NoExecute；节点上的 Pod 在 5 分钟内被大量驱逐，kubectl get node 里 STATUS 是 NotReady，kubelet 已停止上报心跳。
- gold：节点失联后被打上 node.kubernetes.io/not-ready 污点，该内置污点默认 effect 是 NoExecute，因此节点上的 Pod 被自动驱逐；恢复节点 Ready 状态或为 Pod 配置相应容忍。
- 实际 Top-1：kubelet 停止上报心跳致 NotReady
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/taint-and-toleration/
### e011（Node）
- 输入：新增的第三台 worker 在 kubectl get nodes 里始终看不到；节点上 systemctl status kubelet 是 activating，journalctl -u kubelet 里反复出现 failed to run Kubelet: unable to load bootstrap kubeconfig，证书签名请求也没出现在集群里。
- gold：kubelet 无法启动（bootstrap kubeconfig 加载失败），因此没有向 apiserver 注册，节点不会出现在节点列表里；需按节点注册与证书流程排查 kubelet 配置。
- 实际 Top-1：bootstrap kubeconfig 缺失或无效
- 上下文：https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/monitor-node-health/, https://kubernetes.io/zh-cn/docs/concepts/cluster-administration/logging/
- 引用未命中 gold_doc_urls
### e012（网络）
- 输入：kubectl get pods 报：Unable to connect to the server: dial tcp 10.0.0.12:6443: i/o timeout；换一台机器执行同样的命令是正常的，这台机器能 ping 通 10.0.0.12，但没有配代理。
- gold：客户端到 apiserver 的 6443 端口连不通（dial tcp i/o timeout），常见原因是 kubeconfig 指向的地址错误、本机到集群的网络/VPN 不通或代理设置拦截；需用 kubectl 连接排查流程逐项检查。
- 实际 Top-1：本机到 6443 被拦截
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/troubleshoot-kubectl/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-service/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-service/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置版本高于插件支持
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/migrating-from-dockershim/troubleshooting-cni-plugin-related-errors/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/tutorials/cluster-management/use-pod-level-resource-managers/, https://kubernetes.io/zh-cn/docs/concepts/containers/container-lifecycle-hooks/
### e017（存储）
- 输入：Pod 在 node-02 上一直 ContainerCreating；kubectl describe pod 的 Events：Multi-Attach error for volume "pvc-3f9c..." Volume is already exclusively attached to one node and can't be attached to another；该 PVC 的 accessModes 是 ReadWriteOnce，旧 Pod 还残留在 node-01 上。
- gold：ReadWriteOnce 卷只允许被单个节点以读写方式挂载，旧 Pod 还占用着该卷，新 Pod 调度到另一节点后无法再次挂载，报 Multi-Attach；需先确认旧 Pod 已终止或改用支持多节点的访问模式。
- 实际 Top-1：RWO 卷被旧 Pod 独占
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/ephemeral-volumes/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：未设 ephemeral-storage 请求
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-default-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/
- 引用未命中 gold_doc_urls
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：磁盘空间耗尽导致建表失败
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/create-logfile-group.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-redo-log.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 满导致拉镜像写盘失败
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/configuration/secret/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/image-volumes/

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 23 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### + Rerank

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：准入 webhook 超时阻断删除
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/
- 引用未命中 gold_doc_urls
### e005（Pod）
- 输入：kubectl describe pod 的 Events 显示：Error: configmap "app-config" not found；另一个 Pod 报 Error: couldn't find key DATABASE_URL in ConfigMap default/app-config。容器状态 CreateContainerConfigError，Pod 一直起不来。
- gold：Pod 通过 envFrom / configMapKeyRef 引用的 ConfigMap 或其 key 不存在，且没有标记 optional，kubelet 无法构造容器环境变量，报 CreateContainerConfigError（ConfigMap 不存在时非 optional 引用会使 Pod 起不来）。
- 实际 Top-1：ConfigMap 不存在
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/configure-redis-using-configmap/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/
- 引用未命中 gold_doc_urls
### e006（Pod）
- 输入：kubectl describe pod 显示 0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling。Pod 一直 Pending。
- gold：Pod 的 nodeAffinity 硬约束（requiredDuringSchedulingIgnoredDuringExecution 里的 matchExpressions）与集群现有节点的标签都不匹配，调度器找不到满足条件的节点；应修正节点标签或放宽亲和性规则。
- 实际 Top-1：节点亲和性/选择器无匹配节点
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/configuration/manage-resources-containers/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-pods-nodes-using-node-affinity/
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
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：容器未设 ephemeral-storage 请求
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
- 引用未命中 gold_doc_urls
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：磁盘空间耗尽
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/error-message-elements.html, https://dev.mysql.com/doc/refman/8.0/en/error-handling.html
### e020（镜像）
- 输入：Pod 状态 ImagePullBackOff，Events：Failed to pull image "registry.internal/private/api:v2": 401 Unauthorized: authentication required；Pod spec 里没有 imagePullSecrets，节点上的 containerd 也没有配置仓库凭据。
- gold：拉取私有仓库镜像缺少认证信息，仓库返回 401，kubelet 拉取失败；应创建 kubernetes.io/dockerconfigjson 类型的 Secret 并在 Pod 上通过 imagePullSecrets 引用。
- 实际 Top-1：缺少 imagePullSecrets 凭据
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/
- 引用未命中 gold_doc_urls
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 满导致拉镜像写盘失败
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/
### e023（镜像）
- 输入：Pod 事件：MountVolume.SetUp failed for volume "image-vol" : rpc error: failed to mount image volume: runtime does not support image volumes；Pod 卡在 ContainerCreating。
- gold：该 Pod 使用了镜像卷（volume 来源于 OCI 镜像内容），但节点容器运行时未启用/不支持镜像卷特性，卷挂载失败；需换支持镜像卷的运行时或改用普通卷。
- 实际 Top-1：运行时/kubelet 不支持 image 卷
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/windows/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/containers/container-lifecycle-hooks/

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 20 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

### + Query 理解（full）

### e003（Pod）
- 输入：kubectl delete pod order-svc-0 之后 Pod 一直停在 Terminating 超过 40 分钟；kubectl get pod -o yaml 看到 metadata.finalizers: ["foregroundDeletion"]；apiserver 审计日志里反复出现 admission webhook "validate.order.io" denied the request: Post https://validate.order.io/validate: context deadline exceeded。
- gold：Pod 卡在 Terminating 是因为 finalizer 未清理，同时 ValidatingWebhookConfiguration 的 webhook 不可达，admission 请求超时导致删除流程反复失败；需要先恢复或临时移除该 webhook 配置再删除。
- 实际 Top-1：准入 webhook 超时阻塞删除
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/determine-reason-pod-failure/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/
- 引用未命中 gold_doc_urls
### e005（Pod）
- 输入：kubectl describe pod 的 Events 显示：Error: configmap "app-config" not found；另一个 Pod 报 Error: couldn't find key DATABASE_URL in ConfigMap default/app-config。容器状态 CreateContainerConfigError，Pod 一直起不来。
- gold：Pod 通过 envFrom / configMapKeyRef 引用的 ConfigMap 或其 key 不存在，且没有标记 optional，kubelet 无法构造容器环境变量，报 CreateContainerConfigError（ConfigMap 不存在时非 optional 引用会使 Pod 起不来）。
- 实际 Top-1：ConfigMap app-config 不存在
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/configure-redis-using-configmap/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tutorials/configuration/updating-configuration-via-a-configmap/
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
### e011（Node）
- 输入：新增的第三台 worker 在 kubectl get nodes 里始终看不到；节点上 systemctl status kubelet 是 activating，journalctl -u kubelet 里反复出现 failed to run Kubelet: unable to load bootstrap kubeconfig，证书签名请求也没出现在集群里。
- gold：kubelet 无法启动（bootstrap kubeconfig 加载失败），因此没有向 apiserver 注册，节点不会出现在节点列表里；需按节点注册与证书流程排查 kubelet 配置。
- 实际 Top-1：bootstrap kubeconfig 缺失或不可读
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-cluster/, https://kubernetes.io/zh-cn/docs/setup/production-environment/tools/kubeadm/troubleshooting-kubeadm/, https://kubernetes.io/zh-cn/docs/concepts/workloads/pods/pod-lifecycle/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-pods-nodes-using-node-affinity/
### e015（网络）
- 输入：kubelet 日志：failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox "7f3a...": plugin type="calico" failed (add): incompatible CNI versions; config is "1.0.0", plugin supports ["0.3.0" "0.3.1" "0.4.0"]；Pod 一直 ContainerCreating。
- gold：节点上的 CNI 配置文件版本与已安装的 CNI 插件支持的版本不兼容（containerd 1.6.x 与旧 CNI 配置的已知问题），sandbox 网络创建失败导致 Pod 卡在 ContainerCreating；需升级 CNI 插件并让配置版本匹配。
- 实际 Top-1：CNI 配置与插件版本不兼容
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/concepts/containers/container-lifecycle-hooks/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/cpu-constraint-namespace/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/memory-constraint-namespace/
### e017（存储）
- 输入：Pod 在 node-02 上一直 ContainerCreating；kubectl describe pod 的 Events：Multi-Attach error for volume "pvc-3f9c..." Volume is already exclusively attached to one node and can't be attached to another；该 PVC 的 accessModes 是 ReadWriteOnce，旧 Pod 还残留在 node-01 上。
- gold：ReadWriteOnce 卷只允许被单个节点以读写方式挂载，旧 Pod 还占用着该卷，新 Pod 调度到另一节点后无法再次挂载，报 Multi-Attach；需先确认旧 Pod 已终止或改用支持多节点的访问模式。
- 实际 Top-1：RWO 卷被旧 Pod 独占
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/persistent-volumes/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/manage-resources/quota-memory-cpu-namespace/
### e018（存储）
- 输入：Pod 状态 Evicted；kubectl describe pod 的 Message：The node was low on resource: ephemeral-storage. Container log-writer was using 8Gi, which exceeds its request of 0；该命名空间没有为容器设置 ephemeral-storage 的 requests/limits。
- gold：容器向 emptyDir/日志写入的本地临时存储超过节点可用量，节点进入 ephemeral-storage 压力并驱逐 Pod；因为没给容器声明 ephemeral-storage 的 requests/limits，也没有用 LimitRange/ResourceQuota 兜底。
- 实际 Top-1：容器未设临时存储请求
- 上下文：https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/tasks/administer-cluster/reserve-compute-resources/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-cpu-resource/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/extended-resource/
- 引用未命中 gold_doc_urls
### e039（存储）
- 输入：MySQL 建表失败：ERROR 28 (HY000) at line 1: No space left on device；error log 里跟着 InnoDB: Creating table 'orders' failed，磁盘挂载点的使用率 100%，binlog 与 ibdata 都在同一块盘上。
- gold：磁盘写满（errno 28 No space left on device）导致 InnoDB 无法创建表或扩展表空间；应按建库建表 I/O 失败排查流程确认磁盘空间与磁盘配额，而不是先去调参数。
- 实际 Top-1：表空间/磁盘写满
- 上下文：https://dev.mysql.com/doc/refman/8.0/en/error-interfaces.html, https://dev.mysql.com/doc/refman/8.0/en/error-creating-innodb.html, https://dev.mysql.com/doc/refman/8.0/en/innodb-error-handling.html, https://dev.mysql.com/doc/refman/8.0/en/error-message-elements.html, https://dev.mysql.com/doc/refman/8.0/en/error-handling.html
### e020（镜像）
- 输入：Pod 状态 ImagePullBackOff，Events：Failed to pull image "registry.internal/private/api:v2": 401 Unauthorized: authentication required；Pod spec 里没有 imagePullSecrets，节点上的 containerd 也没有配置仓库凭据。
- gold：拉取私有仓库镜像缺少认证信息，仓库返回 401，kubelet 拉取失败；应创建 kubernetes.io/dockerconfigjson 类型的 Secret 并在 Pod 上通过 imagePullSecrets 引用。
- 实际 Top-1：缺少 imagePullSecrets 凭据
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-running-pod/, https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/
- 引用未命中 gold_doc_urls
### e022（镜像）
- 输入：节点 imagefs 使用率到 92% 后 kubelet 开始回收镜像；紧接着 Deployment 滚动更新，新 Pod 拉取基础镜像报 failed to pull image: no space left on device，ImagePullBackOff，但仓库侧确认镜像存在。
- gold：节点磁盘压力下 kubelet 会删除未使用的容器镜像以回收空间，同时新 Pod 又因空间不足无法写入镜像层，拉取失败；根因是节点 imagefs 空间不足，而不是仓库或 tag 问题。
- 实际 Top-1：imagefs 满触发镜像回收
- 上下文：https://kubernetes.io/zh-cn/docs/concepts/storage/volumes/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/, https://kubernetes.io/zh-cn/docs/tasks/debug/debug-application/debug-pods/, https://kubernetes.io/zh-cn/docs/concepts/scheduling-eviction/node-pressure-eviction/

（已达 10 条上限，其余略）

引用校验：非法 chunk_id 0 条 / 无有效证据的 cause 0 条（生成侧已过滤，非 0 说明有降级或过滤缺口）
本档有 20 条靠 judge 判为命中（关键词未覆盖，如中英混写）；失败明细按关键词判定，这部分样本可能同时出现在上面，属已知差异。

## 成本与耗时

| 项 | 值 |
|---|---|
| 生成成本合计 | ¥1.742988 |
| judge 成本合计 | ¥0.056652（调用 141 次，缓存命中 123 次） |
| **总花费** | **¥1.799640** |

| 变体 | 条数 | 单条均值 | 单条 P50 | 单条 P95 | 总耗时 |
|---|---|---|---|---|---|
| BM25 only | 50 | ¥0.007281 | ¥0.007480 | ¥0.008604 | 155.7s |
| 向量 only | 50 | ¥0.007195 | ¥0.007216 | ¥0.008738 | 141.9s |
| 混合 + RRF | 50 | ¥0.007357 | ¥0.007670 | ¥0.008668 | 161.8s |
| + Rerank | 50 | ¥0.006531 | ¥0.007370 | ¥0.008862 | 159.5s |
| + Query 理解（full） | 50 | ¥0.006496 | ¥0.007410 | ¥0.008630 | 170.0s |

## 口径与已知差异（读表前必看）

- 五档只在检索链上不同，生成侧是同一套 prompt、同一个模型：
  - BM25 only：bm25_search(k=20) -> 前 top_k_final 条 -> generate（无向量、无 RRF、无重排、无 Query 理解）
  - 向量 only：vector_search(k=20) -> 前 top_k_final 条 -> generate（无 BM25、无 RRF、无重排、无 Query 理解）
  - 混合 + RRF：retrieve(use_sub_index=False)：向量 20 + BM25 20 -> RRF -> 20 -> 前 top_k_final 条 -> generate
  - + Rerank：hybrid 的 20 条 -> rerank -> top_k_final -> generate（不做子库过滤）
  - + Query 理解（full）：analyze（子库过滤 + 查询扩展）-> retrieve(use_sub_index=True) -> rerank -> top_k_final -> generate
- 非 full 变体不调 analyze()（QueryPlan 只填 raw/clean），所以基线里没有错误码抽取与查询扩展；这是为了把「Query 理解」的增量隔离出来。
- 无重排的三档（bm25/vector/hybrid）没有与 abstain_threshold 同量纲的分数，不套检索侧闸门，只靠生成侧兜底：它们的误拒率天然偏低，负样本拒答准确率也偏低，对比 full 时要带上这条。
- bm25/vector 的召回池也是 20 条（只把前 top_k_final 条喂生成），否则 recall@20 对这两档退化成 recall@top_k_final。
- error 样本只计入 errors，不进 Top-1/recall/成本的分母（scoring.summarize 口径）。
- full 的 Query 理解若走 LLM，其 token 不计入 card.cost_cny（analyze 不返回 usage），所以 full 成本略低估；严格对齐请设 QUERY_LLM_ENABLED=0 再跑。
