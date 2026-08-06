# Agent 平台 AI Coding 开发节奏与记录

> 本文件是非冻结运行记录，不属于需求、API、事件或数据 Schema 契约。

更新时间：2026-08-06

## 开发节奏

1. 每轮只领取一个 `AP-E<epic>-NNN` 任务，先确认契约、范围和已有测试。
2. 按“契约/门禁 → 后端骨架 → 前端骨架 → 跨端集成”推进；任务验收通过后再进入下一项。
3. 每轮记录改动、依赖、验证结果、阻塞和剩余风险，不把未执行的门禁标记为通过。

## 当前状态

| 顺序 | 任务 | 状态 | 结果 |
| --- | --- | --- | --- |
| 1 | `AP-E0-001` 契约完整性与校验门禁 | ✅ 已完成 | `make contract-check` 已接通并通过；哈希、Schema、Example 负向测试均按预期失败 |
| 2 | `AP-E0-002` 后端基础骨架 | ✅ 已完成 | 已建立应用/包边界、配置、健康检查和独立进程入口骨架 |
| 3 | `AP-E0-003` Vue 3 前端基础骨架 | ✅ 已完成 | Vue/Vite/Element Plus/Router/Pinia/Vue Query 已接通，全仓与契约门禁通过 |
| 4 | `AP-E0-004` Client、DTO 与 RunEvent 生成 | ✅ 已完成 | 16 个 Python/TypeScript 生成文件、167 个 operationId 和 RunEvent 判别联合已接通，零 Diff 门禁通过 |
| 5 | `AP-E0-005` PostgreSQL、SQLAlchemy、Alembic 与 TenantContext | ✅ 已完成 | IAM 基础迁移、Async Session/UoW、TenantContext、RLS/索引及 PostgreSQL 16 集成验证通过 |
| 6 | `AP-E0-006` Mock OIDC、`/me`、Tenant/Member/Role 与基础 RBAC | ✅ 已完成 | Mock 身份、权威 `/me`、Tenant/Member/Role API、RBAC、ETag、幂等、Operation 与审计均已通过全仓门禁 |
| 7 | `AP-E0-007` Temporal、Outbox 与可观测性骨架 | ✅ 已完成 | control/run Worker、Probe Workflow、租户 Outbox、JSON 日志、Trace、Prometheus 和真实集成验证通过 |
| 8 | `AP-E0-008` Epic 0 全栈集成验收与工程基线收口 | ✅ 已完成 | 真实 PostgreSQL/Temporal 零 skip、全仓门禁和 FastAPI/Vite readiness 代理冒烟通过 |
| 9 | `AP-E1-001` 资源定义/版本、ETag、幂等和引用查询公共能力 | ✅ 已完成 | 资源注册中心底座、共享幂等 Hash、显式引用查询 Port、真实 PostgreSQL 验收通过 |
| 后续 | `AP-E1-002` 至 `AP-E9-007` | ⏳ 未开始 | 下一任务进入 Prompt CRUD/版本/发布能力，继续复用本底座 |

## 执行记录

### 2026-08-05 — AP-E0-001

- 新增离线契约校验器，校验 31 个基线哈希、2 份 OpenAPI 3.1、167 个唯一 `operationId`、7 份 Draft 2020-12 Schema、184 个 Schema 引用和 4 个 Golden 示例。
- `harness/project.json` 已将契约目录指向 `docs/agent-platform`，`contract-check` 不再跳过。
- 新增直接依赖 `jsonschema-path`、`jsonschema-specifications`，并同步 `uv.lock`；运行时不联网解析 JSON Schema 元规范。
- 验证通过：`make contract-check`、`make backend-check`、Black、Ruff、`uv lock --check`。
- 负向验证通过：破坏 Hash、Schema、Example 时均返回非零。
- 统一 `make check`：后端通过；前端因尚未初始化、缺少 `frontend/package.json` 而失败，留待 `AP-E0-003` 处理。

### 2026-08-05 — AP-E0-002

