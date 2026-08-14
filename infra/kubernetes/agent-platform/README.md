# Agent Platform Kubernetes base

该目录是 AP-E7-002 的生产安全基线，不包含真实 Secret、Registry 地址或环境 CIDR。

默认 `kustomization.yaml` 只启用已完成真实生产装配的 Event Worker。Reconciliation Worker 已完成进程与受信 HTTP Cleanup 组合，但 `reconciliation-worker-deployment.yaml` 在 Sandbox Manager Provider/Service 尚未部署前不进入默认 Kustomization。部署前必须：

1. 由现有 CI 构建 `infra/images/backend/Dockerfile`，扫描后以 Registry digest 替换 `agent-platform-backend:dev`。
2. 通过 External Secrets、Sealed Secrets、Vault Agent 或集群 Secret 管理流程创建 `agent-platform-runtime-secrets`；字段名保持 `AP_SECRET_*`，应用内只保存 `secret://env/...` 引用。
3. 把 `agent-platform-runtime-identities` 中的示例 UUID 替换为已登记的稳定 SERVICE subject。
4. 为 PostgreSQL、Redis、Temporal、MinIO 和 OTel 在 overlay 中增加精确 namespaceSelector/ipBlock；base 只限制端口，不猜测生产网段。
5. 先独立执行 Alembic migration Job，再滚动 Event Worker；应用进程不会自动迁移数据库。
6. Event/Reconciliation Worker 通过 ClusterIP metrics Service 暴露 9090/TCP `/metrics`，Prometheus 通过 ServiceMonitor 抓取；只允许 monitoring namespace 入站。`ServiceMonitor`/`PrometheusRule` 需要集群先安装 Prometheus Operator CRD；若组织不使用 Operator，overlay 必须移除这两类资源，改用等价的受控 scrape/rule 配置。PrometheusRule 与最小 Runbook 位于 `prometheus-rules.yaml` 和 `runbooks/agent-platform-workers.md`。

Reconciliation Worker 默认未进入 base Deployment，因此 base 规则只检测已存在 target 的 `up == 0`，避免未启用时永久告警；它不能检测 target 完全消失。启用 Reconciliation Worker 的 overlay 必须增加基于期望 Deployment 的 `kube-state-metrics` 告警，或仅在该 overlay 中加入 `absent(up{service="agent-platform-reconciliation-worker-metrics"})`。Event Worker 是 base 必备进程，其 target 消失由 base `absent(up)` 告警覆盖。

`worker-metrics-ingress` 默认只允许名为 `monitoring` 的 namespace 访问；如果生产 Prometheus 位于其他 namespace，overlay 必须同步修改 namespaceSelector，不得放宽为任意 namespace 或公网 CIDR。

启用 API 的 Artifact 上传/下载组合时，部署 overlay 还必须为 API Pod 配置 `AP_ARTIFACT_MAX_RESERVED_BYTES_PER_TENANT` 和 `AP_ARTIFACT_MAX_RESERVED_COUNT_PER_TENANT`。base ConfigMap 中的数值只作为示例容量基线，生产值应由容量测试确认；不能删除两项配置后绕过 staging/production 启动校验。

Event Worker base 配置 Artifact retention 默认值：API 创建 Artifact 时以 AVAILABLE 默认 30 天固化 `expires_at`，进入 FAILED/REJECTED/EXPIRED 时以默认 7 天固化 `retention_delete_after`；删除失败 1 小时后恢复且最多 3 个 Operation。API 与 Event Worker 必须使用相同的四项 `AP_ARTIFACT_*RETENTION*`/`AP_ARTIFACT_DELETE_RECOVERY_*` 配置。base 没有 API ConfigMap，不在此伪造一份。

Sandbox Manager 的 Workspace 租户预留硬上限使用 `AP_WORKSPACE_MAX_RESERVED_BYTES_PER_TENANT` 和 `AP_WORKSPACE_MAX_RESERVED_COUNT_PER_TENANT`，开发默认值为 `10737418240`（10 GiB）和 `100`。当前 Sandbox Manager Deployment 尚未进入 base，因此不要把这两项误放进 Event/Reconciliation Worker ConfigMap；启用 Sandbox Manager overlay 时必须在其专属 ConfigMap 配置并经容量测试覆盖默认值。ACTIVE 租户 StoragePolicy 只能收紧该部署上限，DISABLED 或不存在时回退部署值。

启用 Reconciliation Worker 时还必须：

1. 为 `reconciliation_worker_subject_id` 登记稳定 SERVICE UUID，并在 Sandbox Manager 的允许主体清单登记同一 UUID。
2. 由组织 Secret 管理流程生成 Ed25519 Key Pair；私钥只注入 Reconciliation Worker，公钥只注入 Sandbox Manager，配置相同 Issuer、`aud=sandbox-manager` 和 `kid`。
3. 单独生成至少 32 随机字节的 Execution Ticket HMAC Key，不得与 Service Token、OIDC、模型或 Checkpoint Key 复用。
4. 把 `AP_SANDBOX_MANAGER_BASE_URL` 替换为可验证 TLS 的集群内地址，并在 overlay 中将 NetworkPolicy 收紧到准确 Namespace/CIDR。
5. 将 `reconciliation-worker-deployment.yaml` 加入部署 overlay，并用现有 CI 产生的 Registry digest 覆盖开发 tag。Service Token TTL 默认 60 秒，最大 300 秒；租户只来自验签 Claim，不配置或信任 `X-Tenant-ID`。
6. 用 `AP_RUN_CAPACITY_DOMAIN_SLOTS` JSON 为每个 Deployment `runtime_target_id` 提供经容量验收的 slots；base UUID/100 只是格式示例，不是生产容量事实。Lease TTL 和 tenant quantum 默认为 300 秒/1，可在 overlay 受控覆盖。

`secret.example.yaml` 和 `download-ingress.example.yaml` 不进入默认 Kustomization。后者只能在 API 已完成数据库、Mock/OIDC、Temporal、Redis 和 MinIO 生产组合后启用；独立 Ingress 关闭 access log，避免 query bearer token 泄漏，并在代理层执行带宽整形。若组织要求保留访问审计，应使用不包含 query string 的专用日志格式，而不是重新启用默认 `$request_uri`。

Reconciliation Worker 已实现 Ed25519 Service Token、tenant Claim 验证、HTTP Cleanup Controller、PostgreSQL/Temporal/Ticket/TenantContextSource 组合；Sandbox Manager 自身仍需真实隔离 Provider、Policy Resolver 和 Provision Token Verifier 后才能部署，因此其 Deployment 不在本基线中伪造。AgentScope Runtime Worker仍缺生产 SessionFactory、RunSpec/Bundle loader、Tool Gateway/Executor 和完整 Temporal Activity 组合并继续 fail-closed。Vault 仅保留 `SecretBackend` adapter 边界，当前实现为 Kubernetes Secret 注入的 `EnvSecretBackend`。
