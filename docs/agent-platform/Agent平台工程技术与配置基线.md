# Agent 平台工程技术与配置基线

> 文档版本：V2.0
> 文档状态：开发输入基线

## 1. 固定选型

| 领域 | V1 固定选择 |
|---|---|
| 架构 | 模块化单体 API + 独立 Worker/Manager 进程 |
| Python | CPython 3.12 |
| 包管理 | uv、`pyproject.toml`、提交 `uv.lock` |
| Web API | FastAPI、Pydantic v2 |
| 数据访问 | SQLAlchemy 2.x Async、asyncpg、Alembic |
| 数据库 | PostgreSQL 16+，共享数据库 + tenant_id；关键表启用 RLS 作为防御层 |
| 工作流 | Temporal Python SDK |
| Runtime | AgentScope 2.0.x；Codex ACP STDIO |
| 缓存/通知 | Redis 7+ |
| 对象存储 | S3 API；本地 MinIO |
| 身份 | OIDC/OAuth2；开发/测试使用 Mock，生产 Provider 通过配置适配 |
| Secret | SecretBackend 接口；开发加密本地实现，生产 Vault/KMS Adapter |
| 可观测性 | OpenTelemetry SDK/Collector、Prometheus、结构化 JSON 日志 |
| 后端质量 | Ruff、Pyright strict、pytest、coverage |
| 前端 | TypeScript、Vue 3 Composition API、Vite、Vue Router 4、Pinia、`@tanstack/vue-query`、Element Plus |
| 前端质量 | ESLint、Prettier、`vue-tsc --noEmit`、Vitest、Vue Test Utils、Playwright |
| API Client | 从 OpenAPI 生成，禁止手写重复 DTO |
| 容器 | OCI Image、固定 Digest、SBOM、签名 |
| 生产 Sandbox | Kubernetes Job/Pod + gVisor；不支持环境可通过安全 ADR 使用 Kata 等价方案 |

依赖的精确 patch 版本由 `uv.lock`/前端 lockfile 固定。升级不得绕过契约、Replay 和兼容测试。

## 2. 进程边界

```text
api
temporal-worker-control
temporal-worker-run
runtime-worker-agentscope
runtime-worker-codex
event-worker
sandbox-manager
reconciliation-worker
web
```

开发环境可以在一个 Compose 项目运行，但进程边界和服务身份保持独立。生产可按容量水平扩展。

## 3. Python 包边界

```text
backend/packages/contracts
  不依赖 domain/application/infrastructure

backend/packages/domain
  只依赖 contracts 和标准库

backend/packages/application
  依赖 domain/contracts 和抽象 Port

backend/packages/infrastructure
  实现 DB/Redis/S3/Temporal/Secret 等 Port

backend/packages/runtimes
  实现 RuntimeAdapter，不访问业务 ORM

backend/apps/*
  负责组装依赖和进程启动
```

所有 Python 业务代码、迁移和测试位于 `backend/`；所有 Vue 前端代码和测试位于 `frontend/`。仓库根目录不得建立平行业务包。

禁止循环依赖和跨模块直接导入内部实现。模块对外只暴露 `public.py` 或明确包出口。

## 4. 类型和代码风格

`pyproject.toml` 最低规则：

```text
Python target = py312
Ruff: E/F/I/UP/B/SIM/ASYNC/RUF
Pyright: strict
pytest asyncio mode = auto
Black/Ruff line length = 88
```

- 公共接口禁止未标注类型。
- Pydantic Model 默认 `extra='forbid'`。
- OpenAPI/JSON Schema 对象默认禁止未声明字段；使用 `allOf` 时不得将带 `additionalProperties: false` 的请求 Schema 直接复用为增加响应字段的基类。
- Domain 使用 dataclass/enum/value object，不使用 ORM Model 作为领域对象。
- 时间通过 Clock Port 获取；Workflow 使用 Temporal 时间 API。
- UUID/Token/Nonce 通过专用 Port 生成，便于测试。

## 5. 配置层级

```text
代码默认值
< 配置文件/环境变量中的非敏感环境配置
< 部署平台配置
< 租户 Policy 上限
< 已发布 Snapshot 固定配置
< Run 时只能进一步收窄的有效策略
```

环境变量只保存启动配置和 Secret Reference，不保存普通业务资源定义。