- 建立 `backend/apps` 进程边界以及 `contracts/domain/application/infrastructure/runtimes` 包边界。
- 实现 FastAPI 应用工厂、兼容根入口、`/health/live` 和 `/health/ready`；应用启动时校验冻结基线 ID。
- 实现 `AP_*` 强类型配置；staging/production 禁止 Mock Auth，OIDC 模式要求 Issuer、Client ID 和 Secret Reference。
- 预留 Temporal、Runtime、Event、Sandbox、Reconciliation 独立进程入口；未实现入口显式非零退出，不伪报健康。
- 新增 `httpx` 开发依赖并同步 `uv.lock`，用于无外部网络的 ASGI API 测试。
- 验证通过：15 个后端测试、Black、Ruff、Pyright strict、`make backend-check`。
- 统一 `make check`：本轮后端门禁通过；前端仍因缺少 `frontend/package.json` 停止，待 `AP-E0-003` 初始化。

### 2026-08-05 — AP-E0-003

- 初始化 Vue 3、TypeScript、Vite、Vue Router 4、Pinia、Vue Query、Element Plus、Vitest、Vue Test Utils、ESLint 和 Prettier。
- 建立应用壳、响应式导航、设计 Token 和 Element Plus 按需样式入口；项目设计系统已从占位模板更新为真实 Vue/Element Plus 基线。
- 服务端状态由 Vue Query 管理，导航状态由 Pinia 管理；未提前创建 Agent/Prompt/Run 等业务页面或手写业务 DTO。
- 前端通过同源 `/health/ready` 和 Vite 开发代理复用 `AP-E0-002` 后端健康端点，包含超时、取消、HTTP 错误和响应结构校验。
- pnpm 仅批准 `esbuild`、`vue-demi` 两个必要安装脚本；依赖精确版本由 `pnpm-lock.yaml` 固定。
- 验证通过：4 个前端测试、Prettier、ESLint、`vue-tsc`、生产构建、`make frontend-check`。
- 全仓回归通过：`make check` 覆盖后端 15 个测试及前端全部门禁；`make contract-check` 继续通过 31 个基线文件、2 份 OpenAPI、7 份 Schema 和 4 个 Golden 示例校验。
- 安全检查通过：`pnpm audit --audit-level high` 未发现已知漏洞；生产构建主入口约 164 KB（gzip 约 61 KB）。
- 真实跨端联调通过：Vite 代理请求返回后端 `{"status":"ok","service_name":"api"}`。
- `skipLibCheck` 仅用于隔离 Element Plus/Vue Router 上游声明文件兼容问题，项目源码仍启用 TypeScript strict。
- 阻塞与剩余风险：本任务无阻塞；业务 API、DTO、RunEvent/SSE 和鉴权客户端仍由 `AP-E0-004` 按冻结契约统一生成，当前前端仅消费健康检查端点。

### 2026-08-05 — AP-E0-004

- 新增任务包 `harness/tasks/AP-E0-004.yaml`，固定基线 `agent-platform-v1-dev-baseline-2026-08-r5`、允许目录、非目标和验收命令；未修改冻结 OpenAPI、JSON Schema 或基线 SHA-256。
- 新增仓库内离线生成器 `scripts/generate_contracts.py`，从两份冻结 OpenAPI 生成 Python Pydantic DTO/异步 httpx Client，从同一契约生成 TypeScript DTO/fetch Client；生成物提交在 `backend/packages/contracts/generated/` 与 `frontend/src/api/generated/`。
- RunEvent 和 RuntimeEventCandidate 独立从 `schemas/run-event-v1.schema.json` 生成带 `event_type` 判别的 Python `TypeAdapter` 与 TypeScript 联合类型；SSE 只暴露原始流入口，事件归并仍留给后续 Epic 4。
- 新增 `make generate-contracts`、`make generated-check`；`make contract-check` 现在先校验冻结基线，再执行 16 个生成文件的字节级零 Diff 门禁。
- 生成 Client 的共享 transport 统一处理超时、取消、同源凭据、HTTP 错误 `code` 提取、JSON 响应校验和 SSE 原始流入口；不把 Token、Secret 或服务端配置写入生成代码。
- `httpx` 从后端开发依赖调整为运行时依赖并同步 `uv.lock`，因为生成的 Python Client 需要在后端应用中复用；前端生成物由 `.prettierignore` 标记为机器生成，仍通过 TypeScript 编译、ESLint 和 Diff 门禁约束。
- 验证通过：`make contract-check`、`make backend-check`（19 个测试）、`make frontend-check`（7 个测试）、`make check`、`make check-all` 和 16 个生成文件零 Diff。
- 跨阶段协同：健康探针 `/health/ready` 未被强行迁移到业务生成 Client，因为它不在冻结两份 OpenAPI 中；后续 AP-E0-005/006 可直接复用生成 DTO，AP-E0-006 的 `/me` 和租户/RBAC API 应优先接入 `CoreApiClient`/`ResourcesApiClient`。
- 阻塞与剩余风险：无当前阻塞；生成器暂未生成所有 JSON Schema（仅生成 RunEvent 及 OpenAPI 直接引用的资源/Skill/Sandbox 支撑类型），RunSpec 运行时专用校验留给后续 Runtime 任务；SSE 重连、Last-Event-ID 和事件归并仍未在本任务实现。

