# Agent 平台核心接口与事件契约

> 文档版本：V1.4
> 文档状态：开发输入基线  
> 关联需求：[Agent平台需求规格说明书](./Agent平台需求规格说明书.md)  
> 关联架构：[Agent平台架构与流程设计](./Agent平台架构与流程设计.md)

## 1. 目的与适用范围

本文定义控制面、运行面和内部 Runtime 的最低接口契约，作为 OpenAPI、Pydantic Model、RuntimeAdapter Contract Test 和前后端联调的输入。

本文没有展开每个资源的全部展示字段；开发前必须据此生成并评审机器可读 OpenAPI。任何实现不得只依赖本文中的自然语言而省略 Schema 校验。

核心机器可读契约见 [agent-platform-openapi-v1.yaml](./agent-platform-openapi-v1.yaml)，资源和管理面契约见 [agent-platform-openapi-resources-v1.yaml](./agent-platform-openapi-resources-v1.yaml)。知识库、Schedule、评测和 A2A Client 的 V1 最低 Path 和 Component 已冻结在资源管理 OpenAPI 中；对应 Epic 未启用时通过 Feature Flag 隐藏页面和路由，不返回占位成功响应。后续扩展直接修改这两个现有 OpenAPI 并提升版本，不再新建平行 OpenAPI 文件。

## 2. 通用协议约定

### 2.1 URL 与版本

- 外部 API 基础路径为 `/api/v1`。
- 内部 API 基础路径为 `/internal/v1`，只接受服务身份。
- API 版本、RunSpec 版本、RunEvent Schema 版本、Bundle Manifest 版本分别管理。
- ID 是不透明字符串；服务端生成的 ID 应具备全局唯一性，客户端不得解析 ID 业务含义。

### 2.2 编码与时间

- JSON 使用 UTF-8，`Content-Type: application/json`。
- 时间使用 UTC ISO 8601，例如 `2026-08-03T08:15:30.123Z`。
- 金额使用字符串 Decimal 并携带币种，例如 `{"amount": "1.25", "currency": "CNY"}`。
- Hash 明确算法，例如 `sha256:<hex>`。
- 文件大小、Token、序号和计数使用整数，不使用浮点数。

### 2.3 认证与租户

- 浏览器和用户 SDK 使用 OIDC/OAuth2 Access Token。
- 内部服务使用 mTLS、Workload Identity 或等价短期服务凭据。
- `tenant_id` 从认证身份和授权上下文解析；不得直接信任普通客户端提交的租户字段。
- 超级管理员跨租户操作必须使用显式管理接口、原因和审计记录。
- SSE 不在 URL Query 中传递长期 Access Token。
- V1 浏览器固定使用 OIDC Authorization Code + PKCE，前端向 IdP 获取短期 Access Token，API 不代理保存 IdP Refresh Token。
- `GET /api/v1/me` 返回平台用户、当前租户、成员状态、角色和 `membership_version`；前端不得从 Token Claim 自行推导最终权限。
- 退出由前端清理本地 Token 并调用 IdP End Session Endpoint；平台不定义第二套用户密码和 Refresh Token 接口。

### 2.4 通用请求头

| Header | 必需 | 说明 |
|---|---:|---|
| `Authorization` | 外部请求是 | Bearer Token；Cookie 模式按统一认证实现 |
| `X-Request-ID` | 否 | 客户端请求追踪 ID；缺失时服务端生成 |
| `Idempotency-Key` | 创建/副作用请求是 | 同一租户、用户、操作范围内唯一 |
| `If-Match` | 更新资源是 | 强 ETag，固定格式为 `"rv:<resource_version>"`，例如 `"rv:3"` |
| `Last-Event-ID` | SSE 续传否 | 最后确认的 `sequence_no` |

响应至少包含 `X-Request-ID`。创建异步任务时返回业务 ID 和可查询状态地址。

## 3. 幂等与并发控制

### 3.1 幂等规则

必须支持幂等的操作：

- 创建 Run。
- 发布 Agent。
- 创建 Schedule。
- 审批决策。
- Run 取消和重试。
- Artifact 完成上报。
- 危险工具的执行票据消费。

