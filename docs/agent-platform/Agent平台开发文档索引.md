# Agent 平台开发文档索引

> 文档版本：V1.5  
> 文档状态：开发输入基线

## 1. 文档集合与阅读顺序

1. [agent-platform-baseline.yaml](./agent-platform-baseline.yaml)：本次开发使用的唯一文档和机器契约版本组合。
2. [Agent平台AI Coding开发总纲](./Agent平台AI%20Coding开发总纲.md)：AI Coding 工程规则、任务包、代码边界和开发顺序。
3. [Agent平台开发执行计划](./Agent平台开发执行计划.md)：需求收敛、Epic 0～9、目录边界、准入门禁和人工确认点。
4. [Agent平台工程技术与配置基线](./Agent平台工程技术与配置基线.md)：Python 后端、Vue 3 前端、进程边界、依赖方向、配置和部署默认结论。
5. [Agent平台需求规格说明书](./Agent平台需求规格说明书.md)：产品目标、MVP/V1 范围、角色、页面、功能需求和冻结决策。
6. [Agent平台架构与流程设计](./Agent平台架构与流程设计.md)：模块边界、运行流程、发布、Sandbox、Temporal、部署和技术风险。
7. [Agent平台核心接口与事件契约](./Agent平台核心接口与事件契约.md)：API、错误码、幂等、RunSpec、RunEvent、SSE、Runtime 和 Artifact 契约。
8. [Agent平台领域模型与状态机](./Agent平台领域模型与状态机.md)：实体、约束、状态机、事务、对账、索引和保留策略。
9. [Agent平台数据库详细设计](./Agent平台数据库详细设计.md)：字段类型、Null、外键、唯一约束、索引、分区和 Alembic 规则。
10. [Agent平台Temporal工作流与活动契约](./Agent平台Temporal工作流与活动契约.md)：Workflow、Activity、Signal、Query、Retry 和版本升级。
11. [Agent平台Sandbox与Workspace服务契约](./Agent平台Sandbox与Workspace服务契约.md)：Sandbox 内部 API、Workspace URI、安全和对账。
12. [Agent平台权限安全与审计契约](./Agent平台权限安全与审计契约.md)：身份、租户、RBAC、Policy、审批、Secret 和审计。
13. [Agent平台前端页面与交互契约](./Agent平台前端页面与交互契约.md)：Vue 路由、字段、页面状态、API 和 SSE 状态归并器。
14. [Agent平台非功能与验收基线](./Agent平台非功能与验收基线.md)：SLO、容量、安全、灾备、测试场景和上线阻断项。
15. [Agent平台测试验收与AI任务包](./Agent平台测试验收与AI任务包.md)：Golden Files、契约、E2E、安全和任务包模板。
16. [Agent平台需求追踪矩阵](./Agent平台需求追踪矩阵.md)：需求编号、接口、实体和验收场景映射。
17. [agent-platform-openapi-v1.yaml](./agent-platform-openapi-v1.yaml)：Agent、发布、Run、Event、Approval 和 Artifact OpenAPI。
18. [agent-platform-openapi-resources-v1.yaml](./agent-platform-openapi-resources-v1.yaml)：资源与管理面 OpenAPI。
19. [`schemas/`](./schemas/)：RunSpec、RunEvent、Skill Manifest、Bundle Manifest 和 Sandbox Policy JSON Schema。
20. [`examples/`](./examples/)：与 Schema 对齐的 RunSpec、RunEvent、Skill 和 Bundle Golden 示例。
21. [ADR-001：前端框架采用 Vue 3](./ADR-001-前端框架采用Vue3.md)：冻结 Vue 3 工程栈、状态所有权、测试和迁移边界。
22. [ADR-002：前端 UI 组件库采用 Element Plus](./ADR-002-前端UI组件库采用ElementPlus.md)：冻结组件库、封装边界和设计 token 约束。

### 1.1 文档角色和 AI 加载规则

- 需求/流程文档：需求规格、架构与流程、非功能与验收、需求追踪、本索引。主要用于人工评审，不作为每个 AI 任务的全量默认上下文。
- AI Coding 全局输入：基线 YAML、AI Coding 总纲、开发执行计划、工程基线。
- AI Coding 按任务输入：核心接口、领域模型、数据库、Temporal、Sandbox、安全、前端和测试契约，以及相关 OpenAPI/Schema/Example。
- 所有前端任务额外读取 `ADR-001-前端框架采用Vue3.md` 和 `ADR-002-前端UI组件库采用ElementPlus.md`，不得使用 R1 的 React 历史选型作为实现输入。
- 每个任务使用“4 个全局文档 + 2～5 个任务契约”，禁止无差别加载全部文档。

