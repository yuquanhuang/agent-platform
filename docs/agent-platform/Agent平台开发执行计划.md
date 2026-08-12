# Agent 平台开发执行计划

> 文档版本：V1.9
> 文档状态：开发输入计划
> 适用范围：当前 `docs/agent-platform` 需求、架构、契约和测试文档

## 1. 目标与边界

从零建设一个 Python 原生、支持多租户和私有化部署的通用 Agent 平台，不迁移或重构既有平台。首期以 AgentScope 为主要 Runtime，Codex V1 通过 ACP STDIO 接入；所有 Run 和发布从首次生产实现起使用 Temporal。

业务代码目录固定为：

- Python 后端代码、进程入口、领域包、迁移和后端测试：`backend/`
- Vue 3 前端代码、页面、生成 Client 和前端测试：`frontend/`
- OpenAPI、JSON Schema、Golden 示例和设计文档：`docs/agent-platform/`

不得在根目录建立平行的 `apps/`、`packages/` 业务代码目录。

## 2. 需求分析结论

### MVP 必须闭环

1. 身份、租户、角色、RBAC 和审计。
2. Model Provider/Config、Prompt、Skill、MCP 等资源定义与版本管理。
3. Agent 草稿、Snapshot、Bundle、Release、Deployment 和回滚。
4. Session、Message、Run、取消、重试和历史查询。
5. Temporal `AgentRunWorkflow`、Activity、Signal、Query、Outbox 和对账基础。
6. AgentScope Runtime、Run Sandbox、Workspace、Artifact。
7. Event Service 统一生成和持久化强类型 `RunEvent`，提供 SSE 和 AG-UI 出口。
8. Policy、Approval、Secret Reference、配额、限流、可观测性和基础恢复。

MVP 退出条件：AgentScope Agent 完成创建、发布、运行、工具调用、文件产出、取消、断线重连、事件回放和失败恢复，并通过租户隔离、安全和基线容量测试。

### V1 增量能力

Codex ACP STDIO、Session Sandbox、知识库、基础评测与反馈、Temporal Schedule、A2A Client 和更完整的容量/运维能力。未进入当前 Epic 的页面、接口和字段不得以占位实现进入生产代码。

### 不变的核心原则

- Runtime 只读取不可变 Snapshot/Bundle，不直接读取草稿。
- RuntimeAdapter 只能输出 `RuntimeEventCandidate`；RunEvent 由 Event Service 校验、分配序号和持久化。
- Temporal History 只保存编排状态，不逐条写入 Token、文本 Delta 和普通进度事件。
- 租户、权限、幂等、ETag、fencing token、Secret 引用、Sandbox 隔离和资源上限从第一阶段进入设计与测试。
- 前端固定采用 Vue 3 Composition API、TypeScript、Vite、Vue Router 4、Pinia、`@tanstack/vue-query`；API Client 和 RunEvent 类型从契约生成。

## 3. 执行顺序（Epic 0～9）

每个 Epic 都按“契约确认 → 后端实现 → 前端接入 → 集成/安全测试 → 纵向验收 → 更新追踪矩阵”执行。Epic 只有在退出条件满足后才能进入下一阶段。

