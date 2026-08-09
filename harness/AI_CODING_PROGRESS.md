# Agent 平台 AI Coding 开发节奏与记录

> 本文件是非冻结运行记录，不属于需求、API、事件或数据 Schema 契约。

更新时间：2026-08-08

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
| 10 | `AP-E1-002` Prompt 管理、版本治理与 Vue 页面 | ✅ 已完成 | 13 个 Prompt operationId、真实 PostgreSQL、Vue 页面和全仓门禁通过 |
| 11 | `AP-E1-003` Model Provider、Model Config 与异步连通性请求 | ✅ 已完成 | 20 个模型资源 operationId、连接测试 Outbox、Vue 页面和全仓门禁通过 |
| 12 | `AP-E1-004` Model Gateway 核心 | ✅ 已完成 | 已按人工确认新增不可变 `model_binding_snapshot`，生产 Binding Reader 不再读取可变 Provider Draft |
| 13 | `AP-E1-005` 三供应商 Adapter | ✅ 已完成 | OpenAI/Qwen/DeepSeek HTTP/SSE Adapter、输出持久化边界和离线供应商契约测试完成 |
| 14 | `AP-E1-006` 预算、限流、Token/费用与受控 fallback | 🟡 当前子阶段完成 | 快照、Run Token 预约、RPM 限流和受控 fallback 已实现，真实 PostgreSQL/Temporal 复验通过 |
| 15 | `AP-E1-007` Agent Draft API、校验与引用约束 | ✅ 已完成 | 7 个冻结 API、ResourceBinding 路由校验、引用约束、RLS/权限/审计和 `0010_agent_draft` 已完成，真实 PostgreSQL/Temporal 零 skip 验收通过 |
| 16 | `AP-E1-008` Agent Draft Vue 编辑器与 Epic 1 纵向验收 | ✅ 已完成 | Agent 管理列表、六步草稿编辑器、版本绑定、fallback、子 Agent、引用删除保护和错误态已完成 |
| 17 | `AP-E1-009` AgentScope 2.0.x 兼容 Spike | 🟡 等待 CI 制品 | 2.0.5/API/事件/Gateway 边界和受控 Dockerfile已完成，待现有 CI 回填生产 manifest digest |
| 外部前置 | `AP-E1-009` 镜像基线收口 | ⏳ 外部制品 | 现有 CI 执行构建、SBOM、扫描、签名与推送，并回填可拉取的 registry digest |
| 18 | `AP-E2-001` Snapshot 编译与不可变引用 | ✅ 已完成 | 不可变 Agent Version/Snapshot、确定性编译、Model Binding Snapshot 和真实 PostgreSQL 验证完成 |
| 19 | `AP-E2-002` AgentScope Bundle 与 Manifest | ✅ 已完成 | Bundle 编译/校验、资源 Hash/权限/供应链边界和真实 PostgreSQL 验证完成 |
| 20 | `AP-E2-003` Release Workflow 与失败保护 | ✅ 已完成 | 发布 Operation/Outbox/Temporal Workflow、Bundle 持久化和失败关闭完成 |
| 21 | `AP-E2-004` Deployment 激活与 fencing | ✅ 已完成 | STAGED/ACTIVE/DEGRADED/RETIRED、单调 fencing 和原子激活完成 |
| 22 | `AP-E2-005` 发布预览、Diff、历史与 Vue 页面 | ✅ 已完成 | R7 Preview、脱敏 Diff、历史查询和 Vue 发布页完成 |
| 23 | `AP-E2-006` 历史 Snapshot 回滚 | ✅ 已完成 | 新 Release/Operation/Deployment 回滚闭环完成 |
| 24 | `AP-E3-001` Session CRUD、归档、删除约束和分页 | ✅ 已完成 | `0015_chat_session`、当前用户隔离、固定 Deployment、CRUD/归档/延迟删除、Vue 管理页和全链路验证完成 |
| 25 | `AP-E3-002` Message 历史、分支顺序和权限 | ✅ 已完成 | `0016_chat_message`、不可变消息链、分支历史、Vue 只读页面和全链路验证完成 |
| 26 | `AP-E3-003` Run 创建、幂等、状态机和 Snapshot 绑定 | ✅ 已完成 | `0017_agent_run`、原子 USER Message/Run/Outbox、并发保护、固定执行身份和删除 Guard 完成 |
| 27 | `AP-E3-004` AgentRunWorkflow、Activity、Signal 和 Query | ✅ 已完成 | R8、Workflow/Activity、Attempt、RunSpec Ref、最终 Message 事务和真实 PostgreSQL/Temporal 验证完成 |
| 28 | `AP-E3-005` 取消、重试、新 Run 语义和 fencing token | ✅ 已完成 | cancel/retry API、Runtime inspect/cancel、安全恢复、新 Attempt fencing 和真实全链路验证完成 |
| 29 | `AP-E3-006` Outbox dispatcher、Workflow 映射和对账入口 | ✅ 已完成 | R9、正式 Run Router、启动结果持久化、CREATED/CANCELLING 对账和真实全链路验证完成 |
| 30 | `AP-E3-007` RuntimeEventCandidate Fake 与 Epic 3 纵向验收 | ✅ 已完成 | Candidate Publisher Port、HTTP/Outbox/Temporal/PostgreSQL/getRun 纵向闭环和 Epic 3 全链路回归完成 |
| 31 | `AP-E4-001` RunEvent 表、计数器、约束和迁移 | ✅ 已完成 | 非分区 RunEvent、独立 Counter、双全局唯一、Payload 上限、RLS、不可变性和容量迁移待办已完成 |
| 32 | `AP-E4-002` Candidate 校验、批量写入、幂等序号和终态保护 | ✅ 已完成 | serviceAuth 边界、fencing、并发连续序号、逐项幂等、Event Outbox、终态保护和全链路回归完成 |
| 33 | `AP-E4-003` 事件查询、分页、回放和序号缺口语义 | ✅ 已完成 | 所有权隔离、稳定序号游标、缺口失败关闭、Thinking 脱敏和 102 条真实事件分页回放完成 |
| 下一步 | `AP-E4-004` Event Outbox、Redis 唤醒与 SSE | ⏳ 待开始 | 历史优先、Last-Event-ID、去重、背压和终态关闭；Redis 只作唤醒，事实仍读 PostgreSQL |

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

### 2026-08-07 — AP-E1-003 Model Provider、Model Config 与异步连通性请求

- 新增任务包 `harness/tasks/AP-E1-003.yaml`，范围固定为 Provider/Config 治理、Secret Reference 和连接测试请求；未修改 R5 OpenAPI、JSON Schema、基线版本或 SHA-256，也未提前实现 Model Gateway、真实供应商调用或三家 Adapter。
- 后端完成冻结的 20 个 Model Provider/Model Config operationId：Provider CRUD、启停和连接测试；Config CRUD、发布、回滚、版本、Diff 和引用查询。新增迁移 `0006_model_permissions`，为 `tenant_admin` 回填两类资源权限，并让 Operation 查询支持 `model_provider`、`model_config`。
- Provider 仅允许 `openai`、`qwen`、`deepseek`；外部 Base URL 强制 HTTPS，HTTP 只允许 loopback。`secret_ref` 仅接受安全的 `secret://tenant/...` 引用，不解析、不记录、不返回 Secret 明文。
- Model Config 创建/更新在同事务确认 Provider 同租户存在，发布和回滚额外拒绝已停用 Provider；仍有活动 Model Config Draft 引用时拒绝删除 Provider。普通发布继续 Hash 去重，回滚继续生成不可变新版本。
- 连接测试在同一租户事务中创建 `ACCEPTED` Operation、`model_provider.connection_test_requested` Outbox 和审计记录，幂等重放原 Operation；Outbox 只携带 Provider 定义和 Secret Reference，不携带 Secret 值。真实网络请求与 Operation 终态归 AP-E1-004/AP-E1-005。
- 前端新增 `/models/providers`、`/models/configs`，复用 Vue 3、Element Plus、Vue Query、生成 Resources Client 和统一鉴权 Transport；新增 Core Client 按冻结 `status_url` 轮询 Operation。列表掩码展示 Secret Reference，表单明确拒绝 API Key/Token 明文，资源不复制进 Pinia 或浏览器存储。
- 协同修正：既有 PostgreSQL Outbox 集成测试显式固定 `next_attempt_at`，消除数据库当前时间晚于固定 claim 时间造成的日期脆弱性；生产 Outbox 逻辑未变。
- 本阶段未新增 Python/Node 依赖，`uv.lock` 与 `pnpm-lock.yaml` 无变更。后端代码位于 `backend/`，前端代码位于 `frontend/`。
- 验证通过：R5 31 个完整性文件、167 个 operationId 和 16 个生成文件零漂移；真实 PostgreSQL/Temporal `make check-all` 后端 `129 passed`，前端 `18 tests`，Black、Ruff、Pyright strict、Prettier、ESLint、Vue TypeScript 和生产构建全部通过。
- 剩余风险：连接测试 Operation 会保持 `ACCEPTED`，直到后续 Gateway/Adapter Worker 消费新事件；真实 Secret Backend、供应商错误/用量归一化和流式响应仍按 AP-E1-004/AP-E1-005 实现。
- 下一任务建议：`AP-E1-004` Model Gateway 请求、流式响应、错误与用量归一化，并实现连接测试 Worker 的 Operation 状态推进。

### 2026-08-07 — AP-E1-004 Model Gateway 核心实现（冻结方案已确认）