### 2026-08-05 — AP-E0-005

- 新增任务包 `harness/tasks/AP-E0-005.yaml`，限定为 PostgreSQL 基础设施和 AP-E0-006 所需 IAM 数据基础；不实现 OIDC、`/me`、Tenant/Member/Role API、Policy Service 或其他业务表。
- 新增强类型 `TenantContext`，校验租户/主体 UUID、主体类型、用户 `membership_version`、UTC `auth_time`、request/trace 标识并禁止额外字段；稳定出口位于 contracts，基础设施不反向定义权限上下文。
- 新增 SQLAlchemy 2.x Async Engine/Session Factory，强制 `postgresql+asyncpg`、连接级应用名和 statement timeout，Session 使用 `expire_on_commit=False`、`autoflush=False`。
- 新增 `TenantUnitOfWork`，每个请求或 Activity 独立创建 Session 和事务，并在 Repository 使用前通过参数绑定执行事务级 `set_config('app.current_tenant_id', ...)`；正常提交、异常回滚和资源关闭均有测试。
- 新增独立 Alembic 异步迁移环境；数据库 DSN 必须由部署迁移 Job 解析 `AP_DATABASE_DSN_REF` 后注入，API/Worker 启动不自动迁移，仓库配置不保存明文 DSN。
- 首个迁移建立 `tenant`、`app_user`、`tenant_member`、`role`、`role_binding`，包含 UUID、UTC 时间、RESTRICT 外键、租户唯一约束、`tenant_id + id`/主体/资源索引和复合租户角色外键。
- 按人工确认新增 `tenant_member.membership_version bigint NOT NULL DEFAULT 1` 及最小值 Check；租户状态采用冻结 OpenAPI 的 `DISABLED`，解决数据库设计中 `SUSPENDED` 与外部契约不一致。
- `tenant_member`、`role`、`role_binding` 同时启用 `ENABLE/FORCE ROW LEVEL SECURITY`，Policy 使用缺省关闭的 `current_setting(..., true)`，并同时约束 `USING` 与 `WITH CHECK`；RLS 只作防御层，后续 Repository 仍必须显式带 tenant 条件。
- 离线 Alembic SQL测试覆盖扩展、五表、约束、索引和 RLS；真实 PostgreSQL 16 集成测试覆盖空库升级、跨租户读隐藏、跨租户写拒绝、Policy 清单、降级和重复执行，连续两次通过。
- 为真实验收安装 PostgreSQL 16.9；临时 `agent_platform_test` 数据库已删除，本地服务已停止。Docker Hub 拉取镜像超时，因此未依赖 Docker 镜像完成验证。
- 验证通过：`make backend-check`（41 passed、未配置测试数据库时 1 skipped）、`make contract-check`、真实 PostgreSQL 集成测试 2 次、Ruff、Black、Pyright strict。
- 跨阶段协同：AP-E0-006 直接复用 IAM 基础表、TenantContext 和 TenantUnitOfWork；`role_permission` 因详细设计未显式包含 `tenant_id`，留到 AP-E0-006 随权限存储、membership_version 原子递增和审计事务一起定稿。
- 阻塞与剩余风险：无当前阻塞；Secret Backend 的 DSN Resolver、IAM Repository/API、权限变更审计与缓存失效广播属于 AP-E0-006；生产迁移 Job 编排和凭据注入仍需部署阶段实现。

### 2026-08-06 — AP-E0-006 第一子阶段