规则：

1. 幂等键作用域至少包含 `tenant_id + actor_id + operation_type`。
2. 服务端保存请求体规范化 Hash、处理状态和响应摘要，默认保留 24 小时。
3. 同一 Key、相同请求返回首次有效结果。
4. 同一 Key、不同请求体返回 HTTP 409 和 `IDEMPOTENCY_KEY_REUSED`。
5. 首次请求仍处理中时返回原 operation/run ID，不并行执行第二次副作用。

### 3.2 乐观并发

- 可编辑资源包含 `resource_version`、`updated_at`，GET/CREATE/UPDATE 响应必须返回 `ETag: "rv:<resource_version>"`。
- 更新必须携带 `If-Match`；不匹配返回 412 `RESOURCE_VERSION_CONFLICT`。
- 发布流程读取 Agent 关系图后再次校验所有资源版本，避免发布期间发生漂移。
- 已发布 Snapshot、Bundle、RunEvent、AuditLog 不支持更新接口。

## 4. 分页、排序和筛选

推荐游标分页：

```json
{
  "items": [],
  "next_cursor": "opaque-or-null",
  "has_more": false
}
```

- 默认 `limit=20`，最大 `limit=200`。
- 排序必须稳定，默认 `created_at desc, id desc`。
- Cursor 是不透明值，客户端不得拼装。
- 页码分页仅用于数据规模明确的小型管理列表。
- 筛选字段、排序字段和最大时间范围必须在接口文档中白名单声明。

## 5. 统一错误响应

```json
{
  "error": {
    "code": "RUN_ALREADY_ACTIVE",
    "message": "The session already has an active run.",
    "request_id": "req_xxx",
    "details": {
      "active_run_id": "run_xxx"
    },
    "retryable": false
  }
}
```

约束：

- `code` 稳定、可用于程序判断；`message` 可本地化，不作为判断依据。
- `details` 只包含安全、结构化信息，不返回堆栈、SQL、密钥或内部地址。
- `retryable=true` 只表示调用方可以按退避策略重试，不保证重试一定成功。
- HTTP 语义与业务错误一致，不使用 HTTP 200 包装失败。

基础错误码：

| HTTP | Code | 场景 |
|---:|---|---|
| 400 | `VALIDATION_ERROR` | 字段、格式、大小或业务前置条件不合法 |
| 401 | `UNAUTHENTICATED` | 未登录或 Token 无效 |
| 403 | `PERMISSION_DENIED` | 无资源或动作权限 |
| 404 | `RESOURCE_NOT_FOUND` | 资源不存在或无权感知其存在 |
| 409 | `RESOURCE_STATE_CONFLICT` | 当前状态不允许操作 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 幂等键被不同请求复用 |
| 412 | `RESOURCE_VERSION_CONFLICT` | ETag/版本冲突 |
| 413 | `PAYLOAD_TOO_LARGE` | 请求、文件或事件超限 |
| 422 | `CONTRACT_VALIDATION_FAILED` | RunSpec、Manifest 或事件 Schema 不合法 |
| 429 | `RATE_LIMITED` | 限流、并发或配额拒绝 |
| 503 | `DEPENDENCY_UNAVAILABLE` | Runtime、Model、Temporal、Sandbox 等不可用 |
| 504 | `DEPENDENCY_TIMEOUT` | 外部依赖超时 |

领域错误至少包含：

```text
AGENT_NOT_DEPLOYED
SNAPSHOT_IMMUTABLE
BUNDLE_COMPILATION_FAILED
RUNTIME_CAPABILITY_MISMATCH
RUN_ALREADY_ACTIVE
RUN_TERMINAL
RUNTIME_SESSION_NOT_RECOVERABLE
EXECUTION_FENCING_REJECTED
MODEL_BUDGET_EXCEEDED
MODEL_RATE_LIMITED
TOOL_PERMISSION_DENIED
APPROVAL_REQUIRED
APPROVAL_EXPIRED
SANDBOX_PROVISION_FAILED
SANDBOX_POLICY_DENIED
ARTIFACT_NOT_READY
WORKSPACE_QUOTA_EXCEEDED
EVENT_SEQUENCE_CONFLICT
```