- [x] 新增任务包 `harness/tasks/AP-E1-004.yaml`；范围限定为 Gateway 内核、Fake Adapter 契约、连接测试消费者和标准化用量事实，不修改冻结 OpenAPI、JSON Schema、基线版本或 SHA-256，不提前实现 AP-E1-005 三家真实 HTTP Adapter。
- [x] 新增与冻结 `model-gateway-v1.schema.json` 对齐的请求、响应、Usage 和六类流事件 Pydantic Contracts；短期授权字段禁止出现在对象 repr，Provider 长期凭据继续只以 `SecretStr` 在 Gateway 边界内存中存在。
- [x] 新增 Provider Adapter、Model Binding Reader、Secret Resolver、Budget Guard、Usage Recorder 和 Observer 端口；普通与流式编排统一处理能力校验、调用超时、错误归一化、Usage 归一化和终态输出。
- [x] fallback 仅在 `retryable=true` 且 `submission_state=not_submitted` 时进入下一路由；超时、已提交和未知提交状态均禁止自动 fallback，避免重复用户请求或 Tool Result。
- [x] 缺少任一精确 Token 字段时统一 `estimated=true`，未知费用保持为空，不虚构模型费用表；新增低基数请求、fallback 和 Token 指标，不使用 tenant、run、model 或 provider_request_id 作为 Label。
- [x] 新增迁移 `0007_model_gateway_usage`、ORM 和 PostgreSQL Store，覆盖基线 `model_usage` 字段、非负/费用成对/时间顺序约束、租户与 Run 索引及 ENABLE/FORCE RLS。
- [x] 新增 `OutboxEventRouter` 和 `ModelProviderConnectionTestHandler`：保持 `TemporalProbeStarter` 的单一 Probe 语义，模型连接测试按独立事件处理，Operation 可幂等推进 `ACCEPTED → RUNNING → SUCCEEDED/FAILED`，结果、错误和审计只保存安全摘要。
- [x] AP-E1-003→004 真实 PostgreSQL 集成测试代码已补齐：连接测试 Outbox 应变为 `PUBLISHED`、Operation 应为 `SUCCEEDED`、完成审计无 Secret、`model_usage` 可写且跨租户不可见。
- [x] 无数据库门禁通过：`make backend-check` 为 `135 passed, 5 skipped`，Black、Ruff、Pyright strict 通过；`make frontend-check` 为 18 tests、Prettier、ESLint、Vue TypeScript 和生产构建通过；冻结契约 31 个完整性文件、167 个 operationId、7 个 Schema 和 16 个生成文件零漂移；未新增依赖。
- [x] 真实 PostgreSQL/Temporal 终验已在 AP-E1-007 收口：Model Provider 连接测试 Outbox/Operation、`model_usage`、RLS 和迁移链随全仓零 skip 门禁通过。
- [x] FR-MDL-006 冻结载体已人工确认并由 AP-E1-006 实现：采用独立不可变 `model_binding_snapshot`，在 ModelConfig 发布事务中生成，生产 `ModelBindingReader` 不读取可变 Provider Draft。
- 跨阶段协同：现有 Vue Provider 页面继续按 `status_url` 轮询，无需新增前端接口；AP-E1-005 只需实现 OpenAI/Qwen/DeepSeek Adapter 和真实 Secret Backend 装配，不应复制 Gateway 错误、流事件、fallback 或 Usage 逻辑。

### 2026-08-07 — AP-E1-005 OpenAI、Qwen、DeepSeek Provider Adapter

- [x] 新增任务包 `harness/tasks/AP-E1-005.yaml`，限定为三供应商 Adapter、共享 OpenAI-compatible HTTP/SSE 传输和离线供应商契约测试；未修改 R5 OpenAPI、JSON Schema、基线版本或 SHA-256，不提前进入 AP-E1-006 预算/限流策略。
- [x] 修正 AP-E1-004/005 边界：新增 `ModelRequestMaterializer` 将冻结 `prompt_ref/tools_ref` 物化为 Provider-neutral 消息和工具；新增 `ModelOutputWriter` 将 Provider-neutral 输出写成冻结 `output_ref`。Adapter 不读取平台引用、不访问业务 ORM、不伪造对象存储 URI。
- [x] 完成 `OpenAIProviderAdapter`、`QwenProviderAdapter`、`DeepSeekProviderAdapter`。三家复用安全 HTTP/SSE 传输，但通过独立 Profile 管理参数白名单、Request ID Header 和 Usage 差异；不引入供应商 SDK，继续复用现有 `httpx`。
- [x] 完成普通文本、工具调用、reasoning、流式 Delta、Usage、连接测试和 finish reason 映射；Bearer Secret、平台短期授权、供应商原始错误和内部地址不会进入公共响应、日志或测试快照。
- [x] 完成认证、限流、请求错误、502/503、500/504、连接失败、读取超时、协议错误、重定向和取消归一化。仅 `retryable=true + not_submitted` 允许 fallback；未知或已提交状态禁止切换供应商。
- [x] 新增三供应商参数化契约测试和 Gateway 协同测试，覆盖 `Materializer → Adapter → OutputWriter → Usage → Response/Stream`；输出持久化失败被标记为 `submitted`，验证不会调用下一 fallback 路由。
- [x] 独立门禁通过：`make backend-check` 为 `169 passed, 5 skipped`，Black、Ruff、Pyright strict 通过；`make contract-check` 验证 31 个完整性文件、167 个 operationId、7 个 Schema 和 16 个生成文件零漂移；`make frontend-check` 为 18 tests，Prettier、ESLint、Vue TypeScript 和生产构建通过；`git diff --check` 与 `uv lock --check` 通过。
- [x] `make check` 已在 AP-E1-006 补齐并通过；AP-E1-007 收口时进一步完成真实 PostgreSQL/Temporal `make check-all`，三供应商 Adapter 与 Gateway 协同无回归。
- 依赖与在线验证：本阶段未新增 Python/Node 依赖，锁文件无变化；没有使用真实供应商凭证或在线付费请求，三家验证均为 `httpx.MockTransport` 离线契约测试。正式启用模型和费用表仍待配置。
- 人工确认项已关闭：AP-E1-006 已采用独立不可变 `model_binding_snapshot`，并实现生产 `SqlAlchemyModelBindingReader`。
- 下一任务建议：`AP-E1-006` 预算、限流、Token/费用策略和受控 fallback。实现时复用现有 Gateway `BudgetGuard`、Usage Store、Observer 和 Provider Profile，不把策略下沉到三家 Adapter。

### 2026-08-07 — AP-E1-006 当前子阶段：不可变绑定、预算、RPM 与受控 fallback

- [x] 按人工确认新增 `0008_model_binding_snapshot`：ModelConfig 发布与快照写入同一事务，回滚优先复制源 Version 快照；冻结 Provider 类型、Base URL、Secret Reference、超时、模型、能力、默认参数、上下文上限和 RPM，不保存 Secret 明文。
- [x] 新增生产 `SqlAlchemyModelBindingReader`，`model_binding_id` 解析为 ModelConfig Version ID，只读取同租户不可变快照；Provider Draft 后续更新不会改变历史绑定。AP-E1-004 的 FR-MDL-006 人工确认项已关闭。
- [x] Gateway fallback 改为默认禁用，显式配置最多两级和允许错误码；`submitted/unknown`、本地 `GATEWAY_RATE_LIMITED`、Token/费用预算错误均硬性禁止 fallback。预算在请求物化后、Provider 调用前准入，Usage 持久化后结算。
- [x] 新增 `0009_model_gateway_admission`、`budget_reservation` 和 `model_rate_limit_window`：Run 预算使用 PostgreSQL 事务级 advisory lock 串行化，保守预约当前剩余 Token，支持结算、释放、超时恢复和幂等键冲突保护；RPM 使用 tenant/binding/provider 固定 UTC 分钟原子 Upsert。两表均显式 tenant 条件并启用 ENABLE/FORCE RLS。
- [x] 未配置可信费用表时，带 `cost_budget` 的请求返回 `COST_BUDGET_UNAVAILABLE`，不按零费用放行；未新增或虚构 OpenAI/Qwen/DeepSeek 价格。本阶段未新增依赖，锁文件无变化。
- [x] 验证通过：`make check`；最终 `make backend-check` 为 `185 passed, 5 skipped`；`make frontend-check` 为 18 tests；`make contract-check` 验证 R5 基线、167 个 operationId 和 16 个生成文件零漂移；`uv lock --check`、`git diff --check` 通过。
- [x] 真实 PostgreSQL 复验通过：RLS、并发 Token 预约、结算/释放、租户隔离和 RPM 超限随 AP-E1-007 全仓零 skip 门禁执行。
- 后续子阶段：tenant/user/agent 周期 `budget_policy`/`quota_policy`、正式版本化费用表、多实例容量与对账；这些能力未进入当前冻结管理 API，不在本轮创建占位表或虚构配置。
- 下一任务建议：进入 `AP-E1-007` Agent Draft API；编译或引用 ModelConfig 时必须保存已发布 Version ID，后续 AgentSnapshot/Run 继续以该 ID 解析本轮不可变 Binding Snapshot。

### 2026-08-07 — R6 冻结基线与 Agent ResourceBinding 路由扩展

- [x] Core OpenAPI 升级为 `1.3.0`，冻结基线由 R5 升级为 `agent-platform-v1-dev-baseline-2026-08-r6`；文档索引、核心契约、数据库设计、前端交互契约、AI Coding 总纲、执行计划、追踪矩阵和测试任务包已同步。
- [x] `ResourceBinding` 新增 `binding_role`、`configuration_schema_version=model-routing/v1` 和受控 `fallback_error_codes`；`AgentBindingList` 冻结单 Model 兼容、唯一 primary、最多两级连续 fallback、禁止重复角色和错误码白名单约束。
- [x] 保留单 Provider `ModelConfig`，fallback 作为 Agent ResourceBinding 路由策略；生产 Gateway/Runtime 只消费已发布 Version 和不可变 `model_binding_snapshot`，不读取 Agent/Provider Draft。
- [x] Python/TypeScript 生成 DTO 已更新，并修复生成器对带 `allOf` 数组 Schema 的处理；R6 SHA-256 完整性和生成物零漂移已纳入契约门禁。