| Epic | 范围 | 主要交付 | 退出条件 |
|---|---|---|---|
| Epic 0 | 工程底座、身份、租户、契约、数据库、可观测性 | `backend/` 模块骨架、`frontend/` Vue 骨架、生成链路、迁移、健康检查、CI 门禁 | Schema/Golden、迁移、租户隔离、最小 Temporal Workflow 和 `make check` 通过 |
| Epic 1 | Prompt、Model Gateway、Agent Draft | 资源 CRUD/版本、模型连通性、预算/限流、Agent 草稿页面和 API | ETag/幂等/权限测试通过；可创建可校验的 Agent Draft |
| Epic 2 | Snapshot、Bundle、Release、Deployment | 发布编译、不可变快照、部署状态、Diff、回滚 | 发布失败不影响旧 Deployment；发布/回滚 E2E 通过 |
| Epic 3 | Session、Message、Run、Temporal、Outbox | Run 创建、取消、重试、Workflow/Activity、状态事实和对账入口 | Run 可由 UI/API 触发并由 Temporal 完成；重试不重复副作用 |
| Epic 4 | RunEvent、Event Store、SSE、AG-UI | 批量写入、并发序号、终态保护、重连恢复、Vue 状态归并器 | 重复/乱序/缺口/断线恢复、终态冲突和回放测试通过 |
| Epic 5 | Sandbox、Workspace、Artifact | Run Sandbox、工作区 URI、上传/下载、扫描、清理和权限 | 越权、路径穿越、SSRF、资源耗尽和生命周期测试通过 |
| Epic 6 | Skill、MCP、Policy、Approval、Audit | Manifest 校验、供应链扫描、策略求交集、一次性执行票据、审计查询 | 高风险工具审批、拒绝/过期/自审批和审计 E2E 通过 |
| Epic 7 | 恢复、对账、配额、容量和安全加固 | Worker/依赖恢复、Reconciliation、背压、SLO、告警、Runbook、生产 Sandbox | 故障注入、容量基线、安全阻断项和恢复演练通过 |
| Epic 8 | Codex ACP | Codex Bundle、ACP STDIO、独立运行池、Session 映射、取消/恢复 | Codex Contract/E2E 通过；Session、Secret、文件和租户隔离无泄漏 |
| Epic 9 | V1 增量 | Session Sandbox、知识库、评测、Schedule、A2A Client | 每项能力独立 Feature Flag、契约、迁移和 E2E 验收；不影响 MVP |

## 4. 每个任务的准入与完成

### 开发准入（Definition of Ready）

- 明确 FR/AC、范围、非目标、依赖 Epic 和允许修改目录。
- 确认 OpenAPI/JSON Schema、实体、状态转换、幂等、重试、取消和补偿语义。
- 明确租户/权限/Secret/Sandbox/审计影响、资源上限、指标和恢复路径。
- 绑定正常、失败、权限/租户、并发或恢复测试场景。
- 记录任务使用的文档版本和基线标识；发现冲突先停工并登记。

### 完成定义（Definition of Done）

- 契约、生成代码、后端和前端实现保持单一来源且无生成 Diff。
- 迁移可前向执行并有回滚/修复方案；无明文 Secret、越权路径或无界资源。
- 后端执行 `make backend-check`，前端执行 `make frontend-check`，共享契约执行 `make contract-check`。
- 全仓至少执行 `make check`；发布前或高风险变更执行 `make check-all`。
- 单元、契约、集成、安全、必要容量/E2E 测试通过，并更新需求追踪矩阵、运行手册和变更摘要。

## 5. 目录与实现约束

建议结构：

```text
backend/
  apps/                 # API、Temporal、Runtime、Event、Sandbox 进程
  packages/             # contracts、domain、application、infrastructure、runtimes、security
  migrations/
  tests/
frontend/
  src/                  # Vue 3 应用、路由、Store、composable、生成 Client
  tests/
```

共享机器契约继续以 `docs/agent-platform/agent-platform-openapi*.yaml` 和 `schemas/` 为来源；禁止前后端维护含义重复的手写 DTO 或事件字段。

## 6. 卡点处理结果与人工确认项

1. **目录冲突已处理**：AI Coding 总纲、工程基线、架构及相关契约路径统一为 `backend/`、`frontend/`。
2. **Epic 编号冲突已处理**：总纲、索引、执行计划和追踪矩阵统一使用 Epic 0～9，独立保留 RunEvent/SSE Epic 4。
3. **前端未初始化不是前置阻塞**：Vue 脚手架、包管理器、生成 Client 和前端门禁属于 Epic 0 的正式交付。
4. **后端最小骨架不是前置阻塞**：模块边界、配置、迁移、测试和进程入口属于 Epic 0 的正式交付。
5. **计划基线已处理**：本计划已纳入文档索引和 R6 基线，相关版本、SHA-256 和变更摘要同步更新。
6. **Python 门禁环境已处理**：根 Makefile 使用 `uv run python`，避免系统 Python 与项目虚拟环境依赖不一致；Black 固定 `py312`、Pyright 启用 strict，并增加最小健康测试。`make backend-check` 已通过。前端门禁仍因尚无 `package.json` 退出，作为 Epic 0 前端骨架交付处理，不通过关闭门禁绕过。
7. **契约门禁待 Epic 0 落地**：当前 `make contract-check` 尚未配置实际命令，项目环境也缺少 YAML、JSON Schema 和 OpenAPI 校验依赖，因此“跳过”不视为通过。Epic 0 的首个任务必须补齐离线可复现的契约校验和生成 Diff，再开始业务功能。