## 6. 控制面核心接口

### 6.1 Agent 草稿

```text
POST   /api/v1/agents
GET    /api/v1/agents
GET    /api/v1/agents/{agent_id}
PATCH  /api/v1/agents/{agent_id}
POST   /api/v1/agents/{agent_id}/copy
POST   /api/v1/agents/{agent_id}/disable
DELETE /api/v1/agents/{agent_id}
```

创建和更新请求至少覆盖：基本信息、负责人、可见范围、Runtime 类型、资源绑定策略和标签。资源绑定使用资源 ID 加版本策略，不直接嵌入可变对象。

删除规则：

- 已有 Deployment、Schedule、子 Agent 引用或有效 Session 时不能直接物理删除。
- 普通删除进入 `DELETING` 或软删除状态，并产生审计事件。
- 已发布 Version/Snapshot 继续可供历史 Run 追溯。

### 6.2 发布

```text
POST /api/v1/agents/{agent_id}/publish-preview
POST /api/v1/agents/{agent_id}/publish
GET  /api/v1/releases/{release_id}
POST /api/v1/agents/{agent_id}/rollback
GET  /api/v1/agents/{agent_id}/versions
GET  /api/v1/agents/{agent_id}/versions/{version_id}
GET  /api/v1/agents/{agent_id}/diff
GET  /api/v1/deployments/{deployment_id}
```

`publish-preview` 是无副作用查询：在一次一致性读取中解析指定 Draft 版本，复用正式发布的 Snapshot 编译规则，并按 Runtime Target 对比当前 ACTIVE Deployment Snapshot。它不得创建 AgentVersion、AgentSnapshot、Release、Outbox、幂等记录或发布审计事实；Secret、Provider credential、完整 Prompt 和其他敏感正文只返回引用或稳定摘要。

`ready_to_publish` 只有在 Snapshot 必需绑定满足时为 true：根 Agent 必须绑定已发布 Sandbox Profile，AgentScope 还必须绑定已发布 ModelConfig。它不代表 Registry digest、签名、SBOM、扫描、Smoke 或激活已经通过，这些供应链和部署门禁在正式 Release 中重新校验。

Preview 仅供发布确认。正式 `publishAgent` 必须重新读取 Draft、解析绑定、编译 Snapshot 并执行 `expected_agent_version` CAS，不得直接信任客户端提交或缓存的 Preview 结果。

发布请求：

```json
{
  "expected_agent_version": 12,
  "runtime_targets": ["rt_agentscope_default"],
  "release_note": "Add file analysis skill",
  "run_smoke_test": true,
  "activate_on_success": true
}
```

发布返回 HTTP 202：

```json
{
  "release_id": "rel_xxx",
  "workflow_id": "publish-agent/tenant/agent/rel_xxx",
  "status": "VALIDATING",
  "status_url": "/api/v1/releases/rel_xxx"
}
```

回滚不会修改旧 Deployment，而是以历史 Snapshot 创建新的 Release 和 Deployment 记录。

### 6.3 Prompt、Skill、MCP 和模型

这些资源统一支持：

- Draft 创建和编辑。
- Version 发布、Diff 和停用。
- 引用关系查询。
- 使用 ETag 的并发控制。
- 删除前引用检查。
- 发布时静态校验和安全扫描。

权威 operationId 和 Path 在 `agent-platform-openapi-resources-v1.yaml` 中定义。Prompt、Skill、MCP 和 ModelConfig 至少提供 create/get/list/update/delete/copy/publish/rollback/enable/disable/versions/diff/references；ModelProvider 和 RuntimeTarget 额外提供连通性或健康测试。Tenant、Member 和 Role 使用独立 Schema，不复用可发布资源的 `Resource` 响应。

Skill 与 MCP 额外返回依赖解析、权限、工具 Schema Hash、Sandbox 测试和安全扫描结果。