### 2026-08-07 — AP-E1-007 Agent Draft API、ResourceBinding 校验与引用约束

- [x] 新增任务包 `harness/tasks/AP-E1-007.yaml`，以 R6 为唯一冻结输入；完成 `createAgent/listAgents/getAgent/updateAgent/copyAgent/disableAgent/deleteAgent` 7 个冻结 operationId。
- [x] 新增 Agent 领域记录、应用服务、PostgreSQL Repository、ORM 和迁移 `0010_agent_draft`；`agent_definition`/`agent_binding` 启用 FORCE RLS，查询显式携带 tenant，并为 `tenant_admin` 回填 Agent 管理权限。
- [x] 创建、复制、停用和删除支持幂等重放；更新使用强 ETag/`If-Match` CAS。删除在存在活动 Deployment 或其他未删除 Agent Draft 入站引用时拒绝，否则软删除并返回终态 Operation。
- [x] 旧单 Model binding 统一规范为 primary；多 Model binding 验证唯一 primary、连续 fallback 顺序、最多两级和受控错误码。固定 Prompt/ModelConfig 绑定必须引用同租户已存在不可变 Version；子 Agent 当前仅允许 `resolve_on_publish`，并禁止自引用。
- [x] Agent binding 审计仅记录字段、角色与受控错误码摘要，不写入 Secret 或完整配置正文；Operation 查询已纳入 `agent:read` 权限协同。
- [x] 真实 PostgreSQL/Temporal 全仓门禁通过：`make check-all` 后端 `217 passed, 0 skipped`，前端 18 tests，契约 R6、167 个 operationId 和 16 个生成文件零漂移；Black、Ruff、Pyright、Prettier、ESLint、Vue TypeScript 和生产构建通过。
- [x] 真实数据库验收发现并修复 Agent 非模型绑定的 `None` 被写成 JSONB `null` 问题；`configuration_json` 现使用 `none_as_null=True`，与数据库路由字段约束保持 SQL `NULL` 语义，并增加 ORM 元数据回归断言。
- 依赖与部署：本阶段未新增 Python/Node 依赖，锁文件无需变更；部署前执行 Alembic `0010_agent_draft`，回滚只删除 Agent 权限和两张 Agent Draft 表，不修改既有 Resource Version、Model Binding Snapshot 或 Usage 事实。
- 跨阶段协同：AP-E1-008 前端编辑器应复用生成 DTO 和统一 API Transport，并以发布 Version ID 绑定 Prompt/ModelConfig；AP-E2-001 发布编译需冻结 ModelConfig Version、路由顺序、允许错误码和不可变 `model_binding_snapshot`。
- 下一任务建议：`AP-E1-008` Agent Draft Vue 编辑器与 Epic 1 纵向验收。

### 2026-08-07 — AP-E1-008 Agent Draft Vue 编辑器与 Epic 1 纵向验收

- [x] 新增 `/agents` 管理页和 `/agents/:id/edit` 六步草稿编辑器，复用 Vue 3、Element Plus、Vue Query、生成 DTO 和统一鉴权 Transport；Agent 服务端状态不复制到 Pinia。
- [x] 列表覆盖搜索、状态筛选、游标翻页、新建、复制、停用和删除；删除前读取冻结 `ReferencePage`，存在引用或引用检查失败时均阻止提交。
- [x] 编辑器支持 Prompt/ModelConfig 已发布 Version 或 `resolve_on_publish`、连续 `primary → fallback_1 → fallback_2`、受控 fallback 错误码和子 Agent 发布时解析；未实现生产链路的 Skill、MCP、知识库、Sandbox、预算和发布能力明确标记不可用，不提交占位 ID。
- [x] 强 ETag 冲突保留本地修改并提供重新加载；未保存离开提示、重复提交状态和依赖资源失败关闭已覆盖。浏览器错误态验收发现并修复“加载失败仍显示已保存且可保存”的问题，新增回归测试。
- [x] 修复跨阶段历史断链：冻结契约和生成 Client 已包含 `listAgentReferences`，AP-E1-007 后端此前未落地；现已补齐领域记录、应用服务、PostgreSQL Repository、路由、权限/RLS 约束和真实数据库断言，不修改 R6 OpenAPI、Schema、基线版本或 SHA-256。
- [x] 真实 PostgreSQL/Temporal 全仓门禁通过：`make check-all` 后端 `218 passed, 0 skipped`；本轮前端门禁为 `24 tests`，Prettier、ESLint、Vue TypeScript 和 Vite 生产构建通过；R6 31 个完整性文件、167 个 operationId 和 16 个生成文件零漂移。
- 浏览器验证：内置浏览器确认列表、编辑器加载态和失败态布局及禁用行为；其安全策略以 `ERR_BLOCKED_BY_CLIENT` 拦截 `/api/*`，因此真实数据态交互由组件测试和本机 Vite→Mock API 代理 `200` 探针覆盖，不为绕过工具限制修改生产请求路径。
- 依赖与部署：未新增 Python/Node 依赖，锁文件无变化；后端代码位于 `backend/`，前端代码位于 `frontend/`，本阶段不新增迁移。
- 下一任务建议：`AP-E1-009` AgentScope 2.0.x 兼容 Spike。只在隔离测试路径确认精确 patch、镜像 Digest、异步/流式/工具调用兼容性和 Bundle 输入边界，不绕过后续 Temporal、Event Service、Policy 或 Sandbox。

### 2026-08-07 — AP-E1-009 AgentScope 2.0.x 兼容 Spike

- [x] 锁定 `agentscope==2.0.5`，记录官方 wheel SHA-256；新增离线兼容探针，验证 Python 3.12、统一 `Agent.reply_stream`、`ChatModelBase._call_api`、文本/Thinking/Tool/外部执行/取消事件所需符号和字段。
- [x] 在 `backend/packages/runtimes/agentscope/` 建立隔离适配层；AgentScope 对象不进入应用、领域或基础设施公共契约，未启用 `runtime-worker-agentscope`，也未修改 R6 OpenAPI、RunSpec、RunEvent、Bundle Manifest 或基线 SHA-256。
- [x] 新增有状态事件转换器：稳定生成 `agentscope:{event.id}` 幂等源 ID，统一 UTC 时间，丢弃 metadata；文本、Thinking、Tool Call/Result 通过冻结 `RuntimeEventCandidate` 校验。审批、外部执行暂停和二进制数据缺少平台事实时失败关闭，不伪造 Approval、Artifact 或 Run 终态。
- [x] 新增 `AgentScopeGatewayChatModel`：动态消息和工具先由 `AgentScopeInvocationWriter` 写为不可变 `prompt_ref/tools_ref`，随后只调用既有 Model Gateway Stream Port；AgentScope 不持有供应商 Secret、不启用自身重试，也不实例化内置 OpenAI/Anthropic/DashScope/DeepSeek Model Client。
- [x] 新增 13 个隔离测试，覆盖精确版本/API、文本/Thinking/Tool 映射、重复事件、metadata 脱敏、Approval/Artifact/External Execution 失败关闭、Gateway 文本/工具/Usage/终态、安全错误和取消不重试。
- [x] 依赖同步已完成。AgentScope 传递引入约 49 个包并包含供应商 SDK、MCP 和 NumPy，运行代码仍由端口测试保证不直接使用供应商 SDK。本机为 arm64 主机上的 x86_64 Python，`cryptography>=49` 无匹配 macOS x86_64 wheel；新增 `Darwin+x86_64` 的 `<49` 兼容标记后，uv 通用锁统一解析为 `48.0.1`。现有 CI 构建时必须复验 Linux wheel、SBOM和安全扫描，后续可在构建基线支持平台分叉时解除统一版本约束。
- [x] 全链路验证通过：AgentScope 定向 `13 passed`、镜像契约 `2 passed`；`make backend-check` 为 `226 passed, 5 skipped`；新增镜像契约后的真实 PostgreSQL/Temporal `make check-all` 为后端 `233 passed, 0 skipped`、前端 `24 passed`，R6 契约和 16 个生成文件零漂移，前端类型/Lint/格式/生产构建全部通过。
- 跨阶段协同：本 Spike 只定义不可变调用输入 Writer Port，尚未把动态多轮消息写入具体 Artifact/Resource Store；后续 AgentRun/Bundle/Artifact 阶段必须实现该 Port，Runtime 不得为多轮 Tool Result 绕过 Gateway。AgentScope Memory/Session 不作为平台 Session、Run 或恢复事实来源。
- [x] 人工确认采用“复用现有 CI/制品仓库，仓库保留受控 Dockerfile”。新增 `infra/images/runtime-agentscope/Dockerfile`，要求 CI 以 digest 传入批准的 Python/uv 基础镜像，按 `uv.lock` 安装、使用非 root UID，并保持 Worker 入口故障关闭；仓库不新增平行 CI 或制品仓库。
- [x] 新增镜像交接说明和自动化契约测试，固定 build context、基础镜像策略、SBOM/漏洞/许可证扫描、签名及最终 Registry digest 回填要求。
- 外部制品待办：现有 CI 尚未在本工作区提供构建结果，任务更新为 `in_progress/awaiting_existing_ci_registry_build`。CI 推送后需把可拉取的 manifest digest 回填 `harness/runtime-baselines/agentscope-2.0.5.yaml`；本地 image ID 和可变标签均不可使用。

### 2026-08-07 — AP-E2-001 Snapshot 编译与不可变引用