- 新增完整任务包 `harness/tasks/AP-E0-006.yaml`，任务仍处于进行中；本子阶段只交付 Mock 身份、权威 `/me` 和 RBAC 数据基础，没有一次性铺开 Tenant/Member/Role 全部 CRUD。
- Mock OIDC 只接受 local/test 的非 Secret 哨兵 `Authorization: Bearer mock`；issuer、external subject、display name、email、platform role、active tenant 和 `membership_version` 全部来自 `AP_MOCK_*` 服务端配置，客户端提交的 tenant/role Header、Query 或 Body 不参与身份构造。
- 新增 `AuthenticatedPrincipal` 和稳定 `PlatformError`，认证时间强制 UTC、active tenant 强制 UUID；staging/production 继续由配置门禁拒绝 Mock。
- 新增统一请求上下文和错误映射：响应携带 `X-Request-ID`，认证、契约校验、404 和内部错误使用冻结 Error Envelope，不返回堆栈、SQL 或其他租户信息。
- 新增 `/api/v1/me`，直接复用冻结生成模型；通过 PostgreSQL 查询平台用户、成员、Tenant 状态和 ACTIVE RoleBinding，返回权威 `role_ids` 与 `membership_version`，不从历史 Token 角色推导最终权限。
- active tenant 成员不存在/停用时返回 403；服务端 Mock `membership_version` 与数据库当前版本不一致时返回 401，确保角色或成员变更后新请求失败关闭。
- 按权限安全契约实现 `resource:action` 解析和已冻结资源/动作白名单；默认 `tenant_admin` 权限集合已形成领域对象，Condition/Owner/Policy 求交集仍留在后续 Policy Service。
- 新增显式 `PlatformUnitOfWork`，仅供平台/bootstrap Repository；租户级 `TenantUnitOfWork` 继续强制事务级 TenantContext。没有加入 optional tenant、伪造 TenantContext 或通用 RLS bypass 开关。
- 新增迁移 `0002_iam_rbac_foundation`：`role_permission` 显式包含 `tenant_id`、同租户复合 FK 和 RLS；同时建立不可变 `audit_log` 字段、Check 与查询索引，Audit 查询/保留仍留 Epic 6。
- 解决文档差异：权限格式采用安全契约示例 `resource:action`；`role_permission.tenant_id` 服从所有租户表固定约束；ETag 后续实现采用冻结 OpenAPI 的强格式 `"rv:<version>"`，不采用工程文档遗留弱格式。
- 真实 PostgreSQL 16 集成验证通过：Alembic 升级/降级、四张租户表 RLS Policy、`/me` 权威角色查询、`membership_version` 变更立即失效和成员停用拒绝。
- 验证通过：`make check-all` 覆盖 R5 冻结契约与 16 个生成文件零 Diff、Black、Ruff、Pyright strict、64 个常规后端测试（未配置数据库时 2 个 PostgreSQL 集成测试明确跳过）、前端 Prettier/ESLint/typecheck、7 个前端测试和生产构建；两个真实 PostgreSQL 集成测试已单独执行通过，`git diff --check` 与 `uv lock --check` 同时通过。
- 下一子阶段：实现 Tenant 平台管理、Member/Role 租户 Repository/Use Case/API，补齐强 ETag、幂等记录、同步终态 Operation、角色绑定变更的 membership_version 原子递增和 Audit 写入。
- 跨阶段协同：不修改冻结契约或前端生成 Client；AP-E0-007 可继续复用 X-Request-ID/trace_id 和追加式 Audit 字段，但 Outbox、Trace Exporter、指标及 Redis 失效广播不得在 IAM Router 中实现。
- 阻塞与剩余风险：无当前阻塞；真实 OIDC/JWKS、平台数据库专用最小权限角色和 Secret Backend DSN Resolver 仍需后续生产身份/部署任务完成。

### 2026-08-06 — AP-E0-006 第二子阶段与任务收口