## 7. Session、Message 与 Run 接口

### 7.1 Session 生命周期

```text
GET    /api/v1/sessions
POST   /api/v1/sessions
GET    /api/v1/sessions/{session_id}
PATCH  /api/v1/sessions/{session_id}
POST   /api/v1/sessions/{session_id}/archive
DELETE /api/v1/sessions/{session_id}
GET    /api/v1/sessions/{session_id}/messages
GET    /api/v1/sessions/{session_id}/runs
```

```json
{
  "agent_id": "agt_xxx",
  "title": "optional",
  "metadata": {}
}
```

服务端解析当前 Deployment。Session 只保存默认 Agent/Deployment 关系，不代表后续 Run 可以无条件复用 Runtime Session。

- 重命名使用 PATCH 和 `If-Match`；归档不删除 Message、Run 和 RunEvent。
- 删除是可审计异步操作，活动 Run 存在时返回 409，不允许级联物理删除运行事实。
- Message 列表按稳定顺序和 Cursor 分页，默认返回当前分支，显式 `branch_id` 可查询历史分支。

### 7.2 创建 Run

```text
POST /api/v1/runs
```

请求：

```json
{
  "session_id": "ses_xxx",
  "client_request_id": "client-unique-id",
  "input": {
    "text": "分析附件并生成报告",
    "attachments": [
      {
        "artifact_id": "art_input_xxx"
      }
    ]
  },
  "execution": {
    "deployment_id": null,
    "timeout_seconds": 600,
    "token_budget": 20000,
    "cost_budget": {
      "amount": "10.00",
      "currency": "CNY"
    }
  }
}
```

`Idempotency-Key` Header 是创建 Run 的业务幂等键，必须提供。`client_request_id` 是可选的客户端关联标识，只用于日志、UI 和调用链关联，不参与服务端幂等判断；重复请求是否为同一业务请求只以 `Idempotency-Key + 规范化请求 Hash` 判定。

响应 HTTP 202：

```json
{
  "run_id": "run_xxx",
  "session_id": "ses_xxx",
  "status": "CREATED",
  "events_url": "/api/v1/runs/run_xxx/events",
  "stream_url": "/api/v1/runs/run_xxx/stream"
}
```

事务内写 Session Cursor、User Message、Assistant Run Message、AgentRun 和 Outbox。Temporal 启动失败由 Outbox 重试，不回滚已提交的业务记录。

### 7.3 查询、取消和重试

```text
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/cancel
POST /api/v1/runs/{run_id}/retry
```

- Cancel 返回当前取消请求状态；只有 Run 到达 CANCELLED 才表示取消完成。
- 已终态 Run 的 Cancel 幂等返回当前状态。
- Retry 创建新 Run，设置 `retry_of_run_id`，原 Run 不可变。
- 默认继承原 Snapshot、输入和附件；如允许使用当前 Deployment，必须由调用方显式选择并在 UI 展示差异。

### 7.4 Session 并发与分支

- 同一 Session 默认一个主 Run。
- 主 Run 未终态时再次创建返回 409 `RUN_ALREADY_ACTIVE`。
- 从历史消息分支时创建 `branch_id` 和新逻辑 Session Cursor，不重写原消息链。
- Message 使用内容分片结构，至少支持 text、artifact_reference、tool_reference 和 error_notice。

### 7.5 异步 Operation

```text
GET /api/v1/operations/{operation_id}
```

所有返回 `OperationAccepted` 的接口都必须将 `status_url` 指向该查询接口。统一状态为 `ACCEPTED/RUNNING/SUCCEEDED/FAILED/CANCELLED`；失败返回稳定 Error，成功返回安全的结果或业务资源引用。

## 8. RunSpec 契约

RunSpec 是平台交给 Runtime 的不可变执行说明。主要分组：

权威机器契约为 [`schemas/run-spec-v1.schema.json`](./schemas/run-spec-v1.schema.json)。自然语言分组只用于说明，不得建立第二套字段模型。