已确认和仍待确认的实施选择：

| 确认项 | 建议 | 最晚确认时间 |
|---|---|---|
| Vue UI 组件库 | **已确认 Element Plus**；通过统一封装层和 design token 使用 | Epic 0 前端脚手架 |
| OIDC Issuer/Claim 映射 | **开发/测试已确认使用 Mock**；生产仍需提供真实 Issuer、audience 和 Claim 映射 | 生产身份联调前 |
| 首批模型供应商与费用表 | **能力已确认支持 OpenAI、Qwen、DeepSeek**；具体启用模型和费用表待定 | Epic 1 Model Gateway |
| AgentScope 精确版本 | **已确认 2.0.x**；精确 patch 和镜像 Digest 由 Spike、`uv.lock` 固定 | Epic 1 Runtime |
| 生产 Secret 与 Sandbox | Secret 选择 Vault/云 KMS；默认 gVisor，不支持时以 ADR 选择 Kata 等价方案 | Epic 5 前 |
| Codex/ACP 精确版本 | 固定 Codex 可执行文件、ACP 版本和 Digest | Epic 8 前 |

未确认前只实现稳定接口、默认本地适配或隔离 Spike，不形成对应生产决策。

## 7. 开发启动条件

R5 文档完整性通过后可开始 Epic 0 工程任务；Epic 0 必须先完成契约门禁和前后端脚手架，门禁通过前不进入业务功能。第 6 节剩余人工确认项按对应 Epic 的最晚时间完成，不阻塞与其无关的工程骨架工作。后续严格按 Epic 顺序推进，不跨 Epic 建设未验收的生产旁路。

## 8. AI Coding 开发步骤

Epic 是阶段里程碑，不作为一次 AI Coding 的任务粒度。单个任务使用 `AP-E<epic>-NNN` 编号，只交付一个明确行为、一个紧密契约组或一个迁移组。

执行节奏：

1. 每个任务先填写 AI Coding 任务包，确认范围、非目标、允许目录和验收命令。
2. 每个任务独立实现、测试、审查和合并；不把整个 Epic 交给一次生成。
3. 每完成 2～4 个强相关任务执行一次集成门禁；每个 Epic 最后单独安排纵向 E2E 收口任务。
4. 契约、迁移、后端、前端和 E2E 分任务实施；存在生产者/消费者关系时先完成契约和生产者。
5. Spike 可以提前，但只能位于隔离测试路径，不得形成绕过 Temporal、Event Service、Policy 或 Sandbox 的生产旁路。

### Epic 0：工程准入与基础骨架

1. `AP-E0-001`：实现基线 SHA、OpenAPI、JSON Schema 和 Golden 示例校验，接通 `make contract-check`。
2. `AP-E0-002`：建立 `backend/apps`、`backend/packages`、配置、健康检查和进程入口骨架。
3. `AP-E0-003`：初始化 Vue 3、Vite、Element Plus、Router、Pinia、Vue Query 和前端门禁。
4. `AP-E0-004`：建立 Python/TypeScript Client、DTO、RunEvent 类型生成与 Diff 门禁。
5. `AP-E0-005`：建立 PostgreSQL、SQLAlchemy、Alembic、TenantContext 和基础 RLS/索引。
6. `AP-E0-006`：实现 local/test Mock OIDC、`/me`、Tenant/Member/Role 和基础 RBAC。
7. `AP-E0-007`：实现最小 Temporal Worker/Workflow、Outbox、结构化日志、Trace 和指标骨架。
8. `AP-E0-008`：完成 Epic 0 集成验收，要求契约、后端、前端和最小 Workflow 门禁通过。