- 完成冻结 Tenant、Member、Role 和 Operation 共 17 个 API operationId；平台 Tenant 管理要求服务端 `platform_admin`，Member/Role 从服务端 active tenant 构造 `TenantContext`，不读取客户端租户字段。
- 新增 SQLAlchemy IAM Persistence；平台与租户事务边界继续分离，Member/Role 查询显式携带 `tenant_id`，并由 PostgreSQL RLS 二次隔离。
- Member 的展示名和邮箱保存为租户级成员快照，避免租户管理员更新全局 `app_user` 后影响其他租户；`/me` 继续返回平台身份信息。
- 新增 24 小时 `idempotency_record`，规范化 Hash 对 `role_ids`/`permissions` 按集合语义排序；同 Key 同请求重放原响应，不同请求返回 `IDEMPOTENCY_KEY_REUSED`。
- Tenant/Member/Role 使用强 ETag `"rv:<resource_version>"`；版本不匹配返回 `RESOURCE_VERSION_CONFLICT`。Member/Role 删除在当前事务同步完成，但按冻结契约返回可查询 Operation，查询状态为 `SUCCEEDED`。
- 成员角色/状态、角色权限/状态、角色删除和租户状态变化均在同一事务原子递增受影响成员的 `membership_version`；成员绑定变化记录 `role.bind/role.unbind`，跨租户不可感知访问记录 `security.cross_tenant_denied`。
- 验证通过：`make check-all` 在真实 PostgreSQL 16 下执行 81 个后端测试全部通过，包含迁移升级/降级、五张租户表 RLS、双租户 Member/Role 读写矩阵、幂等、ETag、版本失效、Operation 和审计；前端 7 个测试、typecheck、lint、格式和生产构建通过；R5 契约与 16 个生成文件零 Diff。
- 本阶段未新增依赖；`uv.lock` 无需变更。临时 `agent_platform_test` 数据库已删除，PostgreSQL 服务已停止。
- 跨阶段协同：AP-E0-007 可复用 `X-Request-ID`、`trace_id`、追加式 Audit 和同步终态 Operation，但 Outbox 投递、Temporal、Trace Exporter、指标和 Redis 失效广播必须在独立基础设施边界实现，不进入 IAM Router。
- 剩余生产化事项不属于 AP-E0-006：真实 OIDC/JWKS/PKCE、Secret Backend DSN Resolver、平台数据库最小权限角色和 Redis 授权失效广播。
- 下一任务：`AP-E0-007` 最小 Temporal Worker/Workflow、Outbox、结构化日志、Trace 和指标骨架。

### 2026-08-06 — AP-E0-007

- 新增任务包 `harness/tasks/AP-E0-007.yaml`，范围限定为 Temporal/Outbox/可观测性工程骨架；未修改冻结 OpenAPI、JSON Schema、基线版本或 SHA-256，也未提前实现 Run、Release、RunEvent、Event Store 和 SSE。
- 激活独立 `temporal-worker-control` 与 `temporal-worker-run` 入口，固定使用 `control-plane` 和 `run-orchestrator` Task Queue；Temporal 缺少地址或连接失败时有界失败，不伪报可用。
- 新增 `backend/packages/contracts/temporal` 带版本 Pydantic Probe Payload；Workflow ID 为 `probe/{tenant_id}/{probe_id}`，Workflow 本体不访问数据库、网络、随机数或系统时间，副作用只位于 Activity。
- 新增迁移 `0003_temporal_outbox` 和 `outbox_event` ORM，覆盖基线字段、状态约束、`status + next_attempt_at` 索引、租户索引及 ENABLE/FORCE RLS。
- 新增 `SqlAlchemyOutboxWriter`，只加入调用方已有 Session，不自行提交；新增租户级 `SqlAlchemyOutboxStore`，使用 `FOR UPDATE SKIP LOCKED` claim、Lease 恢复、重试和 DEAD 状态，所有查询显式携带 `tenant_id`。
- 新增 `OutboxDispatcher` 和严格 `TemporalProbeStarter`：claim 事务提交后才调用 Temporal，重复 Workflow ID 视为成功，RPC/超时按确定性退避重试，非法事件或 Schema 直接 DEAD，取消不被吞掉。
- 新增独立 event-worker 有界轮询 runner，要求注入显式 `TenantContextSource`，禁止无条件跨租户扫描；生产 Secret Resolver、Tenant Enumerator 和 Run/Release 业务映射按任务范围留给后续阶段，默认进程入口继续失败关闭。
- 新增 JSON 结构化日志与 request/trace/tenant ContextVar，API 请求链路接入 OpenTelemetry Span；Temporal Client 使用 SDK Trace Interceptor，OTLP 导出配置 5 秒超时。
- 新增进程、HTTP、Workflow Start 和 Outbox 的低基数 Prometheus 指标；API `/metrics` 仅允许 `AP_METRICS_ALLOWED_NETWORKS` 中的 CIDR，默认只开放 loopback，不使用 tenant_id、run_id 或 workflow_id 作为 Label。
- 本阶段未新增依赖，现有 `temporalio`、OpenTelemetry 和 `prometheus-client` 已满足需求，`uv.lock` 无需变化。Temporal 官方测试服务器仅下载到 `/private/tmp`，未进入仓库。
- 真实验证通过：PostgreSQL 16 Alembic 升降级、六张租户表 RLS、Outbox `PENDING → PUBLISHING → PUBLISHED`、原 IAM 集成，以及 Temporal Test Environment 的 Pydantic Workflow/Activity 执行。
- 全仓 `make check-all` 通过：R5 冻结契约、31 个完整性文件、2 份 OpenAPI、167 个 operationId、7 份 Schema、4 个 Golden Example 和 16 个生成文件零漂移；后端 103 passed；前端 7 项测试、Prettier、ESLint、typecheck 和生产构建通过。
- 剩余风险：当前只有 Probe Workflow 历史，尚无“上一生产版本”可供 Replay；AgentRun/Publish Workflow Replay、Workflow Run ID 业务映射、生产 event-worker 依赖装配和 Worker 独立指标监听端口由后续 Epic 完成。
- 下一任务：`AP-E0-008` Epic 0 集成验收与收口。