```text
identity: spec_version, run_id, trace_id, tenant_id, user_id
execution: attempt, fencing_token, idempotency_key, timeout, budgets
session: session_id, runtime_session_id, context_ref, context_hash
release: agent_id, snapshot_id, deployment_id, bundle_ref, bundle_hash
runtime: runtime_type, target_id, capability_requirements
input: user_message_ref, text, attachment_refs
prompt: compiled_prompt_ref, prompt_hash, compiler_version
bindings: model, skills, tools, mcp, knowledge
security: permission_policy, sandbox_policy, short_lived_exchange_token
workspace: workspace_uri, input/output policy, quotas
```

RunSpec Schema 必须满足：

- 明确 `spec_version`。
- 规范化序列化后计算 Hash。
- 不保存长期 Secret 明文。
- 大内容使用不可变引用和 Hash。
- Runtime 接收后验证 tenant、run、Bundle Hash、能力和 fencing token。
- Runtime 不得从控制面草稿补充缺失配置。

## 9. RunEvent 契约

权威机器契约为 [`schemas/run-event-v1.schema.json`](./schemas/run-event-v1.schema.json)。RuntimeAdapter 输出该 Schema 中 `$defs.runtime_event_candidate`，Event Service 输出完整 RunEvent。

### 9.1 Envelope

```json
{
  "schema_version": "1.0",
  "event_id": "evt_xxx",
  "source_event_id": "runtime-source-id",
  "tenant_id": "ten_xxx",
  "run_id": "run_xxx",
  "session_id": "ses_xxx",
  "sequence_no": 42,
  "event_type": "tool_call_start",
  "occurred_at": "2026-08-03T08:15:30.123Z",
  "recorded_at": "2026-08-03T08:15:30.150Z",
  "trace_id": "trace_xxx",
  "execution_attempt": 1,
  "payload_version": "1.0",
  "payload": {}
}
```

### 9.2 标准事件与最小 Payload

| Event | 最小字段 |
|---|---|
| `run_created` | `deployment_id`, `snapshot_id` |
| `run_queued` | `queue`, `queued_at` |
| `run_started` | `runtime_type`, `runtime_target_id` |
| `text_message_start` | `message_id`, `role` |
| `text_delta` | `message_id`, `delta` |
| `text_message_end` | `message_id`, `finish_reason` |
| `thinking_delta` | `message_id`, `delta`, `visibility` |
| `plan_updated` | `plan_id`, `steps` |
| `tool_call_start` | `tool_call_id`, `tool_name`, `arguments_summary` |
| `tool_call_args` | `tool_call_id`, `arguments_patch` |
| `tool_call_result` | `tool_call_id`, `status`, `result_summary`, `artifact_refs` |
| `approval_required` | `approval_id`, `tool_name`, `parameter_digest`, `expires_at` |
| `approval_resolved` | `approval_id`, `decision`, `decided_by` |
| `task_progress` | `task_id`, `current`, `total`, `message` |
| `artifact_created` | `artifact_id`, `name`, `content_type`, `size` |
| `warning` | `code`, `message`, `details` |
| `run_succeeded` | `result_message_id`, `usage`, `warnings` |
| `run_failed` | `error_code`, `message`, `retryable` |
| `run_cancelled` | `reason`, `cancelled_by` |
| `run_timeout` | `timeout_seconds`, `stage` |

每个事件建立独立 Pydantic Payload Model。Thinking 默认仅对有权限的调试用户可见，且必须遵循供应商和安全策略。

### 9.3 顺序与幂等

- `sequence_no` 在 Run 内从 1 开始严格递增。
- Event Service 是唯一 sequence 分配者。
- 幂等键为 `run_id + execution_attempt + source_event_id`。
- 旧 fencing token 的事件返回 409 `EXECUTION_FENCING_REJECTED`。
- fencing token 只出现在内部写入请求中，不保存到公开事件，不发送前端。
- 终态事件只能有一个；重复相同终态幂等，冲突终态告警并拒绝。
- 大 Payload 转为 Artifact，单事件默认上限 256KB。
- RuntimeAdapter 输出 `RuntimeEventCandidate`，不得预先构造 `event_id`、`sequence_no` 和 `recorded_at`；Event Service 持久化后才形成公开的 `RunEvent`。

