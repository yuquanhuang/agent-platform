# Agent 平台 Temporal 工作流与活动契约

> 文档版本：V1.2  
> 文档状态：开发输入基线  
> SDK：Temporal Python SDK

## 1. 固定原则

Workflow/Activity/Signal/Query 的输入输出必须定义在 `backend/packages/contracts/temporal` 的带版本强类型模型中，默认禁止未声明字段，并从同一源生成 Golden JSON。Workflow 实现、Activity 实现和测试不得各自重复定义 Payload，也不再拆分新的 Temporal 补充文档。

- 所有 Agent Run 和 Agent 发布从第一次生产实现开始进入 Temporal Workflow。
- Workflow 只保存生命周期、Activity 结果、Signal、Timer 和少量可查询状态。
- Token、text_delta、普通进度和高频资源指标不进入 Workflow History。
- Workflow 代码不得访问网络、数据库、对象存储、随机数或系统时钟。
- 所有副作用位于 Activity，并具有幂等键和明确 Retry Policy。
- Workflow ID 可由业务 ID 确定性推导；重复启动返回已有 Workflow，不创建重复副作用。

## 2. Namespace 与 Task Queue

| 环境 | Namespace |
|---|---|
| local | `agent-platform-local` |
| test | `agent-platform-test` |
| staging | `agent-platform-staging` |
| production | `agent-platform-production` |

Task Queue：

| Queue | Worker | 用途 |
|---|---|---|
| `control-plane` | control worker | 发布、回滚、资源编译 |
| `run-orchestrator` | run worker | Run 主 Workflow、状态和 Signal |
| `runtime-agentscope` | AgentScope worker | AgentScope prepare/execute/cancel/inspect |
| `runtime-codex` | Codex worker | ACP prepare/execute/cancel/inspect |
| `sandbox` | sandbox worker | provision、lease、destroy、export |
| `event-maintenance` | event worker | 事件物化、归档和对账 |
| `knowledge` | knowledge worker | V1 知识库导入 |

运行时类型必须路由到独立 Queue，Codex 长进程不得占用 AgentScope Worker 容量。

## 3. Workflow ID

```text
AgentRunWorkflow:      run/{tenant_id}/{run_id}
PublishAgentWorkflow: publish/{tenant_id}/{release_id}
OfflineTaskWorkflow:  offline/{tenant_id}/{task_id}
CleanupWorkflow:      cleanup/{tenant_id}/{resource_type}/{resource_id}/{generation}
KnowledgeWorkflow:    knowledge/{tenant_id}/{document_id}/{version}
```

规则：

- ID 中只使用服务端生成的不透明 ID 和固定前缀。
- Temporal Search Attribute 保存 tenant_id、run_id、agent_id、release_id、runtime_type、status、created_at。
- 不保存 Prompt、用户输入、文件名、Secret、参数全文和高敏业务数据。

## 4. 公共 Activity 选项

| Activity 类别 | Start-To-Close | Heartbeat | 最大尝试 | 初始退避 | 最大退避 |
|---|---:|---:|---:|---:|---:|
| DB 短操作 | 10s | 无 | 5 | 1s | 15s |
| Bundle 编译 | 10m | 30s | 2 | 5s | 30s |
| Sandbox provision | 2m | 10s | 3 | 2s | 20s |
| Runtime 长执行 | Run timeout + 60s | 10s | 1 | - | - |
| Runtime inspect/cancel | 30s | 无 | 3 | 1s | 5s |
| Artifact 上传/扫描 | 10m | 30s | 5 | 2s | 60s |
| 外部只读 HTTP | 30s | 无 | 3 | 1s | 10s |
| 外部写操作 | 60s | 按需 | 1，除非声明幂等 | - | - |

`Schedule-To-Close` 必须覆盖排队上限；任何 Queue 不允许无限等待。不可重试错误包括验证失败、权限拒绝、能力不匹配、Schema 不兼容、预算硬限制、明确的业务状态冲突。

## 5. AgentRunWorkflow

### 5.1 输入

