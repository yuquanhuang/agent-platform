# Agent 平台需求追踪矩阵

> 文档版本：V1.7
> 文档状态：开发输入基线

## 1. 使用方式

本矩阵用于把需求、接口、领域实体和验收场景关联起来。进入迭代后，团队应在任务系统中继续补充设计、代码、测试用例和发布记录链接。

表中 `FR-XXX-001～006` 范围只用于 Epic 规划，不能作为单个 FR 已完成的证据。每个 AI Coding 任务必须在任务系统中为每个 FR 分别绑定 operationId/Schema、数据迁移、正常用例、失败用例和权限/租户用例；任一缺失则该 FR 不得标记完成。

## 2. MVP 追踪矩阵

| 能力 | 需求编号 | 核心接口/契约 | 核心实体 | 主要验收 |
|---|---|---|---|---|
| 身份与租户 | FR-IAM-001～006 | Auth、Tenant Scope、Service Identity | Tenant、User、RoleBinding | AC-005、权限矩阵 |
| Agent 生命周期 | FR-AGT-001～006 | `/agents`、publish、rollback | AgentDefinition、Version、Snapshot、Release、Deployment | AC-001、AC-007 |
| Runtime | FR-RT-001～006 | RuntimeAdapter、Capabilities | RuntimeTarget、RuntimeSession | Contract Test、Codex V1 |
| Session 与 Run | FR-RUN-001～006 | `/sessions`、`/runs`、cancel、retry | Session、Message、Run、RunAttempt | AC-002、AC-003 |
| Event 与 SSE | FR-RUN-003～004、FR-CON-005 | RunEvent、batch ingest、SSE | RunEvent、Outbox | AC-002、Event Store 验收 |
| Sandbox | FR-SBX-001～007 | Sandbox Policy/API | SandboxInstance、Lease、Workspace | AC-002、AC-005、安全测试 |
| Temporal | FR-TMP-001～007 | Workflow、Signal、Query | Run/Release Workflow Mapping | AC-003、AC-006、Replay Test |
| 模型 | FR-MDL-001～007 | Model Gateway、OpenAI/Qwen/DeepSeek Adapter | ModelConfig、ModelUsage、Budget | AC-001、容量、费用与三供应商契约测试 |
| Prompt/Skill/MCP | FR-RES-001～007 | Resource API、Manifest | Resource Definition/Version | AC-001、供应链测试 |
| 审批 | FR-APR-001～006 | Approval API、Execution Ticket | ApprovalRequest、Decision、Ticket | AC-004 |
| 文件与产物 | FR-DAT-001～006 | Artifact API | Workspace、Artifact | AC-002、AC-005 |
| 幂等与并发 | FR-CON-001～006 | Idempotency、ETag、Fencing | IdempotencyRecord、RunAttempt | AC-001、AC-003 |
| 审计 | FR-AUD-001～005 | Audit Query | AuditLog | AC-004、AC-005、AC-007 |

## 3. V1 增量追踪矩阵

| 能力 | 需求编号 | 关键契约/实体 | 验收入口 |
|---|---|---|---|
| 知识库 | FR-KNW-001～005 | KnowledgeDocument/Chunk/Index、Adapter | 文档权限、删除、重建索引 |
| 评测反馈 | FR-EVL-001～005 | EvaluationSet/Run、Feedback | Snapshot 版本对比、Judge 可追溯 |
| 定时任务 | FR-SCH-001～006 | Schedule、Temporal Schedule、Run | DST、misfire、并发策略 |
| Codex ACP | FR-RT-001～006 | ACP Adapter、RuntimeSession、Codex Bundle | Codex V1 验收 |
| Session Sandbox | FR-SBX-002 | Compatibility Hash、Lease | 清理、版本变化重建、跨 Session 隔离 |
| A2A Client | 范围 4.2 | Remote Agent Binding、Client Adapter | 鉴权、超时、事件映射、数据外发审计 |

## 4. 横向非功能追踪

| 非功能领域 | 影响范围 | 必须测试 |
|---|---|---|
| 租户隔离 | 全部 API、DB、Redis、Object、Workflow、Sandbox | 两租户自动化越权矩阵 |
| 幂等 | Run、发布、审批、Artifact、Schedule、工具 | 重复、并发、超时后重试 |
| 兼容性 | API、RunSpec、RunEvent、Bundle、Manifest | 当前与前一生产版本契约测试 |
| 可恢复性 | Temporal、Runtime、Outbox、Event、Sandbox | Worker/依赖重启与对账 |
| 资源限制 | Model、Run、Sandbox、Workspace、Event | 配额、背压、限流和资源耗尽 |
| 安全 | Skill、MCP、Prompt、文件、工具、Secret | SSRF、路径、供应链、Prompt Injection |
| 可观测性 | 所有进程与跨系统调用 | Trace 关联、SLO、告警与 Runbook |

## 5. 需求完成判定

一个 FR 只有在以下内容全部存在时才能标记完成：

- 评审后的接口或事件契约。
- 数据模型、状态和迁移。
- 权限、审计、幂等和失败语义。
- 单元测试和契约测试。
- 至少一个正常及一个异常验收用例。
- 监控指标和必要告警。
- 文档和需求追踪链接。

## 6. AI Coding 核心任务追踪