- [x] 新增独立 Publishing 领域/应用边界和 deterministic `agent-snapshot/v1` 编译器；canonical JSON 同时生成 `content_hash` 与 `compiler_input_hash`，不包含时间、随机数、AgentScope 对象或 Secret 明文。
- [x] 新增迁移 `0011_agent_snapshot`、`agent_version` 和 `agent_snapshot`；两表启用 ENABLE/FORCE RLS，`agent_id + version_no`、一 Version 一 Snapshot 和复合租户外键完整，数据库触发器拒绝已发布事实的 UPDATE/DELETE。
- [x] Snapshot 编译在 Repeatable Read 事务内锁定 Agent Draft CAS，并将发布成功后的 Draft `resource_version` 前移；相同 Idempotency-Key 并发触发的 PostgreSQL `40001` 仅做最多三次有界事务重试，随后稳定重放同一 Snapshot，其他数据库异常不吞掉。
- [x] `fixed` 与 `resolve_on_publish` 均只接受启用 Definition 的 `PUBLISHED` Version；后者选择最高 `version_no`。知识库尚未进入 Resource Registry、资源停用、版本缺失、Model Binding Snapshot 缺失时失败关闭。
- [x] Model 路由冻结确定的 ModelConfig Version、`primary → fallback_1 → fallback_2` 顺序、受控 `fallback_error_codes` 以及每一路由对应的不可变 `model_binding_snapshot` ID/Hash；Runtime/Gateway 后续无需读取 ModelConfig 或 Provider Draft。
- [x] 子 Agent 解析为最新 Agent Version/Snapshot；未发布、停用、跨租户、直接或传递递归依赖均拒绝编译。新增租户隔离的 Agent Version/Snapshot 读取 Port 和 Draft/Snapshot `ResourceReferenceProvider`，直接从现有 `agent_binding` 和 immutable Snapshot 内容提供引用事实，不建立平行引用表。
- [x] 真实 PostgreSQL 验证覆盖迁移/回滚、RLS、CAS、同键并发、`resolve_on_publish`、模型绑定冻结、未发布子 Agent、传递循环、跨租户隔离、引用分页和数据库不可变触发器；发现并修复 Repeatable Read 并发 `40001` 未重放问题。
- [x] 全链路门禁通过：`make backend-check` 为 `234 passed, 5 skipped`；带 PostgreSQL/Temporal 的 `make check-all` 为后端 `239 passed, 0 skipped`、前端 `24 passed`，R6 契约、31 个完整性文件和 16 个生成文件零漂移，前端生产构建通过。
- 依赖与部署：本阶段未新增 Python/Node 依赖；部署前执行 Alembic `0011_agent_snapshot`。回滚会删除 Agent Snapshot/Version 表及不可变触发器，因此仅允许在确认没有需要保留的发布事实时执行。
- 跨阶段协同：AP-E2-002 Bundle Compiler 必须只消费本阶段 Snapshot 和资源 Version/Hash，不回读 Draft；AP-E2-003 再把该内部编译用例接入 Release Workflow、冻结 `publishAgent` API 和 Operation，不在本阶段提前开放同步发布旁路。
- 下一任务建议：`AP-E2-002` AgentScope Bundle 与 Bundle Manifest 编译/校验；继续保留 AP-E1-009 外部 Registry manifest digest 回填待办，两者不互相伪装完成。

### 2026-08-07 — AP-E2-002 AgentScope Bundle 与 Bundle Manifest 编译/校验

- [x] 新增 Runtime Bundle 领域模型、deterministic AgentScope Bundle compiler 和 verifier；基于不可变 Snapshot 图生成 `runtime.yaml`、Prompt、Skill、MCP、安全策略、`file-manifest.json` 与 `manifest.json`，相同输入得到相同内容寻址 `bundle_id/content_hash`。
- [x] 保持 R6 `bundle-manifest-v1` 不变并直接使用冻结 Schema 做输出验收；文件路径、Hash、大小、权限、来源、绑定、安全 Hash、Secret Reference、排序、唯一性和 Manifest/File 集合均有运行时语义校验，`manifest.json` 不产生自引用 Hash。
- [x] 新增应用装配服务和 PostgreSQL `SqlAlchemyBundleInputReader`，只读取同租户 Agent Snapshot/Version、Resource Version 与 `model_binding_snapshot`；不检查 mutable Definition Draft 状态，因此历史资源停用后仍可按 Snapshot 重放。
- [x] AgentScope Runtime 配置只保存 Model Gateway binding snapshot ID/Hash，不包含 Provider Secret；Skill `secret-purpose:*` 保留在权限策略，只有 MCP 的 `secret://` 引用进入 Manifest，Secret 明文和 AgentScope 对象不进入领域/应用契约。
- [x] Skill Artifact 通过只读 Port 取回并校验声明 Hash；危险路径、Artifact 篡改、资源 Hash 漂移、模型路由不一致、跨租户/递归/歧义子 Agent 图、多个不一致 Sandbox Policy 和缺失根 Sandbox Profile 均失败关闭。
- [x] 跨阶段真实 PostgreSQL 验证证明 AP-E2-001 Reader 可被 Bundle 阶段租户隔离复用；全链路门禁通过：定向 Bundle `11 passed`，`make backend-check` 为 `245 passed, 5 skipped`，带 PostgreSQL/Temporal 的 `make check-all` 为后端 `250 passed, 0 skipped`、前端 `24 passed`，R6 31 个完整性文件、167 个 operationId 和 16 个生成文件零漂移。
- 依赖与部署：本阶段未新增 Python/Node 依赖、数据库迁移或前端代码；`runtime_bundle` 持久化、对象存储上传、签名/SBOM/扫描、Sandbox 冒烟测试、Release Workflow 和 Deployment 均保留到 AP-E2-003 及后续阶段。
- 已确认边界：AP-E1-009 Registry digest 不进入冻结 Bundle Manifest；Bundle 可独立编译，但生产 Release/Deployment 和 Worker 启用前仍必须由现有 CI 回填可拉取的 Registry manifest digest。
- 显式前置条件：当前生产 Sandbox Profile 管理入口和具体 Skill Artifact Object Store Adapter 尚未接入；Bundle 编译器对缺失输入失败关闭，不创建伪造默认策略或本地文件旁路。
- 下一任务建议：`AP-E2-003` Release Workflow、异步 Operation 和发布失败保护；接入 RuntimeBundle Store/对象存储、签名/SBOM/扫描，并在发布激活前统一校验 Sandbox Profile 与 AgentScope Registry digest。

### 2026-08-07 — AP-E2-003 Release Workflow、异步 Operation 与发布失败保护

- [x] 完成冻结 `publishAgent/getRelease`：发布请求在同一租户事务内校验 `agent:publish`、Draft `resource_version`、Runtime Target 集合和幂等键，并原子创建 `REQUESTED` Release、`ACCEPTED` Operation、审计事实与 `agent.release_requested.v1` Outbox；API 不直接调用 Temporal。
- [x] 新增确定性 `publish/{tenant_id}/{release_id}` Starter 与 `PublishAgentWorkflow`，复用 `USE_EXISTING + REJECT_DUPLICATE`；Workflow 分段执行校验/Snapshot、Bundle 编译存储、扫描、可选 Smoke、激活/最终化，Activity 失败在重试耗尽后同步终结 Release/Operation。
- [x] 新增迁移 `0012_release_bundle`、`release` 与 `runtime_bundle` ORM/RLS/复合租户外键和唯一约束；Release 状态采用 CAS 语义，Bundle 按 Snapshot、Runtime、Compiler 与 content hash 幂等，扫描结果只能从 PENDING 进入终态。
- [x] 跨阶段复用 AP-E2-001 `SnapshotCompilationStore` 和 AP-E2-002 `AgentScopeBundleCompilationService`，不回读 Agent/Resource/Provider Draft；对象存储、签名、SBOM、扫描、Sandbox Smoke 和 Deployment 激活使用显式端口，缺少供应链元数据或 Registry manifest digest 时失败关闭。
- [x] `activate_on_success=false` 仍按冻结状态机经过 ACTIVATING 最终化但不创建 Deployment；`true` 必须由 AP-E2-004 激活 Adapter 为每个 Runtime Target 返回 Deployment，否则 Release FAILED。激活前失败不修改既有 `agent_definition.active_deployment_id`。
- [x] 验证通过：定向发布测试、真实 PostgreSQL 迁移/RLS/幂等/失败保护 `1 passed`、Temporal Test Environment 正常与失败路径 `3 passed`；`make backend-check` 为 `266 passed, 7 skipped`，真实 PostgreSQL/Temporal `make check-all` 为后端 `273 passed, 0 skipped`、前端 `24 passed`，R6 契约、31 个完整性文件、167 个 operationId 和 16 个生成文件零漂移。
- 依赖与部署：本阶段未新增 Python/Node 依赖，`uv.lock` 无新增变更；部署前执行 Alembic `0012_release_bundle`。生产对象存储、签名/SBOM、扫描和 Sandbox Smoke Adapter 必须由部署环境显式注入，当前未配置时不会伪造成功。
- 已知外部前置：AP-E1-009 AgentScope Registry manifest digest 仍等待现有 CI/制品仓库回填；这不阻塞代码与 Bundle 编译，但会按设计阻塞生产 Release 成功。
- 下一任务建议：`AP-E2-004` Deployment STAGED/ACTIVE/RETIRED、并发 fencing 和原子激活；复用本阶段 `ReleaseDeploymentActivator`，不在 Workflow 内直接更新 ACTIVE Deployment。

### 2026-08-07 — AP-E2-004 Deployment 激活、历史状态和并发保护

