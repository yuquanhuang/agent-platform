# Agent 平台 AI Coding 开发总纲

> 文档版本：V1.4  
> 文档状态：开发输入基线  
> 基线清单：[agent-platform-baseline.yaml](./agent-platform-baseline.yaml)

## 1. 目的

本文把需求、架构和契约转换为可交给 AI Coding 工具执行的工程规则，目标是减少隐含假设、重复返工和跨模块契约漂移。

平台定位为使用 Python 3.12 从零建设的通用 Agent 平台，不迁移、不重构、不兼容任何既有平台。任何外部项目只能作为非权威技术参考，不能改变本基线的实体、接口、状态和安全边界。

## 2. 开发目标

交付一个可以私有化部署、支持多租户的通用 Agent 平台，至少完成以下闭环：

```text
身份与租户
→ 模型、Prompt、Skill、MCP 等资源定义
→ Agent 草稿和版本发布
→ 不可变 Snapshot 与 Runtime Bundle
→ Session、Message、Run
→ Temporal 编排
→ AgentScope 或 Codex ACP Runtime
→ Sandbox、Workspace、Artifact
→ RunEvent 持久化和 SSE/AG-UI 输出
→ 审批、审计、监控、恢复和回滚
```

## 3. 技术基线

| 领域 | 固定基线 |
|---|---|
| 语言 | CPython 3.12 |
| 依赖管理 | `pyproject.toml + uv.lock` |
| API | FastAPI、Pydantic v2、OpenAPI 3.1 |
| 数据库 | PostgreSQL、SQLAlchemy 2.x Async、Alembic |
| 工作流 | Temporal Python SDK；所有 Run 和发布使用 Workflow |
| Runtime | AgentScope 2.0.x；Codex 通过 ACP STDIO 接入 |
| 事件 | 内部强类型 RunEvent；Event Service 分配序号 |
| 前端出口 | SSE；RunEvent 转换为 AG-UI |
| 前端工程 | Vue 3 Composition API、TypeScript、Vite、Vue Router 4、Pinia、`@tanstack/vue-query`、Element Plus |
| 缓存与通知 | Redis 只用于缓存、锁辅助和通知，不作为事件事实来源 |
| 对象存储 | S3 兼容接口；开发环境 MinIO |
| 可观测性 | OpenTelemetry、结构化日志、Prometheus 指标 |
| Sandbox | 独立 Sandbox Manager；MVP Run Sandbox |
| 测试 | pytest、契约 Golden Files、Temporal Replay、E2E、安全与容量测试 |

## 4. 权威来源与冲突处理

开发任务必须读取 [agent-platform-baseline.yaml](./agent-platform-baseline.yaml) 指定的版本。

冲突优先级：

1. 已批准 ADR 和安全政策。
2. JSON Schema、OpenAPI 和核心接口事件契约。
3. 领域模型和状态机。
4. 冻结需求与全局业务规则。
5. 架构和流程说明。
6. 示例、页面文案和伪代码。

发现冲突时立即停止受影响部分的编码，记录冲突位置、影响和建议修改，不允许 AI 根据常见实践自行裁决。

### 4.1 任务上下文包

每个 AI Coding 任务固定读取基线清单、本总纲和工程基线，再按任务选择 2～5 个权威契约；禁止无差别加载全部 Markdown。

| 任务类型 | 额外必读 |
|---|---|
| API | 核心接口契约、相关 OpenAPI、安全契约、测试任务包 |
| 数据库 | 领域模型与状态机、数据库详细设计、安全契约 |
| Temporal | Temporal 契约、领域状态机、RunSpec/RunEvent Schema |
| Sandbox | Sandbox/Workspace 契约、SandboxPolicy Schema、安全契约 |
| Runtime | 核心契约、RunSpec、RunEvent、Model Gateway Schema |
| 前端 | 前端交互契约、前端框架 ADR、两个 OpenAPI、RunEvent Schema |

任务包必须记录 `baseline_id` 和相关文件 SHA-256。Hash 与基线清单不一致时不得开始生产编码。

## 5. 工程仓库目标结构