## 6. 配置键

最低配置：

```text
AP_ENV
AP_SERVICE_NAME
AP_PUBLIC_BASE_URL
AP_AUTH_MODE
AP_DATABASE_DSN_REF
AP_REDIS_DSN_REF
AP_OBJECT_STORAGE_ENDPOINT
AP_OBJECT_STORAGE_BUCKET
AP_OBJECT_STORAGE_CREDENTIAL_REF
AP_TEMPORAL_ADDRESS
AP_TEMPORAL_NAMESPACE
AP_SANDBOX_MANAGER_BASE_URL
AP_SANDBOX_MANAGER_REQUEST_TIMEOUT_SECONDS
AP_SANDBOX_MANAGER_ALLOWED_SUBJECT_IDS
AP_INTERNAL_SERVICE_TOKEN_ISSUER
AP_INTERNAL_SERVICE_TOKEN_AUDIENCE
AP_INTERNAL_SERVICE_TOKEN_KEY_ID
AP_INTERNAL_SERVICE_TOKEN_SIGNING_KEY_REF
AP_INTERNAL_SERVICE_TOKEN_VERIFICATION_KEY_REF
AP_INTERNAL_SERVICE_TOKEN_TTL_SECONDS
AP_INTERNAL_SERVICE_TOKEN_CLOCK_SKEW_SECONDS
AP_EXECUTION_TICKET_KEY_REF
AP_OIDC_ISSUER
AP_OIDC_CLIENT_ID
AP_OIDC_CLIENT_SECRET_REF
AP_SECRET_BACKEND
AP_OTEL_EXPORTER_OTLP_ENDPOINT
AP_METRICS_ALLOWED_NETWORKS
AP_WORKER_METRICS_HOST
AP_WORKER_METRICS_PORT
AP_LOG_LEVEL
AP_CONTRACT_BASELINE_ID
AP_ARTIFACT_MAX_RESERVED_BYTES_PER_TENANT
AP_ARTIFACT_MAX_RESERVED_COUNT_PER_TENANT
AP_ARTIFACT_RETENTION_SECONDS
AP_ARTIFACT_FORENSIC_RETENTION_SECONDS
AP_ARTIFACT_DELETE_RECOVERY_DELAY_SECONDS
AP_ARTIFACT_DELETE_RECOVERY_MAX_OPERATIONS
AP_WORKSPACE_MAX_RESERVED_BYTES_PER_TENANT
AP_WORKSPACE_MAX_RESERVED_COUNT_PER_TENANT
AP_RUN_CAPACITY_DOMAIN_SLOTS
AP_RUN_CAPACITY_LEASE_TTL_SECONDS
AP_RUN_CAPACITY_TENANT_QUANTUM
```

`*_REF` 指向 Secret Backend，不包含明文值。启动时必须校验 `AP_CONTRACT_BASELINE_ID` 与部署 Bundle 一致。

API 和独立 Worker 必须使用各自的 Prometheus `CollectorRegistry`，不依赖 Python 全局默认 Registry。指标暴露只允许受控内网或 Kubernetes 监控身份访问；端口、抓取路由和网络策略由部署配置管理，不进入公开业务 OpenAPI。`AP_METRICS_ALLOWED_NETWORKS` 继续控制 API `/metrics` 来源；独立 Worker 使用固定 `/metrics`，`AP_WORKER_METRICS_HOST`/`AP_WORKER_METRICS_PORT` 开发默认为 `127.0.0.1:9090`，Kubernetes 只能在 NetworkPolicy 和监控抓取边界同时生效时受控覆盖为 Pod 可访问地址。Worker 抓取不得通过暴露 API 管理员凭证解决。

Metric Label 只允许固定枚举或有明确上界的 `process/outcome/method/status_class/provider/mode/runtime_type` 等维度。`tenant_id`、`run_id`、`workflow_id`、`session_id`、`sandbox_id`、Prompt、文件名、具体模型标识和用户输入只允许在脱敏 Trace/Log 中关联，不得作为长期 Metrics Label。