- [x] 新增冻结 Deployment 领域状态、确定性 `tenant/release/runtime_target` 身份和兼容性 Hash；兼容 Hash 覆盖 Snapshot、Bundle content hash、Runtime Target、Runtime 类型和 Registry manifest digest，不暴露内部 fencing token。
- [x] 新增迁移 `0013_deployment_activation`、Deployment ORM、FORCE RLS、复合租户/Release/Snapshot/Bundle 外键、同 Release+Target 幂等唯一约束和 `(tenant, agent, runtime_target) where ACTIVE` 部分唯一索引；`agent_definition.active_deployment_id` 继续仅作默认 Deployment 查询物化。
- [x] 实现 `SqlAlchemyDeploymentStore` 并接入 AP-E2-003 `ReleaseDeploymentActivator` Port：全部 Runtime Target、Snapshot、Bundle、签名/SBOM、scan 和 Registry digest 先完成预校验，再在一个事务内退役旧 ACTIVE/DEGRADED 并激活新 STAGED；任一失败整体回滚。
- [x] Release 新增数据库生成的单调 `activation_fencing_token`；Agent 行锁负责稳定串行化，历史最大 token 拒绝过期 Release，确定性 Deployment ID 保证 Activity 重试返回原 ID，数据库唯一索引提供最终双 ACTIVE 防线。
- [x] 完成冻结 `getDeployment`，复用 `agent:read`、显式 tenant 条件和 RLS；不修改 R6 OpenAPI、权限字符串、生成 DTO、基线版本或 SHA-256。多目标场景按 `runtime_target_id` 排序物化默认 Deployment，权威集合始终来自 Deployment ACTIVE 状态。
- [x] 验证通过：定向 `42 passed`；真实 PostgreSQL 迁移/RLS `1 passed`、Release/Deployment 初次激活、替换、历史、重试、预校验失败、过期 fencing、并发最终一致和跨租户读取 `1 passed`；`make backend-check` 为 `279 passed, 7 skipped`，PostgreSQL/Temporal `make check-all` 为后端 `286 passed, 0 skipped`、前端 `24 passed`，R6 31 个完整性文件、167 个 operationId 和 16 个生成文件零漂移。
- 依赖与部署：未新增 Python/Node 依赖，`uv lock --check` 和 `git diff --check` 通过；部署前执行 Alembic `0013_deployment_activation`。若应用使用非对象所有者的最小权限数据库角色，需为 Release identity sequence 授予 `USAGE, SELECT`；降级会删除 Deployment 历史，仅允许在确认无需保留发布事实时执行。
- 已知外部前置：AP-E1-009 AgentScope Registry manifest digest 仍等待现有 CI/制品仓库回填，未回填时 AP-E2-003 Validator 与本阶段 Activator 均继续失败关闭，不切换旧 ACTIVE。
- 下一任务建议：`AP-E2-005` 发布 Diff、Release/Deployment 历史查询和 Vue 发布页面；只读消费本阶段历史事实，不提前实现 AP-E2-006 回滚或 Session/Run Deployment 固定。

### 2026-08-07 — AP-E2-005 发布预览、Snapshot Diff、版本历史和 Vue 发布页面

- [x] 基线升级为 `agent-platform-v1-dev-baseline-2026-08-r7`，Core OpenAPI 升级到 `1.4.0` 并新增 `previewAgentPublish`；AI Coding 总纲、执行计划、核心接口、前端交互、需求追踪、测试契约、文档索引、SHA-256 和生成 DTO/Client 已同步。
- [x] 新增确定性脱敏 Snapshot Diff：按 JSON Pointer 稳定排序，区分 resource_version/permission/network/sandbox/model/secret_reference/runtime；Secret、credential、Prompt 正文、长字符串和复杂值只返回引用或稳定摘要。
- [x] 实现 AgentVersion 游标列表/详情、历史 Snapshot Diff 和按 Runtime Target 的发布预览；所有查询校验 tenant + Agent + Snapshot 归属。Preview 在 PostgreSQL `REPEATABLE READ + READ ONLY` 事务中复用正式绑定解析与 `compile_agent_snapshot`，不创建 Version、Snapshot、Release、Outbox、幂等或发布审计事实。
- [x] `ready_to_publish` 落实为 Snapshot 前置判断：根 Agent 缺少已发布 Sandbox Profile，或 AgentScope 缺少已发布 ModelConfig 时返回 false；Vue 页面显示缺失项并阻止提交。Registry digest、签名、SBOM、扫描、Smoke 和激活继续由正式 Release 重验，不在 Preview 中伪造通过。
- [x] 新增部署侧 `create_database_publication_app/build_database_publication_services`，由部署层注入已解析 Session Factory、Identity Provider 和受信 Runtime Target 配置；发布、Deployment、历史、Diff 和 Preview 不再只存在于测试注入，默认无配置应用仍失败关闭。
- [x] 新增 `/agents/:id/publish` Vue 页面和 Agent 编辑器入口：支持 Runtime Target、发布说明、Smoke/激活选项、解析版本、首次/增量脱敏 Diff、发布防重复、Release 轮询、Deployment 详情、版本历史分页/详情/比较和卸载取消；未提前实现 AP-E2-006 rollback。
- [x] 全链路验证通过：定向后端 `15 passed`、真实 PostgreSQL 发布查询/无副作用 `1 passed`；`make check` 后端 `285 passed, 7 skipped`、前端 `29 passed`；PostgreSQL/Temporal `make check-all` 后端 `292 passed, 0 skipped`、前端 `29 passed`。R7 31 个完整性文件、168 个 operationId 和 16 个生成文件零漂移，Black、Ruff、Pyright、Prettier、ESLint、Vue TypeScript、Vite 生产构建、`uv lock --check` 和 `git diff --check` 通过。
- 依赖与迁移：未新增 Python/Node 依赖或数据库迁移；复用 `0011`～`0013` 不可变 Snapshot、Release、Deployment 历史事实。
- 历史协同结论：真实数据库场景保留“旧 Deployment 可运行、当前 Draft 因失效/递归绑定不可再次发布”的合法状态；Preview 对无效 Draft 失败关闭，修复 Draft 后成功且写入计数不变，未修改旧 Deployment 或放宽发布规则。
- 后续前置：Sandbox Profile 管理入口仍需独立任务接入 Agent 编辑器/资源 API；外部 CI 仍需回填 AgentScope Registry manifest digest。两者未满足时页面或 Release 会明确阻断，不伪造生产可用。
- 下一任务建议：`AP-E2-006` 以历史 Snapshot 创建新的回滚 Release，补齐发布/回滚 E2E；不得复活旧 Deployment，也不得绕过 R7 Preview、供应链门禁或 fencing。

### 2026-08-07 — AP-E2-006 历史 Snapshot 回滚 Release 与发布回滚 E2E

- [x] 保持 R7 `rollbackAgent` 公共契约不变，补齐 API、应用服务和 PostgreSQL Store；继续使用 `agent:publish`，目标 Snapshot 必须同时属于当前 tenant 和 Agent，跨租户请求按不存在处理。
- [x] 新增迁移 `0014_release_rollback`，Release 内部显式保存 `PUBLISH/ROLLBACK` 和 `requested_snapshot_id`；`expected_agent_version` 只表达普通发布 Draft CAS，回滚不再借用当前 Draft 版本语义。
- [x] 回滚原子创建新的 Release、Operation、审计和 Outbox，按 Idempotency-Key 稳定重放；不修改历史 AgentVersion/Snapshot/Release/Deployment，也不创建伪造的新 AgentVersion。
- [x] Temporal 校验阶段按 Release 数据库事实选择路径：普通发布继续编译 Draft Snapshot，回滚校验历史 Snapshot 的 Agent 归属后直接复用；后续 Bundle、Registry digest、签名/SBOM、扫描、Smoke、Activation 和单调 fencing 链路保持统一。
- [x] Vue 版本历史增加 Element Plus 回滚确认；使用当前 Runtime Target 集合创建回滚 Release，轮询同一 `getRelease` 并展示新 Deployment，不把旧 Deployment 重新标记为 ACTIVE。
- [x] 真实 PostgreSQL E2E 覆盖“激活较新 Snapshot → 回滚历史 Snapshot”：回滚产生全新 Release/Deployment，较新 ACTIVE 被原子退役，回滚前后 AgentVersion 数量不变，跨租户目标不可解析，审计动作是 `agent.rollback.requested`。
- [x] 迁移验证发现并修正降级边界：降级到 0013 会保留 Release/Deployment/Snapshot 历史，并把回滚行回填为旧 Schema 可接受的 Agent 版本；由于旧 Schema 无来源字段，`ROLLBACK` 分类和 `requested_snapshot_id` 会丢失，生产降级前需明确接受该信息损失。
- [x] 全链路门禁通过：真实 PostgreSQL/Temporal `make check-all` 后端 `297 passed, 0 skipped`、前端 `31 passed`；R7 31 个完整性文件、168 个 operationId 和 16 个生成文件零漂移，Black、Ruff、Pyright、Prettier、ESLint、Vue TypeScript 和 Vite 生产构建通过。
- 依赖与部署：未新增 Python/Node 依赖；部署前执行 Alembic `0014_release_rollback`。外部 CI Registry manifest digest、生产对象存储、签名、SBOM、扫描和 Smoke Adapter 仍由部署环境注入，缺失时发布与回滚继续失败关闭。
- Epic 2 结果：Snapshot、Bundle、Release、Deployment、预览/Diff/历史和回滚闭环已完成。下一任务建议进入 `AP-E3-001` Session CRUD、归档、删除约束和分页，并在 Session 创建时开始落实 Deployment 固定边界。

### 2026-08-08 — AP-E3-001 Session CRUD、归档、删除约束和分页