## 10. 事件写入内部接口

```text
POST /internal/v1/runs/{run_id}/events:batch
```

请求允许批量事件，但客户端不提交最终 sequence_no：

```json
{
  "execution_attempt": 1,
  "execution_fencing_token": "opaque",
  "events": [
    {
      "source_event_id": "src-1",
      "event_type": "text_delta",
      "occurred_at": "2026-08-03T08:15:30.123Z",
      "payload_version": "1.0",
      "payload": {
        "message_id": "msg_xxx",
        "delta": "hello"
      }
    }
  ]
}
```

响应返回每个 source_event_id 对应的 event_id、sequence_no 和 `created/duplicate` 状态。批次部分失败时必须逐项返回，Runtime 只重试未确认事件。

高频 text_delta 可以在 50～200ms 窗口内合并。审批、工具副作用、Artifact 和终态事件不延迟合并。

## 11. SSE 与事件回放

```text
GET /api/v1/runs/{run_id}/events?after=41&limit=200
GET /api/v1/runs/{run_id}/stream?after=41
```

SSE：

```text
id: 42
event: run_event
data: {RunEvent JSON}
```

规则：

- `Last-Event-ID` 优先于 Query `after`。
- 先读取历史，再订阅实时通知；历史与实时通过 sequence_no 去重。
- Redis/PubSub 只负责唤醒，收到通知后仍从 PostgreSQL 拉取事实事件。
- 慢消费者超过服务端缓冲上限后断开，客户端按最后 sequence_no 重连。
- SSE 心跳不持久化，使用注释帧或专用非业务事件。
- 终态事件发送并确认无缺失事件后关闭连接。

## 12. RuntimeAdapter 契约

Runtime 必须声明能力：

```text
session_reuse
resume_after_worker_restart
cancel
thinking
plan
tool_call
approval_pause
files
structured_output
max_context_tokens
supported_transports
```

标准生命周期：

```text
capabilities -> validate_target -> prepare -> stream
                                      |         |
                                      |         +-> cancel / inspect
                                      +------------> resume
                                                -> release_session
```

最小返回类型：

```python
class RuntimeCapabilities(BaseModel):
    contract_version: str
    session_reuse: bool
    resume_after_worker_restart: bool
    cancel: bool
    thinking: bool
    plan: bool
    tool_call: bool
    approval_pause: bool
    files: bool
    structured_output: bool
    max_context_tokens: int | None
    supported_transports: list[Literal["internal", "acp_stdio"]]


class PreparedRuntime(BaseModel):
    run_id: str
    execution_attempt: int
    runtime_handle_ref: str
    runtime_session_id: str | None
    recovery_token_ref: str | None
    expires_at: datetime


class RuntimeInspection(BaseModel):
    state: Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "NOT_FOUND", "UNKNOWN"]
    recoverable: bool
    runtime_session_id: str | None
    completion_ref: str | None


class CancellationResult(BaseModel):
    state: Literal["REQUESTED", "CONFIRMED", "ALREADY_TERMINAL", "NOT_SUPPORTED", "UNKNOWN"]
    forced_termination_required: bool
```

`runtime_handle_ref`、`recovery_token_ref` 和 `completion_ref` 是受限内部引用，不得包含长期 Secret 或未经保护的 Provider 凭据。

统一 Runtime 错误：

- `INVALID_TARGET`
- `CAPABILITY_MISMATCH`
- `RUNTIME_UNAVAILABLE`
- `RUNTIME_TIMEOUT`
- `SESSION_INVALID`
- `SESSION_NOT_RECOVERABLE`
- `CANCEL_NOT_CONFIRMED`
- `PROTOCOL_ERROR`
- `OUTPUT_CONTRACT_VIOLATION`

每个 Adapter 必须执行同一套 Contract Test：正常文本、工具、文件、取消、超时、重复事件、Worker 重启、旧 fencing token 和终态冲突。

## 13. Model Gateway 契约