### 2026-08-06 — AP-E0-008 Epic 0 全栈集成验收与工程基线收口

- 新增 `harness/tasks/AP-E0-008.yaml` 和 `scripts/epic0_acceptance.py`，通过 `make epic0-acceptance` 提供失败关闭的统一验收入口；要求 `AP_TEST_DATABASE_URL` 使用 `postgresql+asyncpg` 且数据库名以 `_test` 结尾，并显式设置 `AP_TEST_TEMPORAL=1`。
- 验收顺序固定为：`make check-all` → 四组真实 PostgreSQL/IAM/Temporal 集成测试并解析 JUnit 确认零 skip → 临时 FastAPI 与 Vue/Vite readiness 代理跨端冒烟；API/Vite 子进程成功、失败和取消路径均清理。
- 验证通过：R5 契约和 16 个生成文件零漂移；后端 103 passed；前端 Prettier、ESLint、vue-tsc、Vitest 7 tests 和生产构建通过；专项集成 `4 passed`、JUnit 零 skip；Vite `/health/ready` 与 FastAPI 权威 JSON 一致。
- 本阶段未修改冻结 OpenAPI、JSON Schema、基线版本或 SHA-256；未新增业务依赖。Temporal 测试下载目录改为系统临时目录，保证跨平台且不污染仓库。
- 跨阶段协同：验收覆盖 AP-E0-001～007 的契约生成、IAM/RLS、Outbox、Temporal Worker、日志/指标和 Vue 应用壳；后端代码继续位于 `backend/`，前端代码继续位于 `frontend/`。不提前进入 Prompt、Model Gateway 或 Agent Draft 业务实现。
- 剩余风险：真实 OIDC/JWKS、Secret Backend、生产部署、AgentRun/Publish Workflow、Event Store/SSE 和 AgentScope 2.0.x 精确 patch 仍按 Epic 1～后续 Epic 处理。
- 下一任务：`AP-E1-001` 资源定义/版本、ETag、幂等和引用查询公共能力。

### 2026-08-06 — AP-E1-001 资源注册中心公共底座

- 新增任务包 `harness/tasks/AP-E1-001.yaml`，固定只实现共享资源底座；未新增 Prompt、ModelProvider、ModelConfig、Agent Draft 路由或 Vue 页面，也未修改冻结 OpenAPI、JSON Schema、基线版本或 SHA-256。
- 新增 `resource_definition` 与 `resource_version` ORM/迁移 `0004_resource_registry`：Definition 保存受治理 Draft JSON，Version 保存不可变内容、`sha256:<64 hex>` Hash、版本号、发布人和发布说明；同租户 `resource_type + code` 软删除唯一，版本按定义和内容 Hash 去重。
- 两张资源表启用 ENABLE/FORCE RLS；所有 Repository 查询显式传递 `tenant_id` 并通过 `TenantUnitOfWork` 注入上下文。跨租户同 code 可存在，跨租户读取不可感知。
- 复用冻结强 ETag `"rv:<resource_version>"` 和 CAS；从 IAM 抽出共享 24 小时幂等 claim/complete 与规范化请求 Hash，声明集合字段后才排序，避免不同业务误用集合语义。
- 新增 `CompositeResourceReferenceReader` 和显式 `after` 游标下推的 Provider Port；不新建无事实来源的 `resource_reference` 表，后续 Agent Binding、Snapshot、Deployment、Schedule、Session 提供实际引用源。
- 验证通过：R5 契约及 16 个生成文件零漂移；真实 PostgreSQL 资源集成 `1 passed`，全仓真实 PostgreSQL/Temporal `111 passed`；后端完整门禁通过，前端 7 tests、格式、Lint、类型和构建无回归；引用查询 3 页稳定游标单测通过。
- 依赖与迁移：未新增 Python/Node 依赖；`uv.lock` 无变更。部署需先执行 Alembic `0004_resource_registry`，回滚按迁移降级删除本任务新增表；临时数据库和 PostgreSQL 服务在本轮结束时清理。
- 协同与剩余风险：E0 IAM 的幂等语义已切换到共享 Hash/持久化原语，RLS 清单同步扩展到资源表；前端暂不消费新资源 API。Prompt CRUD/版本 API、Model Gateway、Agent Draft 和引用源实现由 `AP-E1-002` 及后续任务完成。
- 下一任务：`AP-E1-002` Prompt CRUD、版本、发布、回滚和前端页面。