- [x] 新增任务包 `harness/tasks/AP-E3-001.yaml`，固定 R7 契约和 Session/Deployment/Run 边界；未修改 OpenAPI、JSON Schema、基线版本、SHA-256 或生成文件。
- [x] 新增领域 `SessionRecord`、应用 `SessionManagementService`、PostgreSQL `SqlAlchemySessionStore` 和冻结 6 个 Session 路由；后端代码位于 `backend/`。
- [x] 新增迁移 `0015_chat_session`：Session 表、用户/Agent/Deployment 复合外键、生命周期 Check、分页索引、FORCE RLS，以及 tenant-admin `session:create/read/list/update/delete` 权限回填。
- [x] Session 创建只读取当前 Agent 的 `active_deployment_id`，校验 Deployment 为 `ACTIVE/DEGRADED` 后不可变写入；客户端不能指定 Deployment，后续发布/回滚不会更新已有 Session。
- [x] list/get/update/archive/delete 始终按当前 tenant + actor user 过滤；更新/状态变更使用强 ETag CAS；创建、归档、删除复用共享幂等，删除只允许 `ARCHIVED → DELETED`，生成同步终态 `SUCCEEDED` Operation 并保留延迟删除墓碑。
- [x] 默认列表隐藏 DELETED，显式 `status=DELETED` 可查看当前用户墓碑；非终态 Run 删除检查留待 AP-E3-003 的 `agent_run` 表和真实状态机接入，不创建占位表。
- [x] 新增 Vue `/sessions` 页面、服务层和 4 个前端测试：游标分页、Agent 筛选、新建、重命名、归档、归档后删除、Operation 轮询、loading/empty/error/窄屏布局；前端代码位于 `frontend/`。
- [x] 全链路验证通过：`make contract-check`（R7、168 operationIds、16 生成文件零漂移）、`uv lock --check`、`git diff --check`；后端 `make check-all` 为 `307 passed, 0 skipped`，前端 `35 passed`，Prettier、ESLint、Vue TypeScript、Vite 构建全部通过。
- [x] 真实 PostgreSQL 验证覆盖迁移升级/降级、Session RLS、权限回填、跨租户/跨用户不可见、Deployment 固定、创建幂等、分页、ETag、归档前置删除、终态 Operation、审计摘要和发布/回滚后旧 Session 仍固定原 Deployment。
- 依赖与部署：未新增 Python/Node 依赖；部署前执行 Alembic `0015_chat_session`。降级会删除 Session 历史和权限回填，仅在确认无需保留 Session 事实时执行。
- 协同结论：平台 Session 与 Runtime Session 继续分离；AP-E3-002 必须复用本阶段的 tenant/user 过滤和固定 Deployment，不得从 Agent 当前 Draft 或新发布重新解析历史 Session。
- 下一任务建议：`AP-E3-002` Message 历史、分支顺序和权限；随后 `AP-E3-003` 再把删除的非终态 Run 约束补入同一事务。

### 2026-08-08 — AP-E3-002 Message 历史、分支顺序和权限

- [x] 保持 R7 `listSessionMessages`、`Message` 和 `MessagePage` 契约不变，新增只读应用服务、FastAPI 路由和 PostgreSQL Store；查询同时要求当前 tenant、actor Session 所有权、`session:read` 与 `message:list`。
- [x] 新增迁移 `0016_chat_message`：不可变 `chat_message`、复合 Session/Parent/Cursor 外键、分支父节点唯一索引、FORCE RLS、UPDATE/DELETE 拒绝触发器，以及 tenant-admin `message:read/list` 权限回填。
- [x] `parent_message_id` 作为顺序权威来源；默认从 Session `cursor_message_id` 回溯当前链，显式 `branch_id` 从唯一 Tip 回溯并包含继承祖先；分页 Cursor 固定原 Tip，后续追加消息不会改变已开始的分页视图。
- [x] DELETED Session 不返回消息正文，ACTIVE/ARCHIVED 保持只读；跨租户、跨用户、跨 Session Parent、空内容数组和消息变更均在应用或数据库边界拒绝。
- [x] 新增 Vue `/sessions/:id/messages` 页面和 Session 列表入口，支持分支筛选、游标分页、链路元数据与 text/artifact/tool/error 分片纯文本展示；不使用 `v-html`，未提前实现聊天输入、Run 或 SSE。
- [x] 全链路验证通过：真实 PostgreSQL 定向 `2 passed`；PostgreSQL/Temporal `make check-all` 后端 `323 passed, 0 skipped`、前端 `38 passed`；R7 31 个完整性文件、168 个 operationId 和 16 个生成文件零漂移。
- 依赖与部署：未新增 Python/Node 依赖；部署前执行 Alembic `0016_chat_message`。降级会删除消息历史，仅应在确认无需保留会话事实时执行。
- 协同结论：AP-E3-003 必须复用本阶段不可变 Message 和 Session Cursor，在创建 Run 的同一事务写入 USER Message、推进 Cursor，并补齐 Session 删除的非终态 Run Guard；Artifact/Tool/Run 外键等待真实事实表，不创建占位表。
- 下一任务建议：`AP-E3-003` Run 创建、Session 并发保护和不可变执行输入快照。

### 2026-08-08 — AP-E3-003 Run 创建、幂等、状态机和 Snapshot 绑定

- [x] 保持 R7 `listSessionRuns`、`createRun`、`getRun`、`RunCreateRequest/RunAccepted/Run/RunPage` 不变；新增 Run 应用服务、FastAPI 路由和真实 PostgreSQL Store，当前用户所有权与 `run:create/read/list` 权限边界已接通。
- [x] 按人工批准的方案 1，创建事务只追加 USER Message，并原子写入 AgentRun CREATED、`agent.run_requested.v1` Outbox、脱敏审计、Session Cursor 和幂等完成事实；`assistant_message_id` 保持 NULL，不创建可变或虚假占位消息。
- [x] 新增迁移 `0017_agent_run`、`agent_run/run_attempt` ORM 和领域状态机：Deployment/Agent/Snapshot、USER/ASSISTANT Message、Session、重试来源均由租户复合外键约束；Run 输入由数据库触发器保护，Assistant 绑定只允许后续一次性 `NULL → 同 Run ASSISTANT Message`。
- [x] Session 行锁和活动 Run 部分唯一索引共同保证同一 Session 分支只有一个非终态 Run，稳定冲突码为 `RUN_ALREADY_ACTIVE`；Run 创建幂等重放返回原 `RunAccepted`，同键不同请求继续使用共享 `IDEMPOTENCY_KEY_REUSED`。
- [x] Run 固定精确 Deployment 和 Snapshot：Session 默认 Deployment 后续处于 RETIRED 仍可供历史 Session 使用；客户端显式 Deployment 仅接受同 Agent 的 ACTIVE/DEGRADED。Artifact 事实表尚未落地，非空附件失败关闭且不保存裸引用。
- [x] Session 删除事务已补齐非终态 Run Guard；ARCHIVED Session 保留 Run 历史只读但不可再创建，DELETED Session 不返回 Run。Message 的 UPDATE/DELETE 拒绝触发器保持不变。
- [x] 真实 PostgreSQL 验证覆盖原子写入、审计不含用户正文、幂等、并发双请求、固定 Snapshot、RETIRED 默认 Deployment、显式 Deployment 拒绝、跨租户/跨用户隔离、RLS、Run 输入不可变、Session 删除 Guard 和迁移升级/降级。
- [x] 全链路门禁通过：局部 `59 passed`；`make backend-check` 为 `332 passed, 8 skipped`；PostgreSQL/Temporal `make check-all` 后端 `340 passed, 0 skipped`、前端 `38 passed`；R7 31 个完整性文件、168 个 operationId 和 16 个生成文件零漂移，Black、Ruff、Pyright、Prettier、ESLint、Vue TypeScript 和 Vite 构建通过。
- 依赖与部署：未新增 Python/Node 依赖；部署前执行 Alembic `0017_agent_run`。降级会删除 Run/RunAttempt 历史和 Run 权限，并解除 Message 来源 Run 外键，仅应在确认无需保留执行事实时执行。
- 历史问题评估：修正 PostgreSQL RLS 测试中新增表名的预期排序；同时修复默认应用未装配数据库服务时 Session/Message/Run 路由可能在鉴权前访问空服务并返回 500 的同型问题，现统一返回稳定 `DEPENDENCY_UNAVAILABLE`。两项均未改变业务契约或成功路径，发布、回滚、Session、Message、前端和契约链路均通过回归。
- 后续前置：`AP-E3-004` 开始前同步正式数据库设计，把 `assistant_message_id` 调整为创建期 nullable，并明确最终化事务只 INSERT ASSISTANT Message、一次性绑定 Run 和推进 Cursor；随后实现 Workflow/Activity、RunAttempt 分配与 RunSpec 读取。

### 2026-08-08 — AP-E3-004 AgentRunWorkflow、Activity、Signal 和 Query