```text
agent-platform/
├── pyproject.toml
├── uv.lock
├── backend/
│   ├── apps/                   # API、Temporal、Runtime、Event、Sandbox 进程
│   ├── packages/               # contracts、domain、application、infrastructure 等
│   ├── migrations/
│   └── tests/
│       ├── unit/
│       ├── contract/
│       ├── integration/
│       ├── e2e/
│       ├── security/
│       ├── replay/
│       └── golden/
├── frontend/
│   ├── src/                    # Vue 3 + TypeScript + Vite 前端应用
│   └── tests/
├── deploy/
│   ├── compose/
│   └── helm/
└── docs/agent-platform/        # OpenAPI、JSON Schema、Golden 示例和设计文档
```

Python 业务代码不得在仓库根目录建立平行 `apps/`、`packages/`、`migrations/` 或 `tests/`；前端业务代码不得放入 `apps/web/`。

模块依赖方向固定为：

```text
API/Worker
→ Application
→ Domain + Contracts
→ Infrastructure Adapter
```

Domain 不得导入 FastAPI、SQLAlchemy ORM、Temporal Client、AgentScope、Codex ACP 或云厂商 SDK。

## 6. 首期纵向切片

首个可合并的生产纵向切片必须同时包含：

1. local/test 使用服务端配置的 Mock OIDC，并解析 tenant/user；生产保留正式 OIDC Adapter。
2. ModelConfig、Prompt、Skill、MCP、Agent Draft 的最小创建能力。
3. 发布生成 Snapshot、AgentScope Bundle 和 ACTIVE Deployment。
4. 创建 Session 和 Run，事务内写 Message、Run、Outbox。
5. Outbox 幂等启动 `AgentRunWorkflow`。
6. Workflow 创建 Run Sandbox 并调用 AgentScope RuntimeAdapter。
7. RuntimeAdapter 输出 RuntimeEventCandidate。
8. Event Service 分配 sequence_no 并持久化 RunEvent。
9. SSE 从 PostgreSQL 回放并实时续传。
10. Artifact 通过 Workspace 校验后导出。
11. 支持取消、超时、Worker 重启和旧 fencing token 拒绝。

首期禁止：

- 使用内存事件列表作为事实来源。
- 使用 FastAPI BackgroundTasks 执行生产 Run。
- Runtime 直接写 RunEvent 表。
- API 进程直接执行 STDIO MCP 或任意 Shell。
- 从 Agent 草稿动态读取运行配置。
- 将 Secret 明文写入 Snapshot、Bundle、RunSpec、事件或日志。

## 7. 契约优先开发顺序

```text
Schema/状态机
→ Pydantic Model
→ Contract Test/Golden File
→ Domain 与 Repository 接口
→ API/Workflow/Adapter 实现
→ 集成测试
→ 页面接入
```

禁止先实现接口再反向生成契约。Pydantic Model 与 JSON Schema 必须保持单一生成源；推荐以 Pydantic Model 生成 JSON Schema，并在 CI 比较生成结果是否漂移。

## 8. 编码约束

### 8.1 类型

- 新增公共函数、Use Case、Adapter 和事件必须完整类型标注。
- 禁止在核心契约中使用裸 `dict`、`list`、`Any`。
- 无法立即识别的 Runtime 原始事件只能进入受限 Raw Trace，不得进入公开 RunEvent。
- 金额使用 Decimal + ISO 4217 currency。
- ID 使用不透明强类型或 NewType，不解析业务含义。

### 8.2 异步与事务

- SQLAlchemy AsyncSession 每个请求或 Activity 独立创建。
- 不跨 `await` 长时间持有数据库事务。
- 外部网络调用不放在数据库事务内。
- 数据库事实与异步动作通过 Outbox 连接。
- Temporal Workflow 不访问数据库、网络、随机数或系统时间；这些操作必须放入 Activity。

### 8.3 幂等与并发

- 副作用操作声明幂等键作用域和请求 Hash。
- 状态变化使用 CAS/条件更新。
- Runtime 接管生成新的 execution_attempt 和 fencing token。
- Retry Run 创建新 Run，不修改原 Run。
- 同一 Session 默认只允许一个非终态主 Run。

### 8.4 安全