权威机器契约为 [`schemas/model-gateway-v1.schema.json`](./schemas/model-gateway-v1.schema.json)。Runtime 和 Provider Adapter 不得建立不同的模型请求、用量和错误字段。

Runtime 传递平台模型绑定和短期授权，不传长期供应商密钥。

Gateway 请求至少包含：

```text
tenant_id, user_id, agent_id, snapshot_id, run_id
model_binding_id, capability_requirements
messages/prompt reference, tools, stream
token_budget, cost_budget, timeout
```

Gateway 响应和流事件统一记录：供应商、模型、请求 ID、Token、费用、缓存、重试、fallback 和错误类型。

Fallback 规则：

- 仅对配置的可重试错误执行。
- 不跨越模型能力要求。
- 不在未知提交状态下重复 Tool Result 或用户请求。
- 每次 fallback 产生可观测事件，但不得暴露供应商密钥和内部地址。
- fallback 属于 Agent 的 Model `ResourceBinding` 路由策略，不扩展单 Provider 的 ModelConfig 契约。
- 单 Model binding 未设置 `binding_role` 时服务端规范化为 `primary`；多 Model binding 必须显式声明且恰好包含一个 `primary`，可连续增加 `fallback_1`、`fallback_2`。
- `configuration_schema_version` 固定为 `model-routing/v1`，配置仅允许位于主 binding，`fallback_error_codes` 仅允许 `RATE_LIMITED`、`PROVIDER_UNAVAILABLE`。
- 本地限流、Token/费用预算、认证、权限、能力和配置错误禁止 fallback；`PROVIDER_TIMEOUT` 以及 `submitted/unknown` 提交状态禁止 fallback。
- AgentSnapshot 编译时解析确定的 ModelConfig Version，并冻结每一路由关联的不可变 `model_binding_snapshot`；运行时不得读取 Agent Draft、ModelConfig Draft 或 Provider Draft。

## 14. Approval 契约

```text
GET  /api/v1/approvals
GET  /api/v1/approvals/{approval_id}
POST /api/v1/approvals/{approval_id}/decision
```

决策请求：

```json
{
  "decision": "APPROVED",
  "comment": "Approved for this one execution"
}
```

Approval Ticket 必须绑定：tenant、run、execution attempt、tool、参数摘要、申请人、审批策略版本、有效期和 nonce。批准后产生短期单次执行票据，Tool Gateway 原子消费。

## 15. Artifact 契约

Artifact 状态：`UPLOADING -> SCANNING -> AVAILABLE`，失败进入 `REJECTED` 或 `FAILED`，过期进入 `EXPIRED`。

```text
POST /api/v1/artifacts/uploads
POST /api/v1/artifacts/{artifact_id}/complete
GET  /api/v1/artifacts/{artifact_id}
GET  /api/v1/artifacts/{artifact_id}/download
DELETE /api/v1/artifacts/{artifact_id}
```

- 上传使用受限预签名 URL、内容长度和 Content-Type。
- Complete 校验 Hash、大小、路径和上传者。
- 扫描完成前不能被 Runtime 当作可信输入。
- 下载时重新鉴权并生成短期单资源 URL。

## 16. 契约版本管理

- 新增可选字段属于向后兼容变更。
- 删除字段、改变含义、缩小枚举或改变默认值属于不兼容变更。
- Event Payload 通过 `schema_version + payload_version` upcast 到平台当前内部模型。
- Worker 启动时向 Registry 报告支持的契约版本；Admission 拒绝不兼容组合。
- API、Worker、Bundle Compiler、Runtime Adapter 的版本兼容矩阵必须在发布前验证。
- 契约测试样例作为代码仓库中的 Golden Files 版本管理。

## 17. OpenAPI 开发完成条件

每个接口进入开发前必须具备：

- 权限动作和租户解析方式。
- 请求、响应和错误 Schema。
- 幂等、重试、超时和并发语义。
- 状态机前置条件和状态变化。
- 审计事件。
- 限流、配额和 Payload 上限。
- 单元测试、契约测试和至少一个异常验收用例。