- [x] 完成 R8 正式基线同步：方案 1 已进入数据库、领域、架构、Temporal、核心接口、AI Coding、执行计划、测试和索引文档；31 个完整性文件、168 个 operationId 与 16 个生成文件保持一致。
- [x] 新增版本化 `AgentRunWorkflowInput/State/Result`、RunSpec Ref、RuntimeCompletion、Finalize、`cancel_run` Signal 和 `run_state` Query；Workflow History 只保存引用、Hash、Attempt 与小型终态摘要，不保存 Prompt 或高频事件。
- [x] 新增 `AgentRunWorkflowActivities` 及 RunSpecCompiler、RunRuntimeExecutor、FencingTokenIssuer、RunWorkflowStore 端口；准备阶段先幂等绑定 Workflow/分配首个 Attempt，再通过确定性 token 编译不可变 RunSpec，执行阶段默认不自动重试用户 Prompt。
- [x] PostgreSQL 完成 `CREATED → QUEUED → PREPARING → RUNNING → SUCCEEDED/FAILED` 和 `ALLOCATED → STARTING → RUNNING → COMPLETED/LOST`；重复准备、启动和最终化不重复分配 Attempt 或 Message。
- [x] 成功最终化事务只 INSERT 一条 ASSISTANT Message，并原子绑定 `assistant_message_id`、推进 Session Cursor、完成 Run/Attempt；失败或 RunSpec 准备失败不创建回复。ASSISTANT Message 的 UPDATE/DELETE 仍被数据库拒绝。
- [x] 新增确定性 `run/{tenant_id}/{run_id}` Starter、Run Worker Definition 和 HMAC fencing token 发行器；token 只以 `SecretStr` 短暂存在，数据库仅保存 SHA-256。正式 Outbox Router 接线和启动结果回写保留给 AP-E3-006。
- [x] 历史协同修正：删除 `RunAttemptModel` 中 R8 数据库设计和 `0017` 迁移均不存在的 `created_by` 字段，避免实际 Attempt 读写产生 ORM/数据库列漂移；无需新增迁移。
- [x] 验证通过：定向单元 `19 passed`、Temporal Test Environment `6 passed`（含 Query、重复 Signal、准备失败和 History Replay）、真实 PostgreSQL `1 passed`；`make backend-check` 为 `342 passed, 10 skipped`，真实 PostgreSQL/Temporal `make check-all` 为后端 `352 passed, 0 skipped`、前端 `38 passed`。
- 依赖与部署：未新增 Python/Node 依赖或数据库迁移，`uv lock --check` 通过。生产 RunSpec 对象存储编译器、真实 Runtime Executor 和部署 Secret 解析必须显式注入，缺失时不会伪造执行成功。
- 下一任务建议：`AP-E3-005` 复用本阶段 Signal/Query、RunAttempt 和 fencing 边界，实现 cancelRun/retryRun、Runtime cancel/inspect、取消/超时竞态及安全的新 Attempt 接管；不得提前把 E3-006 Outbox 对账或 Epic 4 RunEvent/SSE 混入。

### 2026-08-08 — AP-E3-005 取消、重试、新 Run 语义和 fencing token

- [x] 按 R8 冻结 OpenAPI 实现 `cancelRun/retryRun` FastAPI 路由、应用服务、当前用户所有权、`run:cancel/retry` 权限和共享幂等；新增 `0018_run_control_permissions` 为既有 tenant_admin 回填控制权限，不修改冻结公共 DTO。
- [x] Cancel 事务只把非终态 Run 推进到 `CANCELLING`；已终态请求幂等返回当前事实。Workflow 通过确定性 Temporal Signal、`inspect_agent_runtime_v1`、`cancel_agent_runtime_v1` 和最终化 Activity 协同，只有 Runtime 已停止/已不存在时才写 `CANCELLED`，未知取消结果失败关闭且不创建 ASSISTANT Message。
- [x] Retry 始终创建全新 Run 并设置 `retry_of_run_id`；`original_snapshot` 固定原 Deployment/Snapshot，`current_deployment` 只解析同 Agent 当前 ACTIVE/DEGRADED Deployment。新 Run 复用原不可变 USER Message，不追加重复 Prompt、不修改旧 Run/Attempt/Message，并阻止 Session 已前移时隐式回退历史分支。
- [x] 新增 RuntimeInspection/Cancellation/Recovery 1.1 内部契约和 Runtime Controller Port。执行 Activity 异常后先 inspect：只有 `LOST + safe_to_retry` 才有界创建 `attempt_no + 1`；RUNNING/UNKNOWN/不安全 LOST 终结为 `SESSION_NOT_RECOVERABLE`，不会自动重放 Prompt。
- [x] 新 Attempt 使用新的确定性 HMAC fencing token，明文只在 Activity/Runtime 控制请求内短暂存在；数据库和 Workflow History 不保存明文。`mark_run_running/finalize_run/finalize_cancellation` 校验当前 Attempt Hash，旧 Attempt/token 无法推进新 Attempt 或写入新终态。
- [x] Temporal 使用 patch marker 隔离 AP-E3-005 控制语义；重复 Signal 幂等，执行 Activity 可受控中断。真实 Test Environment 覆盖运行中取消、LOST 安全恢复、UNKNOWN 不重放和 History Replay。
- [x] 验证通过：定向单元 `34 passed`；真实 Temporal `8 passed`；真实 PostgreSQL `1 passed`，覆盖 CREATED/RUNNING 取消、终态重复取消、Retry Message 复用、旧 token 拒绝和两代 Attempt；真实 PostgreSQL/Temporal `make check-all` 为后端 `363 passed`、前端 `38 passed`，契约 31 个完整性文件、168 个 operationId 和 16 个生成文件零漂移。
- 依赖与部署：未新增 Python/Node 依赖，`uv lock --check` 与 `git diff --check` 通过；部署前执行 Alembic `0018_run_control_permissions`。生产 API 组合必须注入 `TemporalRunWorkflowControl`，Run Worker 必须同时注入 Runtime Executor、Runtime Controller、RunSpec Compiler 和 fencing Secret。
- 协同与剩余边界：正式 Run Outbox Router、Temporal 启动结果回写、长时间 CREATED/CANCELLING Reconciler 属于 `AP-E3-006`；`current_deployment` 的 Snapshot Diff 与二次确认由后续 Run UI/Epic 3 纵向验收实现，本阶段未提前引入 Epic 4 RunEvent/SSE。
- 下一任务建议：`AP-E3-006` 将 `agent.run_requested.v1` 接入正式 Outbox Router，持久化 Workflow 启动映射，并对无 Workflow 的 CREATED 与长时间 CANCELLING Run 提供幂等对账入口。

### 2026-08-08 — AP-E3-006 Outbox dispatcher、Workflow 映射和对账入口

- [x] 完成 R9 基线同步：数据库、领域、架构、Temporal、AI Coding、执行计划、测试和索引明确启动确认与对账语义；31 个完整性文件、168 个 operationId 和 16 个生成文件保持一致，公共 OpenAPI/Run DTO/RunSpec/RunEvent 未改变。
- [x] 通用 `OutboxDispatcher` 在 Temporal Start/Already Exists 后、Outbox PUBLISHED 前调用结果回写 Port；正式组合 Router 同时注册 Probe、Release 和 `agent.run_requested.v1`，禁止额外路由覆盖冻结事件。
- [x] 新增 `0019_run_workflow_reconciliation`：持久化 `temporal_run_id`、`workflow_start_outcome`、`workflow_started_at`、`cancelling_at`，增加 tenant+workflow 唯一约束、完整性 Check 和 CANCELLING 对账索引；历史只有 workflow_id 的 Run 保持兼容。
- [x] 启动映射同时校验 tenant、aggregate 与确定性 Workflow ID；同一映射重复回写幂等，Temporal 未返回 Run ID 时保持可重试，不把已启动 Workflow 误入死信。
- [x] 新增 CREATED/CANCELLING Reconciler 和 bounded runner：Temporal 不存在时恢复既有 PENDING/DEAD/缺失 Run Outbox，存在时补启动映射；运行中的 CANCELLING 重发稳定 signal_id，Temporal 已终态但数据库仍非终态只计入 unresolved，不直接写 CANCELLED。
- [x] 新增低基数 `agent_platform_run_reconciliation_total{outcome}` 指标；未将 tenant_id、run_id 或 workflow_id 作为 Label。真实 PostgreSQL 验证发现并修正补偿聚合名必须复用历史 `aggregate_type=run`，避免产生第二条启动事件。
- [x] 验证通过：定向单元 `49 passed`；真实 PostgreSQL `1 passed`；真实 Temporal `8 passed`；PostgreSQL/Temporal `make check-all` 后端 `375 passed`、前端 `38 passed`，Black、Ruff、Pyright、Prettier、ESLint、Vue TypeScript 和 Vite 构建全部通过。
- 依赖与部署：未新增 Python/Node 依赖；`uv lock --check`、`git diff --check` 通过。部署前执行 Alembic `0019_run_workflow_reconciliation`。生产 Event/Reconciliation 进程仍需部署层注入 Secret Backend、数据库 DSN、服务 TenantContext Source 和 Temporal Client；当前主入口继续失败关闭，不会伪造 Worker 已可部署。
- 协同结论：Session、Message 不可变性、RunAttempt/fencing、取消/重试和 Temporal History 语义保持不变；本阶段未创建 RunEvent、SSE 或 AG-UI 旁路。
- 下一任务建议：`AP-E3-007` 使用 RuntimeEventCandidate 测试 Fake 和 `getRun` 轮询完成 Epic 3 纵向验收；生产 Worker 装配、深度终态修复、告警与恢复演练留在 Epic 7。

### 2026-08-08 — AP-E3-007 RuntimeEventCandidate Fake 与 Epic 3 纵向验收