当前 Kubernetes base 只为 Event Worker 以及可选的 Reconciliation Worker 定义受控 metrics Service/ServiceMonitor。API 虽提供 CIDR 限制的 `/metrics`，但 base 尚无 API Deployment/Service；API、Temporal Worker、Sandbox Manager 和 Runtime Worker 必须在各自生产 Deployment 进入 overlay 时同步补齐受控 scrape 目标，未接入前不得把对应 API/Event/SSE 或进程指标声明为生产可告警。Prometheus Operator CRD（或等价受控 scrape/rule 系统）是部署前置。

启用 Artifact 对象存储时，staging/production 必须配置两项租户级硬上限。Sandbox Manager 的 Workspace 部署默认上限为 10 GiB/100 个，可通过两项 `AP_WORKSPACE_*` 配置覆盖。durable StoragePolicy 将 Workspace/Artifact 作为分离额度池，ACTIVE 版本只能逐维收紧部署值，DISABLED/不存在时回退部署值；两条准入均统计 `DELETED` 以外预留并写入策略版本审计。

Event Worker 每轮先分批回收 `upload_expires_at` 已过期的 `UPLOADING`，再处理 AVAILABLE 到期、Retention purge、失败删除恢复和删除 Outbox。AVAILABLE 的 `expires_at` 在创建时按默认 30 天固化；FAILED/REJECTED/EXPIRED 的 `retention_delete_after` 在进入取证状态时按默认 7 天固化。删除恢复默认延迟 1 小时、最多 3 个 Operation。API 和 Event Worker 必须注入相同四项 Retention 配置，已固化的 Artifact deadline 不随配置更改。

Reconciliation Worker 使用 `AP_RUN_CAPACITY_DOMAIN_SLOTS` JSON 把 `runtime_target_id` 映射到明确正整数 slots；staging/production 不得空配置或根据 Worker 数量猜测容量。Lease TTL 默认 300 秒，tenant quantum 默认 1；只有 Reconciliation Worker 需要这三项运行配置。

Reconciliation Worker 必须配置稳定 SERVICE subject、Sandbox Manager 内部地址、Ed25519 签名私钥引用和独立 Execution Ticket HMAC Key 引用；Sandbox Manager 配置同一 Issuer/Audience/Key ID、验证公钥引用和允许的 SERVICE subject。签名/验证 Key 不得复用模型、OIDC、Checkpoint 或 Execution Ticket Key。

`AP_AUTH_MODE=mock` 仅允许 local/test，提供固定的 `sub`、tenant、role 和 `membership_version` Claim；staging/production 启动时必须拒绝 Mock，并要求配置真实 OIDC Issuer/Claim 映射。

## 7. 租户隔离实现

V1 使用共享 PostgreSQL 数据库：

1. 所有租户表强制 tenant_id NOT NULL。
2. Repository API 强制 TenantContext。
3. 关键表配置 PostgreSQL RLS，连接事务设置当前 tenant context。
4. 后台跨租户任务使用受审计的服务身份和显式 tenant 循环，不使用无条件全表业务查询。
5. Redis Key、S3 Key、Workflow ID、Sandbox 名称均以 tenant 隔离。

RLS 是防御层，不代替应用授权和 Repository 条件。

## 8. 数据库 Session

- API 每请求一个 AsyncSession。
- Activity 每次调用一个 AsyncSession。
- `expire_on_commit=False`，禁止隐式 Lazy Load。
- Repository 查询显式 selectin/join 策略。
- 事务由 Application UnitOfWork 管理。
- 不在 DB 事务中调用 Model、MCP、Object Storage、Temporal 或 Sandbox。

## 9. API 实现

- Router 只处理协议转换、认证上下文和 Use Case 调用。
- Use Case 返回 Contracts Model，不返回 ORM。
- 中间件生成 request_id/trace_id，并统一错误映射。
- `Idempotency-Key` 在 Use Case 副作用前处理。
- ETag 格式固定为 `W/"<resource_version>"`。
- 所有响应携带 `X-Request-ID`。
- 列表 Cursor 使用签名或不可伪造编码，包含排序键和过滤摘要。

## 10. Model Gateway 首期 Adapter

V1 必须实现 OpenAI、Qwen、DeepSeek 三个 Provider Adapter，但平台内部不直接依赖供应商请求类型。Adapter 可以复用 OpenAI-compatible 传输实现，供应商标识、模型能力、错误映射、限流、Token 统计和费用表必须分别配置和测试。

统一能力：