```python
class AgentRunWorkflowInput(BaseModel):
    workflow_contract_version: Literal["1.0"]
    tenant_id: str
    run_id: str
    initial_execution_attempt: int
```

Workflow 不携带完整 Prompt、附件或 Secret。Activity 通过 run_id 获取不可变 RunSpec，并验证 Hash。

### 5.2 可查询状态

```python
class AgentRunWorkflowState(BaseModel):
    run_id: str
    status: RunStatus
    current_activity: str | None
    execution_attempt: int
    runtime_session_id: str | None
    sandbox_instance_id: str | None
    latest_sequence_no: int
    cancel_requested: bool
    waiting_approval_id: str | None
```

### 5.3 主流程

```text
load_and_validate_run
→ mark_run_queued
→ reserve_quota_and_budget
→ create_run_spec
→ provision_sandbox
→ prepare_runtime
→ mark_run_running
→ execute_runtime
→ process completion/approval/cancel/timeout
→ export_required_artifacts
→ write_terminal_event_and_state
→ release_runtime_session_if_needed
→ release_sandbox_and_budget
```

每个清理步骤放在 Workflow `finally` 逻辑中，并分别记录成功、失败和待对账状态。

### 5.4 Activity 清单

| Activity | 输入 | 输出 | 幂等键 | 重试 |
|---|---|---|---|---|
| `load_and_validate_run` | tenant_id, run_id | RunExecutionContext | run_id | 可重试 DB 错误 |
| `mark_run_queued` | run_id, expected=CREATED | version | run_id+QUEUED | 可重试 |
| `reserve_quota_and_budget` | run_id, budgets | reservation_id | run_id+policy_version | 可重试 |
| `create_run_spec` | run_id, attempt | RunSpecRef+hash | run_id+attempt | 可重试 |
| `provision_sandbox` | RunSpecRef | SandboxHandle | run_id+attempt+policy_hash | 可重试，Provider 创建需幂等 |
| `prepare_runtime` | RunSpecRef, SandboxHandle | PreparedRuntimeRef | run_id+attempt | Runtime Adapter 定义 |
| `execute_runtime` | PreparedRuntimeRef | RuntimeCompletion | run_id+attempt | 默认不自动重试 |
| `inspect_runtime` | run_id, attempt | RuntimeInspection | 查询无副作用 | 可重试 |
| `cancel_runtime` | run_id, attempt, fencing | CancellationResult | run_id+attempt+cancel | 可重试 |
| `export_artifacts` | run_id, workspace | ArtifactResult[] | run_id+export_generation | 可重试 |
| `finalize_run` | terminal candidate | Run terminal | run_id+terminal_type | 可重试、CAS |
| `release_resources` | handles | ReleaseResult | resource+lease token | 可重试 |

### 5.5 Runtime 长 Activity

`execute_runtime` 通过 heartbeat 保存：

```text
runtime_handle_ref
runtime_session_id
last_source_event_id
last_confirmed_event_sequence
sandbox_instance_id
provider_progress_ref
```

Worker 重启时：

1. Workflow 不直接重试用户 Prompt。
2. 先 `inspect_runtime` 判断原执行是否仍运行、已完成、已失败或未知。
3. 只有确认原执行失效且 Runtime 声明可安全恢复时，生成新 attempt 和 fencing token。
4. 未知提交状态进入 `SESSION_NOT_RECOVERABLE` 或人工处理，不重复写工具和用户 Prompt。

## 6. Signal 契约

### 6.1 cancel_run

```python
class CancelRunSignal(BaseModel):
    signal_id: str
    requested_by: str
    requested_at: datetime
    reason: str | None
```

- `signal_id` 在 Workflow 内幂等。
- 终态收到取消只记录幂等响应，不改变状态。
- CREATED/QUEUED/PREPARING/RUNNING/WAITING_APPROVAL 都必须可取消。
- 进入 CANCELLING 后先调用 Runtime cancel，宽限期后强制终止 Sandbox。

### 6.2 approval_decided

包含：`signal_id, approval_id, decision_id, decision, ticket_ref, decided_at`。