- [x] 新增内部 `RuntimeEventCandidatePublisher` Port；Runtime Executor 在 Activity 内直接发布冻结 Candidate，不把高频事件作为 Activity 返回值写入 Temporal Workflow History。
- [x] `RunExecutionRequest` 增加 Activity 内存态 `SecretStr` fencing token；同一 token Hash 用于 `mark_run_running`，明文只传给 Runtime/Publisher，数据库继续只保存 SHA-256。
- [x] 测试 Fake 产生 `run_started/text_message_start/text_delta/text_message_end`，全部经过冻结 TypeAdapter；重复 `source_event_id` 的第 5 次发布被 Fake Publisher 幂等收敛为 4 条外部候选事实。
- [x] 真实纵向链路覆盖 HTTP `createRun`、正式 Outbox Router、Temporal `AgentRunWorkflow`、真实 PostgreSQL Activity Store、Fake Runtime/Publisher 和 HTTP `getRun` 轮询，最终状态为 `SUCCEEDED`。
- [x] 协同事实验证：Outbox 为 `PUBLISHED`，Workflow ID/Temporal Run ID/启动结果完整，RunAttempt 为 `COMPLETED`，Session Cursor 指向唯一 ASSISTANT Message；USER/ASSISTANT Message 的 UPDATE/DELETE 均被数据库拒绝。
- [x] Epic 4 边界验证：Candidate source ID 和 Delta 不进入 Workflow History，数据库不存在 `run_event` 表，公共 `getRun.latest_sequence_no` 仍为 0；未实现 Event Store、sequence 分配、Redis、SSE 或 AG-UI。
- [x] 回归通过：定向单元/AgentScope/Worker `17 passed`；真实 PostgreSQL 纵向 `1 passed`；真实 Temporal 取消、恢复和 Replay `8 passed`；真实 PostgreSQL/Temporal `make check-all` 后端 `375 passed`、前端 `38 passed`。
- 依赖、迁移与契约：未新增 Python/Node 依赖，未新增数据库迁移；R9 公共 OpenAPI、Run DTO、RunSpec、RunEvent Schema 和前端生成代码不变，31 个完整性文件、168 个 operationId、16 个生成文件零漂移。
- 历史问题评估：首次纵向运行未 claim 新 Outbox，确认是测试固定时间早于 API 创建时间，不是生产 Dispatcher 缺陷；改为以 dispatch 时钟计算可见时间后通过，未修改生产重试或消息语义。
- Epic 3 结果：Session、不可变 Message、Run/Attempt、Outbox、Temporal、取消/重试、fencing、对账入口和状态轮询已形成完整可验证闭环。生产 RunSpec/AgentScope/Event Publisher 进程装配仍按计划留给 Epic 7，Event Store/SSE 从 `AP-E4-001` 开始。

### 2026-08-08 — AP-E4-001 RunEvent 表、计数器、约束和迁移

- [x] 新增 `0020_run_event_store`、`run_event` 和独立 `run_event_counter`；字段、V1 版本、20 个冻结事件类型、`run_id + sequence_no`、`run_id + execution_attempt + source_event_id`、256KB Payload Check 和复合 Run/Session 外键完整。
- [x] 两表启用 ENABLE/FORCE RLS；RunEvent 使用数据库触发器拒绝 UPDATE/DELETE，Counter 保留 AP-E4-002 所需原子 UPDATE 能力。未新增公共写入 API、Candidate Writer、查询、SSE、Redis 或 AG-UI 旁路。
- [x] 人工确认本阶段使用非分区普通表：PostgreSQL 16 无法同时原生满足 `recorded_at` 月分区和不含分区键的冻结全局唯一约束，当前优先保证序号与幂等正确性，且没有把 `recorded_at` 加入唯一键弱化语义。
- [x] 真实 PostgreSQL 覆盖普通表类型、升级/降级、双唯一冲突、Payload 超限、不可变触发器、Counter 更新和跨租户不可见/不可写；历史 Epic 3 验收同步改为“表存在但 Candidate 尚未写入，`latest_sequence_no=0`”。
- [x] 验证通过：模型/迁移 `37 passed`，真实 PostgreSQL RLS `1 passed`、完整资源/事件链路 `1 passed`；带真实 PostgreSQL/Temporal 的 `make check-all` 后端 `377 passed`、前端 `38 passed`，R9 契约、31 个完整性文件和 16 个生成文件零漂移。
- 依赖与部署：未新增 Python/Node 依赖；部署前执行 Alembic `0020_run_event_store`。降级会删除 RunEvent/Counter 事实，仅允许在确认无需保留事件历史时执行。
- **硬性容量待办**：生产达到 RunEvent 分区容量阈值前，必须建立“全局唯一登记表 + 按 `recorded_at` 月分区事件表”，持续保证 event_id、Run sequence 和 attempt/source 全局唯一，并提供双写、完整性核对、存量迁移、切换与回滚方案；该项不得以增加 `recorded_at` 到唯一键的方式降级幂等语义。
- 下一任务建议：`AP-E4-002` Candidate 校验、批量写入、Counter 原子分配、`AgentRun.latest_sequence_no` 同事务物化和终态保护。

### 2026-08-08 — AP-E4-002 Candidate 校验、批量写入、幂等序号和终态保护

- [x] 新增 `RunEventIngestionService`、PostgreSQL Store 和冻结内部批量路由；只接受可信 service workload context 与 `internal:event_write`，部署未注入 mTLS/SPIFFE Adapter 时失败关闭。
- [x] 按 Run 行锁串行化批次，校验当前 Attempt 和 SHA-256 fencing hash；只为 created 项连续分配序号，并在同一事务写 RunEvent、Counter、`AgentRun.latest_sequence_no` 和一条 `run.events_appended.v1` Outbox。
- [x] 支持跨请求与同批 `run_id + attempt + source_event_id` 幂等；相同内容返回原 event_id/sequence_no，不同内容逐项 `IDEMPOTENCY_KEY_REUSED`，部分失败不阻断其他合法事件。
- [x] stale fencing 返回批次级 409 `EXECUTION_FENCING_REJECTED`，且拒绝审计先提交；终态冲突、Finalizer 尚未物化终态和结果 Message/错误不匹配均逐项拒绝并审计。
- [x] 为保持既有 Session/Message/Run 协同，Event Service 不绕过 Finalizer 修改 Run 终态；Finalizer 前拒绝终态 Candidate，Finalizer 后只接受匹配事实，相同终态重放幂等，冲突终态拒绝。
- [x] 新增 `SqlAlchemyRuntimeEventCandidatePublisher`；Publisher 被拒绝时向 Activity 传播稳定错误。Temporal Outbox Store 只领取已注册 Workflow 事件，RunEvent Outbox 留给 AP-E4-004 Event Dispatcher。
- [x] 真实 PostgreSQL 验收覆盖 10 个并发批次/100 个 Candidate、序号 1～100、同批/跨批重复、key 复用、部分成功、stale fencing、终态保护、Outbox、审计、不可变 Message、Attempt 和 Session Cursor；最终 Run 事件序号推进到 102。
- [x] 验证通过：定向单元/API/Publisher/Composition `16 passed`，真实 PostgreSQL纵向 `1 passed`；`make backend-check` 为 `376 passed, 12 skipped`；真实 PostgreSQL/Temporal `make check-all` 为后端 `388 passed`、前端 `38 passed`。
- 契约与依赖：R9 的 31 个完整性文件、168 个 operationId 和 16 个生成文件零漂移；未新增 Python/Node 依赖和数据库迁移。
- 部署与待办：生产必须注入可信 Internal Service Identity Adapter；`run.events_appended.v1` 的 Redis/SSE Dispatcher 属于 AP-E4-004；“全局唯一登记表 + recorded_at 月分区事件表”仍为生产容量阈值前硬性待办。
- 下一任务建议：`AP-E4-003` 实现事件查询、分页、回放和序号缺口语义，复用 PostgreSQL 事实、`latest_sequence_no` 和现有 RLS，不提前加入 SSE、Redis 或 AG-UI。

### 2026-08-08 — AP-E4-003 RunEvent 查询、分页、历史回放和序号缺口保护

- [x] 接通冻结 `listRunEvents`：普通用户通过 `run:read` 和当前 Session 所有权读取，跨租户、跨用户、Run 不存在或 Session 已删除统一返回 404；内部 `internal:event_write` 身份未被复用于用户查询。
- [x] PostgreSQL 先固定 `AgentRun.latest_sequence_no`，再按 `sequence_no > after AND sequence_no <= latest_sequence_no` 升序读取 `limit + 1`；`after ==/> latest` 合法返回空页，并避免并发追加污染本次页视图。
- [x] 每条数据库事实通过冻结 `RUN_EVENT_ADAPTER` 校验后返回；首序号、相邻序号或页尾存在缺口时稳定返回 409 `RUN_EVENT_SEQUENCE_GAP` 和非敏感定位详情，不跳过缺口伪造完整回放。
- [x] 协调安全与连续序号：无 `run:view_sensitive` 时保留 `thinking_delta` 的事件标识和序号，仅把正文替换为固定脱敏文本；有权限时返回原内容，不过滤事件制造 Reducer 永久补洞。
- [x] 真实 PostgreSQL 链路复用 AP-E4-002 写入事实，按 37 条分页完整回放 1～102，`latest_sequence_no=102`，并验证其他用户不可见；Run/Attempt、不可变 Message、Session Cursor、Outbox 和终态保护均未回归。
- [x] 验证通过：定向应用/API/Composition `14 passed`；`make backend-check` 为 `383 passed, 12 skipped`；真实 PostgreSQL/Temporal `make check-all` 为后端 `395 passed`、前端 `38 passed`。R9 的 31 个完整性文件、168 个 operationId 和 16 个生成文件零漂移。
- 契约、依赖与迁移：未修改冻结 OpenAPI/RunEvent Schema/生成类型，未新增 Python/Node 依赖或数据库迁移；继续复用 `0020_run_event_store` 和非分区事实表。
- 历史问题与风险：本轮没有发现需要扩大范围修改的历史遗留问题；`.npmrc` 读取权限和 Rollup 上游 PURE 注释仅产生既有非阻断告警。Thinking 脱敏保持 Schema 兼容，后续 AG-UI/Vue Reducer 必须按权限忽略脱敏正文。
- 下一任务建议：`AP-E4-004` 实现 Event Outbox Dispatcher、Redis 唤醒和 SSE；必须先补历史再订阅实时，按 sequence_no 去重，支持 Last-Event-ID、背压与终态关闭，Redis 不作为事实源。