## 2. 冲突处理优先级

发生描述冲突时按以下顺序处理：

1. 已评审 ADR 和安全政策。
2. 基线清单中机器可读 Schema、核心接口与事件契约、领域模型与状态机。
3. 需求规格中的固定决策和全局业务规则。
4. 架构流程图和模块说明。
5. 页面文案、示例和参考实现。

任何冲突不得由开发人员自行选择实现；必须更新对应文档和变更记录后再编码。

## 3. 需求变更规则

- MVP 范围、公共 API、状态机、RunEvent、Snapshot、租户隔离和安全基线的变更必须经过产品、架构、开发、测试和安全评审。
- 新增不兼容接口或事件必须提升主版本，并提供迁移和兼容期。
- 已进入迭代的需求变更必须说明影响的 API、表、Workflow、Runtime、前端、测试和上线计划。
- 文档修改和代码变更放在同一个需求或变更单中追踪。
- 临时技术 Spike 不得形成未记录的生产契约。
- 不再新建“补充设计”、“二次完善”、“最终版”或按 Epic 拆分的平行契约文件；新能力必须修改现有权威文档和两个 OpenAPI。
- 架构文档不重复定义代码风格、完整状态机、数据库字段和 API DTO；这些内容分别以工程基线、领域模型、数据库设计和机器契约为准。

## 4. Definition of Ready

一个开发任务进入开发前至少满足：

- 有明确 FR/AC 编号、范围和非目标。
- 有交互或调用流程。
- 有 API/事件 Schema 和错误码。
- 有实体、状态转换、幂等和事务说明。
- 有权限、租户、Secret、Sandbox 和审计要求。
- 有性能限制、资源上限和可观测指标。
- 有正常、异常、取消、重试和恢复验收场景。
- 有依赖项、负责人和可测试环境。

## 5. Definition of Done

- 实现与评审后的契约一致。
- 数据库迁移和回滚/前向修复方案通过验证。
- 单元、契约、集成、安全和必要容量测试通过。
- OpenAPI、Schema、状态机和运维文档同步更新。
- 指标、日志、Trace、告警和 Runbook 可用。
- 不包含明文 Secret、越权路径和无上限资源使用。
- 已知风险有负责人、等级、缓解措施和截止时间。

## 6. 仍需按部署环境记录的 ADR

[Agent平台工程技术与配置基线](./Agent平台工程技术与配置基线.md)已经给出 V1 默认实现。只有实际部署环境或供应商选择不同时才需要 ADR，且不得降低契约和安全要求：

| ADR | 最晚时间 | 责任角色 |
|---|---|---|
| 具体 OIDC Issuer、Claim 映射和服务身份设施 | 部署登录前 | 安全/平台架构师 |
| 生产 Secret Backend 选择 Vault 或具体云 KMS | Runtime 联调前 | 安全/运维 |
| 生产集群不支持 gVisor 时的等价隔离方案 | Sandbox 开发前 | 安全/运维 |
| OpenAI/Qwen/DeepSeek 的具体启用模型和正式费用表 | 模型接入前 | AI 平台架构师 |
| AgentScope 2.0.x 精确 patch/镜像 Digest、Codex 可执行文件和 ACP 精确版本 | Runtime Spike 完成时 | Runtime 负责人 |
| 法律要求覆盖默认保留期限的地区/业务策略 | 上线前 | 合规/数据 Owner |
| 压测后覆盖默认 Event 分区、队列和资源阈值 | 容量验收后 | 数据架构师/运维 |

## 7. 推荐开发任务拆分

```text
Epic 0：工程骨架、身份、租户、数据库、OpenAPI、可观测性
Epic 1：Prompt、Model Gateway、Agent Draft
Epic 2：Snapshot、Bundle、Release、Deployment
Epic 3：Session、Message、Run、Temporal、Outbox
Epic 4：RunEvent、SSE、AG-UI Adapter
Epic 5：Sandbox、Workspace、Artifact
Epic 6：Skill、MCP、Policy、Approval、Audit
Epic 7：恢复、对账、配额、容量和安全加固
Epic 8：Codex ACP
Epic 9：Session Sandbox、知识库、评测、Schedule、A2A Client
```

每个 Epic 必须以纵向可验收场景结束，不能只交付数据库表或管理页面。

## 8. 当前机器可读契约