`AP-E0-002` 与 `AP-E0-003` 可在 `AP-E0-001` 完成后并行；`AP-E0-005` 依赖后端和契约生成，`AP-E0-006` 依赖数据库租户基础。

### Epic 1：资源、Model Gateway 与 Agent Draft

1. `AP-E1-001`：资源定义/版本、ETag、幂等和引用查询公共能力。
2. `AP-E1-002`：Prompt CRUD、版本、发布、回滚和前端页面。
3. `AP-E1-003`：ModelProvider/ModelConfig、Secret Reference 和连通性异步操作。
4. `AP-E1-004`：Model Gateway 请求、流式响应、错误和用量归一化。
5. `AP-E1-005`：OpenAI、Qwen、DeepSeek Adapter 及供应商契约测试。
6. `AP-E1-006`：预算、限流、Token/费用统计和受控 fallback。
7. `AP-E1-007`：Agent Draft API、ResourceBinding 规范化/多模型路由校验、复制、停用和引用约束。
8. `AP-E1-008`：Agent Draft Vue 编辑器和 Epic 1 纵向验收。
9. `AP-E1-009`：AgentScope 2.0.x 兼容 Spike，固定精确 patch 和镜像 Digest；不形成生产旁路。

### Epic 2：发布与部署

1. `AP-E2-001`：Snapshot 编译与不可变引用。
2. `AP-E2-002`：AgentScope Bundle 和 Bundle Manifest 编译/校验。
3. `AP-E2-003`：Release Workflow、异步 Operation 和发布失败保护。
4. `AP-E2-004`：Deployment 激活、历史状态和并发保护。
5. `AP-E2-005`：更新 R7 契约，实现无副作用发布预览、脱敏 Snapshot Diff、版本历史和 Vue 发布页面；正式发布仍事务内重编译和 CAS。
6. `AP-E2-006`：回滚产生新 Release，并完成发布/回滚 E2E。

### Epic 3：Session、Run 与 Temporal

1. `AP-E3-001`：Session CRUD、归档、删除约束和分页。
2. `AP-E3-002`：Message 历史、分支顺序和权限。
3. `AP-E3-003`：Run/RunAttempt 创建、幂等、状态机和 Snapshot 绑定。
4. `AP-E3-004`：AgentRunWorkflow、Activity、Signal 和 Query。
5. `AP-E3-005`：取消、重试、新 Run 语义和 fencing token。
6. `AP-E3-006`：Outbox dispatcher、Workflow 映射和对账入口。
7. `AP-E3-007`：使用测试 Fake 验证 RuntimeEventCandidate Port，通过 `getRun` 轮询完成 Epic 3 验收；不实现 Event Store/SSE。

### Epic 4：RunEvent、SSE 与 AG-UI

1. `AP-E4-001`：RunEvent 表、计数器、约束和迁移。
2. `AP-E4-002`：Candidate 校验、批量写入、幂等序号和终态保护。
3. `AP-E4-003`：事件查询、分页、回放和序号缺口语义。
4. `AP-E4-004`：SSE Cursor、Last-Event-ID、重连和背压。
5. `AP-E4-005`：RunEvent 到 AG-UI 的出口映射。
6. `AP-E4-006`：Vue 独立 Reducer，覆盖重复、乱序、缺口和终态。
7. `AP-E4-007`：完成断线恢复、回放和终态冲突 E2E。

### Epic 5：Sandbox、Workspace 与 Artifact

1. `AP-E5-001`：SandboxPolicy、内部 API 和 Provider Port。
2. `AP-E5-002`：SandboxInstance、Lease、provision/destroy 生命周期。
3. `AP-E5-003`：Workspace URI、路径隔离和容量限制。
4. `AP-E5-004`：Artifact 上传、完成和扫描流程。
5. `AP-E5-005`：Artifact 下载、权限、过期和删除。
6. `AP-E5-006`：完成隔离、SSRF、路径穿越、清理和 Artifact E2E。

### Epic 6：能力治理、审批与审计