| Task/Epic | FR | operationId/契约 | 数据实体 | 主要测试 |
|---|---|---|---|---|
| Epic 0 身份租户 | FR-IAM-001～006 | getCurrentIdentity/createTenant/getTenant/updateTenant/disableTenant/createMember/getMember/updateMember/deleteMember/createRole/getRole/updateRole/deleteRole | tenant、app_user、tenant_member、role_binding | 两租户权限矩阵、membership_version 失效 |
| Epic 0 契约代码生成 | FR-RT-001、FR-RUN-003 | RunSpec/RunEvent JSON Schema | 无 | Schema Golden、生成 Diff |
| Epic 1 Model Gateway | FR-MDL-001～007 | Model Gateway 契约、OpenAI/Qwen/DeepSeek Adapter、ModelProvider/ModelConfig CRUD、testModelProviderConnection、publishModelConfig、Agent Model `ResourceBinding` 路由策略 | resource_definition/version、model_binding_snapshot、model_usage、budget | 三供应商契约、限流、预算、受控 fallback、不可变绑定、连通性 |
| Epic 1 Agent Draft | FR-AGT-001、FR-MDL-005～006 | createAgent/listAgents/getAgent/updateAgent/copyAgent/disableAgent/deleteAgent、AgentBindingList | agent_definition、agent_binding | ETag、幂等、引用删除、权限、旧单模型兼容、多模型角色/顺序/错误码负向校验 |
| Epic 2 发布 | FR-AGT-002～006、FR-RES-006～007 | previewAgentPublish/publishAgent/getRelease/rollbackAgent/listAgentVersions/getAgentVersion/diffAgentSnapshots/getDeployment | agent_version、snapshot、bundle、release、deployment | AC-001、AC-007、预览无副作用、Diff 脱敏和 Draft 漂移保护 |
| Epic 3 Session/Run | FR-RUN-001～006、FR-CON-001～006 | listSessions/createSession/getSession/updateSession/archiveSession/deleteSession/listSessionMessages/listSessionRuns/createRun/getRun/cancelRun/retryRun | chat_session、chat_message、agent_run、run_attempt、outbox | AC-002、AC-003、Session 历史/分支/归档 |
| Epic 4 Event Store | FR-RUN-003～004、FR-CON-005 | appendRunEventCandidates/listRunEvents/streamRunEvents | run_event、run_event_counter | 并发序号、重连、终态冲突 |
| Epic 5 Sandbox | FR-SBX-001～007 | Sandbox 内部 API、SandboxPolicy | sandbox_instance、lease、workspace | 隔离、资源耗尽、对账 |
| Epic 5 Artifact | FR-DAT-001～006 | createArtifactUpload/completeArtifactUpload/getArtifact/createArtifactDownload/deleteArtifact | artifact、workspace | 扫描、越权、过期 |
| Epic 6 Skill/MCP | FR-RES-003～005 | Resource API、Skill Manifest、MCP Discover | resource_definition/version | 路径、供应链、Schema Hash |
| Epic 6 Approval | FR-APR-001～006 | listApprovals/getApproval/decideApproval | approval_request/decision、execution_ticket | AC-004、重放、自审批 |
| Epic 7 可靠性 | FR-TMP-001～007 | Temporal 契约、Reconciliation | outbox、inbox、workflow mapping | AC-006、Replay、故障注入 |
| Epic 8 Codex | FR-RT-001～006 | CodexAcpRuntimeAdapter、ACP STDIO | runtime_session、run_attempt | Codex V1 验收 |
| Epic 9 V1 增量 | FR-KNW-001～005、FR-EVL-001～005、FR-SCH-001～006、FR-SBX-002 | Knowledge/Evaluation/Schedule/Session Sandbox/A2A Client 契约 | 对应 V1 增量实体 | E2E-007～010、Session Sandbox 隔离 |

## 6.1 异步操作与资源完整性追踪

| 范围 | 必须契约 | 最低验收 |
|---|---|---|
| 通用异步操作 | getOperation、OperationAccepted、Operation | status_url 可查询、终态稳定、错误可编程判断 |
| Prompt/Skill/MCP/ModelConfig | CRUD、copy、publish、rollback、enable/disable、versions、diff、references | ETag、引用删除、安全扫描、Snapshot 引用不可变 |
| ModelProvider/RuntimeTarget | CRUD、enable/disable、test | Secret 不泄漏、连通性结果可查询、被引用资源不可直接删除 |
| Tenant/Member/Role | 独立 Schema 和 CRUD/停用 | 跨租户阻断、membership_version 失效、权限变更审计 |
| Session | list/get/update/archive/delete/messages/runs | 活动 Run 阻断删除、历史不级联物理删除、分支顺序稳定 |

## 7. 契约到代码制品映射

| 文档/Schema | 目标代码 | 禁止重复定义 |
|---|---|---|
| `agent-platform-openapi-v1.yaml` | FastAPI Request/Response、TypeScript Client | 页面手写 DTO |
| `agent-platform-openapi-resources-v1.yaml` | Resource/Admin API 与 Client | 各资源自行复制分页/错误模型 |
| `run-spec-v1.schema.json` | `backend/packages/contracts/run_spec.py` | Runtime Adapter 自定义 RunSpec |
| `run-event-v1.schema.json` | `backend/packages/contracts/events.py` | Adapter/前端自定义事件字段 |
| `skill-manifest-v1.schema.json` | Skill 校验器和发布扫描 | 仅按 YAML 示例解析 |
| `bundle-manifest-v1.schema.json` | Bundle Compiler/Verifier | Runtime 私有 Bundle 清单 |
| `sandbox-policy-v1.schema.json` | Sandbox Profile/Effective Policy | Sandbox Provider 自定义策略 |
| 数据库详细设计 | SQLAlchemy Model、Alembic、Repository | ORM 默认推断约束 |
| Temporal 契约 | Workflow/Activity/Signal Model | BackgroundTasks/Celery 旁路 |
| 前端交互契约与 ADR-001 | Vue Router、Pinia、`@tanstack/vue-query`、SSE 状态归并器、权限守卫 | 页面本地发明状态机或重复维护服务端实体 |