- Workflow 必须重新加载 Approval 状态并校验参数摘要、策略版本和有效期。
- 不信任 Signal 中的明文工具参数或权限声明。

### 6.3 external_task_completed

包含：`signal_id, task_id, result_ref, result_hash, status`。Workflow 通过 Activity 加载并验证外部结果。

### 6.4 extend_timeout

仅管理员或策略允许的任务可使用，包含原超时时间、新超时时间、原因和授权审计 ID。不得超过平台最大 Run 时长。

## 7. 超时与取消竞态

- Workflow Timer 负责业务总超时，Activity Timeout 负责单次执行保护。
- 第一个通过 AgentRun CAS 写入的终态生效。
- 超时触发后仍执行 Runtime cancel 和 Sandbox 清理。
- 取消请求早于超时但取消未完成时，最终状态按 CAS 结果确定，并记录竞态审计。
- `CANCELLED` 表示执行已确认停止或 Sandbox 已强制终止；仅收到请求不能写 CANCELLED。

## 8. 审批等待

```text
Runtime candidate approval_required
→ Event Service 持久化
→ create_approval_request Activity
→ Run WAITING_APPROVAL
→ Workflow 等待 Signal 或 Timer
→ 批准：校验并取得一次性 Ticket，恢复 Runtime
→ 拒绝/过期：按 Agent Policy 让工具失败或取消 Run
```

等待超过 Sandbox 保留阈值时，释放 Sandbox 和 Secret；恢复时使用同一 Snapshot、RunSpec Hash 和新的 execution_attempt/执行票据重建环境。

## 9. PublishAgentWorkflow

### 9.1 输入

`tenant_id, release_id, expected_agent_version`。

### 9.2 Activities

```text
validate_release_graph
resolve_resource_versions
create_agent_version_and_snapshot
compile_runtime_bundles
scan_bundles
run_sandbox_smoke_tests
create_staged_deployments
activate_deployments
finalize_release
```

- 解析资源图使用一次一致性快照；发布期间版本变化导致 412/Release FAILED。
- `activate_deployments` 使用 DB 事务和 fencing，只允许一个有效激活者。
- 任何激活前失败不得影响当前 ACTIVE Deployment。
- 回滚创建新的 Release，并引用历史 Snapshot。

## 10. Continue-As-New

满足任一条件时在安全点 Continue-As-New：

- History 事件数达到 20,000。
- History 大小估算达到 20MB。
- 长期审批或外部等待超过 7 天。
- Workflow 运行超过 30 天且仍需继续。

Continue-As-New 输入保存业务 ID、状态摘要、attempt、等待对象和已处理 Signal ID 集合摘要，不复制文本 Delta 和大 Payload。

## 11. Workflow 版本升级

- Workflow 行为不兼容变更使用 Temporal Versioning/Patching API。
- CI 必须对当前和上一生产版本的脱敏 History 运行 Replay Test。
- Activity 输入输出增加可选字段可兼容；删除、改义、收窄枚举需新版本 Activity 名称。
- Worker 发布使用 Build ID/Worker Versioning 或等价方案，禁止不兼容 Worker 同时处理同一 Task Queue。

## 12. Outbox 启动规则

创建 Run/Release 的数据库事务只写业务记录和 Outbox。Outbox Consumer：

1. 以确定 Workflow ID 调用 Start。
2. 已存在相同 ID 视为成功。
3. 记录 Temporal Run ID 和启动结果。
4. 超过重试阈值进入 DEAD 并告警。
5. Reconciler 扫描长时间无 Workflow 的 CREATED/REQUESTED 记录并幂等补启动。

## 13. 测试要求

- 每个 Workflow 有正常、取消、超时、Activity 重试和不可重试错误测试。
- 使用 Temporal Test Environment 跳过 Timer。
- Runtime 未知状态不得触发第二次 Prompt 的测试。
- activate_deployments 重试不产生两个 ACTIVE Deployment。
- Signal 重复、乱序、过期均有测试。
- 当前和上一版本 History Replay 通过。
- Worker 崩溃、Temporal 暂不可用和 Queue 堆积有集成测试。