- tenant_id 只从认证上下文解析。
- Repository 必须接收 TenantContext，不允许可选 tenant 条件。
- 工具执行前由 Policy Service 和 Tool Gateway 重新授权。
- 用户输入、模型输出、Skill、MCP、文件、知识库内容均为不可信数据。
- Secret 只通过 secret_ref 和短期交换票据使用。

### 8.5 Vue 前端

- 组件默认使用 Composition API 和 `<script setup lang="ts">`，公共逻辑提取为强类型 composable。
- Vue Router 4 统一管理路由、权限守卫和 Feature Flag，不在页面中实现平行权限模型。
- Pinia 管理客户端状态，`@tanstack/vue-query` 管理服务端状态；禁止把 API 实体复制到两个状态源。
- API Client 和 DTO 从 OpenAPI 生成，RunEvent Type 从 JSON Schema 生成或使用同一源码。
- SSE/AG-UI 状态归并必须独立于组件生命周期，并覆盖重复、乱序、缺口和重连测试。
- 前端任务至少执行 Prettier、ESLint、`vue-tsc --noEmit`、相关 Vitest/Vue Test Utils 测试和生产构建。

## 9. AI Coding 任务包模板

每个任务在开始编码前必须提供以下内容：

```yaml
task_id: AP-EPIC-NNN
title: 明确、单一的交付目标
baseline: agent-platform-v1-dev-baseline-2026-08-r4
baseline_integrity:
  hash_algorithm: sha256
  verified_files: []
scope:
  includes: []
  excludes: []
requirements: [FR-XXX-001]
acceptance: [AC-XXX]
authoritative_contracts:
  openapi_operations: []
  json_schemas: []
  state_machines: []
data:
  entities: []
  migrations: []
security:
  actions: []
  audit_events: []
failure_semantics:
  retryable_errors: []
  non_retryable_errors: []
  cancellation: ""
  compensation: ""
file_scope:
  allowed: []
  forbidden: []
dependencies: []
verification:
  commands: []
  golden_files: []
  expected_results: []
open_questions: []
```

`open_questions` 非空时只允许进行不影响生产契约的 Spike。

## 10. AI 提交完成条件

一次 AI Coding 提交只有满足以下条件才算完成：

- 只修改任务允许的文件。
- Schema、代码和测试一致。
- 新状态转换包含合法与非法测试。
- 新 API 包含权限、租户、错误、幂等和审计测试。
- 新 Activity 说明 Retry Policy 和副作用幂等条件。
- 新事件包含 Payload Schema、脱敏和 AG-UI 映射。
- `ruff`、格式化、类型检查、单元测试、契约测试通过。
- 没有新增裸 `dict/Any` 公共契约。
- 没有新增明文 Secret、物理 Workspace 路径或未限资源。
- 更新需求追踪矩阵和必要文档。

## 11. 开发 Epic 0～9

| Epic | 目标 | 必须退出条件 |
|---|---|---|
| Epic 0 | 工程、身份、租户、契约、DB、Temporal 骨架 | Schema、迁移、租户隔离和最小 Workflow 通过 |
| Epic 1 | Prompt、Model Gateway、Agent Draft、AgentScope 最小运行 | 草稿、模型流、工具和错误归一化通过 |
| Epic 2 | Snapshot、Bundle、Release、Deployment | 发布、部署、失败保护和回滚通过 |
| Epic 3 | Session、Message、Run、Temporal、Outbox | 创建、取消、重试、Workflow 和 fencing 通过 |
| Epic 4 | RunEvent、Event Store、SSE、AG-UI | 并发序号、回放、重连、终态冲突和前端归并通过 |
| Epic 5 | Sandbox、Workspace、Artifact | Run Sandbox 隔离、生命周期和安全导出通过 |
| Epic 6 | Skill、MCP、Policy、Approval、Audit | 供应链、权限求交集、一次性票据和审计通过 |
| Epic 7 | 恢复、对账、配额、容量与安全加固 | 故障注入、SLO、Runbook 和恢复演练通过 |
| Epic 8 | Codex ACP | STDIO、独立 CODEX_HOME、取消和恢复判断通过 |
| Epic 9 | V1 增强 | Session Sandbox、知识库、Schedule、评测和 A2A Client 分项验收 |

每个 Epic 必须交付至少一个可以从 UI/API 触发并完成全链路验收的纵向场景。