1. `AP-E6-001`：Skill Manifest、导入、版本和供应链扫描。
2. `AP-E6-002`：MCP 配置、Discover、能力冻结和安全校验。
3. `AP-E6-003`：Policy 有效策略求交集和 Admission Controller。
4. `AP-E6-004`：ApprovalRequest/Decision、过期和自审批阻断。
5. `AP-E6-005`：一次性 Execution Ticket 和 Tool Gateway 消费。
6. `AP-E6-006`：Audit 写入、查询、保留和敏感字段脱敏。
7. `AP-E6-007`：完成高风险工具审批和审计 E2E。

### Epic 7：生产可靠性与安全加固

1. `AP-E7-001`：Reconciliation 规则和状态修复。
2. `AP-E7-002`：API、Worker、Temporal、Redis、S3 故障恢复。
3. `AP-E7-003`：配额、预算、限流和背压。
   - 子阶段 A：配置驱动 Run 并发硬限制和 PostgreSQL 原子准入。
   - 子阶段 B：durable QuotaPolicy 管理 API、不可变版本、RLS/RBAC/Audit，并接入 Run 创建/重试；已完成。
   - 子阶段 C：周期 BudgetPolicy、Token/费用聚合与可信费用表。
   - 子阶段 D：有界队列、存储配额、Event/SSE 背压和容量指标。
4. `AP-E7-004`：SLO、指标、告警和 Trace 关联。
5. `AP-E7-005`：容量、耐久和资源池隔离测试。
6. `AP-E7-006`：安全阻断项、供应链和生产 Sandbox 验收。
7. `AP-E7-007`：Runbook、灾备和恢复演练，完成生产准入。

### Epic 8：Codex ACP

1. `AP-E8-001`：Codex/ACP 精确版本和兼容 Spike。
2. `AP-E8-002`：Codex Bundle 与 Runtime Target。
3. `AP-E8-003`：ACP STDIO RuntimeAdapter。
4. `AP-E8-004`：独立 CODEX_HOME、Worker Pool 和 Sandbox 进程管理。
5. `AP-E8-005`：Codex Session 映射和隔离。
6. `AP-E8-006`：取消、超时和恢复安全判断。
7. `AP-E8-007`：完成 Codex 契约、安全和 E2E 验收。

### Epic 9：V1 增量能力

1. `AP-E9-001`：Session Sandbox 独立任务链。
2. `AP-E9-002`：知识库导入、解析、分块和索引任务链。
3. `AP-E9-003`：知识检索、ACL、删除和失效任务链。
4. `AP-E9-004`：EvaluationSet、EvaluationRun 和版本冻结任务链。
5. `AP-E9-005`：用户反馈、标签、评论和权限任务链。
6. `AP-E9-006`：Temporal Schedule、DST、misfire 和并发策略任务链。
7. `AP-E9-007`：A2A Client、认证、SSRF 防护和事件映射任务链。

Epic 9 每项能力必须使用独立 Feature Flag，并继续拆成“契约/迁移 → 后端 → 前端 → E2E”，不得把整项能力一次性生成。

## 9. 首个 AI Coding 任务

首个任务固定为 `AP-E0-001`：契约完整性与校验门禁。

```yaml
task_id: AP-E0-001
title: 实现契约完整性与校验门禁
baseline: agent-platform-v1-dev-baseline-2026-08-r11
scope:
  includes:
    - scripts/
    - harness/project.json
    - pyproject.toml
    - uv.lock
  excludes:
    - backend 业务模块
    - frontend 业务模块
    - OpenAPI/JSON Schema 业务语义修改
acceptance:
  - make contract-check 实际执行且不再显示 skip
  - 校验基线 SHA-256、OpenAPI 3.1、JSON Schema Draft 2020-12
  - examples 全部通过对应 Schema
  - 破坏任一 Hash、Schema 或 Example 时命令返回非零
  - 不自动修改或重新格式化业务契约
verification:
  - make contract-check
  - make backend-check
```

开始该任务前，项目环境需提供固定版本的 YAML、JSON Schema 和 OpenAPI 校验依赖；依赖由项目 lockfile 管理，不在 CI 运行时临时下载。