| 契约 | 文件 | 使用方式 |
|---|---|---|
| 核心 API | `agent-platform-openapi-v1.yaml` | 生成 Python/TypeScript DTO 和 API Client |
| 资源管理 API | `agent-platform-openapi-resources-v1.yaml` | 生成控制面 Client 和管理页面类型 |
| RunSpec | `schemas/run-spec-v1.schema.json` | 生成/校验 Runtime 输入 |
| RunEvent | `schemas/run-event-v1.schema.json` | 生成事件 Payload、SSE 和 AG-UI Adapter 输入 |
| Skill Manifest | `schemas/skill-manifest-v1.schema.json` | Skill 导入、发布和扫描校验 |
| Bundle Manifest | `schemas/bundle-manifest-v1.schema.json` | Bundle 编译、签名和部署校验 |
| Sandbox Policy | `schemas/sandbox-policy-v1.schema.json` | Sandbox Profile 和有效策略校验 |
| Resource Content | `schemas/resource-content-v1.schema.json` | Prompt、Skill、MCP、Model 和 Runtime Target 内容校验 |
| Model Gateway | `schemas/model-gateway-v1.schema.json` | 模型请求、统一响应、用量和流事件校验 |

机器可读契约与自然语言示例冲突时不得继续编码，必须先更新基线和变更记录。

当前冻结版本：核心 OpenAPI 1.2.0，资源管理 OpenAPI 1.1.0。准确文件版本和 SHA-256 以 `agent-platform-baseline.yaml` 为准。

## 9. 版本变更摘要

### Baseline 2026-08

- 明确平台为 Python 原生通用 Agent 平台，不迁移、不重构任何既有平台。
- 固定 RuntimeAdapter 输出 RuntimeEventCandidate，Event Service 生成 RunEvent。
- 固定金额使用 Decimal Money，Session Context 使用不可变引用和 Hash。
- Codex V1 仅实现 ACP STDIO。
- MVP 从首个 Run/发布实现开始使用 Temporal，不建立旁路任务系统。
- 新增数据库、Temporal、Sandbox、权限、前端、测试和 AI Coding 契约。
- 新增 JSON Schema、资源 OpenAPI 和基线版本清单。

### Frozen Baseline 2026-08-R1（已被 R2 取代）

- 基线清单纳入资源管理 OpenAPI 和全部文件 SHA-256。
- 修复资源响应 Schema 组合问题，Tenant/Member/Role 使用独立 Schema。
- 补齐 Session 生命周期、资源管理、连通性测试和异步 Operation 查询契约。
- 固定 Black 88、React + Vite、MVP Run Sandbox 和 RuntimeEventCandidate 单写入路径。

### Frozen Baseline 2026-08-R2（已被 R3 取代）

- 前端固定调整为 Vue 3 Composition API + TypeScript + Vite。
- 路由使用 Vue Router 4；Pinia 管理客户端状态，`@tanstack/vue-query` 管理服务端状态。
- API Client 继续从 OpenAPI 生成，RunEvent/AG-UI/SSE 和全部后端契约保持不变。
- 新增 ADR-001，补齐 Vue 前端 AI Coding、测试、状态隔离和生产构建约束。
- 重新冻结文档版本和完整文件 SHA-256；R1 的 React 记录只保留为历史，不再作为开发输入。

### Frozen Baseline 2026-08-R3（已被 R4 取代）

- 新增开发执行计划并固定采用 Epic 0～9，独立保留 RunEvent/SSE Epic 4。
- AI Coding 总纲和需求追踪矩阵统一到 Epic 0～9，不再使用 E0～E8/E0～E7 平行编号。
- Python 后端业务代码统一位于 `backend/`，Vue 前端业务代码统一位于 `frontend/`；根目录不再建立平行 `apps/`、`packages/`。
- 更新任务包路径、文档版本、基线清单和完整文件 SHA-256；API、事件、状态机和 Vue 技术选型未变化。

### Frozen Baseline 2026-08-R4

- 前端 UI 组件库固定为 Element Plus，并新增 ADR-002 与统一封装/design token 约束。
- local/test OIDC 固定使用服务端 Mock Claim；staging/production 禁止 Mock，真实 Issuer/Claim 待部署确认。
- Model Gateway 固定支持 OpenAI、Qwen、DeepSeek Adapter，具体启用模型和版本化费用表由 Epic 1 配置。
- AgentScope 固定为 2.0.x，精确 patch 由 `uv.lock` 和运行镜像 Digest 固定。
- API、RunEvent、状态机、Epic 0～9 和 `backend/`、`frontend/` 目录边界保持不变。
