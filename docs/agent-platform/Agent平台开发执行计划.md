# Agent 平台开发执行计划

> 文档版本：V1.1
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
5. **计划基线已处理**：本计划已纳入文档索引和 R4 基线，相关版本、SHA-256 和变更摘要同步更新。
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

R4 文档完整性通过后可开始 Epic 0 工程任务；Epic 0 必须先完成契约门禁和前后端脚手架，门禁通过前不进入业务功能。第 6 节剩余人工确认项按对应 Epic 的最晚时间完成，不阻塞与其无关的工程骨架工作。后续严格按 Epic 顺序推进，不跨 Epic 建设未验收的生产旁路。