```text
chat/response streaming
tool calling
structured output capability flag
reasoning capability flag
usage normalization
timeout/retry/rate limit
token and cost accounting
provider request id
```

Provider Adapter 只位于 Model Gateway，不散落在 Runtime 和业务模块。

## 11. AgentScope 集成

- AgentScope 对象只存在于 AgentScopeRuntimeAdapter 内部。
- AgentScope 版本范围固定为 `>=2.0,<2.1`，精确 patch 由 `uv.lock` 和运行镜像 Digest 固定。
- 平台 RunSpec 编译为 AgentScope 输入；AgentScope 原始事件转换为 RuntimeEventCandidate。
- 模型访问统一经过 Model Gateway。
- AgentScope 的 Memory/Session 不作为平台 Session 事实来源。
- AgentScope 版本通过 `uv.lock` 精确固定，升级必须运行 RuntimeAdapter Contract Test。

## 12. Codex 集成

- V1 只使用 ACP STDIO，不解析 CLI 展示文本。
- 每个 Deployment/Sandbox 使用独立 CODEX_HOME。
- ACP 协议版本和 Codex 可执行文件 Digest 记录在 Runtime Target 和部署清单。
- 进程启动、stdin/stdout、取消和终止由 Codex Runtime Worker + Sandbox Manager 控制。
- 无法确认恢复安全时不重复 prompt，返回明确可重试/不可重试结果。

## 13. 本地开发环境

Docker Compose 必须包含：

```text
postgres
redis
minio
temporal
temporal-ui
otel-collector
api
workers
sandbox-manager development provider
web
```

本地 Sandbox Provider 可以使用受限 Docker/进程实现，但只能由 Sandbox Manager 访问，并保持与生产相同接口和安全测试。不得让 API 直接执行本地命令。

## 14. 生产部署

- API、Temporal Worker、Runtime Worker、Event Worker、Sandbox Manager 使用独立 ServiceAccount。
- AgentScope/Codex 独立 Deployment 和 Task Queue。
- PostgreSQL、Temporal、Object Storage 采用持久化和备份方案。
- Redis 可丢通知但不可导致事实事件丢失。
- 所有 Pod 配置 requests/limits、PDB、探针和滚动策略。
- Schema Migration 使用独立 Job，应用启动不自动执行生产迁移。

## 15. 健康检查

```text
/health/live   进程存活，不检查远端依赖
/health/ready  当前服务必需依赖和契约版本可用
/metrics       Prometheus，受内部网络和身份保护
```

Runtime Target、Model Provider、MCP 和 Sandbox Provider 使用业务健康检查，不混入 API Pod liveness。

## 16. 依赖与供应链

- 使用私有或受控 PyPI/npm/OCI 源。
- 提交 lockfile，不允许生产构建动态解析未锁定依赖。
- CI 生成 SBOM，执行漏洞、许可证和 Secret 扫描。
- 构建器、基础镜像和依赖固定 Digest/Hash。
- 高危漏洞例外需要风险接受人、截止时间和补偿措施。

## 17. 默认 ADR 结论

| 事项 | V1 默认结论 |
|---|---|
| 服务拆分 | 模块化单体 API，Worker/Manager 独立进程 |
| 租户数据库 | 共享库 tenant_id + 关键表 RLS |
| 类型检查 | Pyright strict |
| Secret | 接口抽象，生产 Vault/KMS Adapter |
| Sandbox | Run 默认；生产 K8s + gVisor |
| Event 分区 | PostgreSQL 按 recorded_at 月分区 |
| 前端 | Vue 3 + TypeScript + Vite；Vue Router 4、Pinia、`@tanstack/vue-query`、Element Plus；OpenAPI 生成 Client |
| 知识库 | V1 增量默认 PostgreSQL + pgvector Adapter，可替换 |
| Codex | ACP STDIO，独立 Worker/CODEX_HOME |

前端框架决策以 [ADR-001：前端框架采用 Vue 3](./ADR-001-前端框架采用Vue3.md) 为准，组件库以 [ADR-002：前端 UI 组件库采用 Element Plus](./ADR-002-前端UI组件库采用ElementPlus.md) 为准。只有部署环境确实无法满足时才创建新 ADR；不得由单个 AI Coding 任务隐式改变这些结论。