### 2026-08-06 — AP-E1-002 Prompt 管理、版本治理与 Vue 页面

- 新增任务包 `harness/tasks/AP-E1-002.yaml`，按“冻结契约核对 → 后端服务/路由 → Vue 页面 → 真实数据库 → 全仓门禁”分段完成；未修改 R5 OpenAPI、JSON Schema、基线版本或 SHA-256，也未提前进入 Model Gateway、Agent Draft 或 AgentScope Runtime。
- 后端完成冻结的 13 个 Prompt operationId：CRUD、复制、启停、发布、版本列表、结构化 Diff、引用查询和回滚；所有用例先解析服务端身份与 TenantAccess，再按 `prompt:*` 权限授权，跨租户继续由显式 tenant 条件和 FORCE RLS 双重隔离。
- 新增迁移 `0005_prompt_permissions`：为现有和后续内置 `tenant_admin` 补齐 Prompt 权限；普通 `PUBLISH` 继续按 Canonical Content Hash 去重，显式 `ROLLBACK` 可复制历史内容生成新版本且不修改旧版本。降级会删除仅由本阶段产生的 ROLLBACK-kind 版本后恢复 AP-E1-001 唯一约束。
- Prompt 更新审计改为字段清单与内容 Hash，启停原因只保存 SHA-256；结构化 Diff 对敏感变量默认值返回 `[REDACTED]`。真实 PostgreSQL 已验证审计中不包含完整模板或启停原因明文。
- Prompt 删除使用既有同步终态 Operation；同步扩展 Operation 查询授权，使 `prompt:read` 可读取 Prompt 删除状态，避免状态 URL 只能返回但不可查询的跨阶段断链。
- 前端新增 `/prompts` 与 `/prompts/:id/edit`，采用 Vue 3 Composition API、Vue Query、Element Plus 和生成的 `ResourcesApiClient`；资源实体不复制到 Pinia。页面覆盖列表搜索、新建、复制、删除、模板/变量 Schema 分离编辑、强 ETag 保存、发布前版本 Diff/引用、启停、回滚和冲突重载。
- OIDC 仍按当前确认使用 Mock：开发环境统一请求层发送非 Secret 的 `Bearer mock`，生产构建不注入该 Header；敏感变量测试值不写入 localStorage/sessionStorage、URL 或日志。
- 本阶段未新增 Python/Node 依赖，`uv.lock` 与 `pnpm-lock.yaml` 无需变更。后端代码位于 `backend/`，前端代码位于 `frontend/`。
- 验证通过：R5 31 个完整性文件、167 个 operationId 和 16 个生成文件零漂移；真实 PostgreSQL/Temporal 全仓 `make check-all` 后端 `115 passed`，前端 `11 tests`，Black、Ruff、Pyright strict、Prettier、ESLint、Vue TypeScript 和生产构建全部通过。
- 剩余风险：生产 API 依赖装配仍受真实 OIDC/Secret Backend 阶段约束；发布前 Draft 与最新 Published Version 的逐字段服务端 Diff 受冻结契约未提供“读取版本内容/草稿对版本 Diff”端点限制，当前页面展示最近两个发布版本的服务端 Diff，并独立提示草稿是否有未保存修改。
- 下一任务建议：`AP-E1-003` Model Provider/Model Config 与 Model Gateway 基础，继续复用本阶段的资源治理、权限、ETag、幂等、版本和 Vue Query 页面模式。
