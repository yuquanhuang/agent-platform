# Agent Platform Kubernetes base

该目录是 AP-E7-002 的生产安全基线，不包含真实 Secret、Registry 地址或环境 CIDR。

默认 `kustomization.yaml` 只启用已完成真实生产装配的 Event Worker。Reconciliation Worker 已完成进程与受信 HTTP Cleanup 组合，但 `reconciliation-worker-deployment.yaml` 在 Sandbox Manager Provider/Service 尚未部署前不进入默认 Kustomization。部署前必须：

1. 由现有 CI 构建 `infra/images/backend/Dockerfile`，扫描后以 Registry digest 替换 `agent-platform-backend:dev`。
2. 通过 External Secrets、Sealed Secrets、Vault Agent 或集群 Secret 管理流程创建 `agent-platform-runtime-secrets`；字段名保持 `AP_SECRET_*`，应用内只保存 `secret://env/...` 引用。
3. 把 `agent-platform-runtime-identities` 中的示例 UUID 替换为已登记的稳定 SERVICE subject。
4. 为 PostgreSQL、Redis、Temporal、MinIO 和 OTel 在 overlay 中增加精确 namespaceSelector/ipBlock；base 只限制端口，不猜测生产网段。
5. 先独立执行 Alembic migration Job，再滚动 Event Worker；应用进程不会自动迁移数据库。

启用 Reconciliation Worker 时还必须：

1. 为 `reconciliation_worker_subject_id` 登记稳定 SERVICE UUID，并在 Sandbox Manager 的允许主体清单登记同一 UUID。
2. 由组织 Secret 管理流程生成 Ed25519 Key Pair；私钥只注入 Reconciliation Worker，公钥只注入 Sandbox Manager，配置相同 Issuer、`aud=sandbox-manager` 和 `kid`。
3. 单独生成至少 32 随机字节的 Execution Ticket HMAC Key，不得与 Service Token、OIDC、模型或 Checkpoint Key 复用。
4. 把 `AP_SANDBOX_MANAGER_BASE_URL` 替换为可验证 TLS 的集群内地址，并在 overlay 中将 NetworkPolicy 收紧到准确 Namespace/CIDR。
5. 将 `reconciliation-worker-deployment.yaml` 加入部署 overlay，并用现有 CI 产生的 Registry digest 覆盖开发 tag。Service Token TTL 默认 60 秒，最大 300 秒；租户只来自验签 Claim，不配置或信任 `X-Tenant-ID`。

`secret.example.yaml` 和 `download-ingress.example.yaml` 不进入默认 Kustomization。后者只能在 API 已完成数据库、Mock/OIDC、Temporal、Redis 和 MinIO 生产组合后启用；独立 Ingress 关闭 access log，避免 query bearer token 泄漏，并在代理层执行带宽整形。若组织要求保留访问审计，应使用不包含 query string 的专用日志格式，而不是重新启用默认 `$request_uri`。

Reconciliation Worker 已实现 Ed25519 Service Token、tenant Claim 验证、HTTP Cleanup Controller、PostgreSQL/Temporal/Ticket/TenantContextSource 组合；Sandbox Manager 自身仍需真实隔离 Provider、Policy Resolver 和 Provision Token Verifier 后才能部署，因此其 Deployment 不在本基线中伪造。AgentScope Runtime Worker仍缺生产 SessionFactory、RunSpec/Bundle loader、Tool Gateway/Executor 和完整 Temporal Activity 组合并继续 fail-closed。Vault 仅保留 `SecretBackend` adapter 边界，当前实现为 Kubernetes Secret 注入的 `EnvSecretBackend`。
