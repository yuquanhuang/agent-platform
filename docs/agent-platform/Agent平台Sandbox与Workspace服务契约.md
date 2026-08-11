# Agent 平台 Sandbox 与 Workspace 服务契约

> 文档版本：V1.3
> 文档状态：开发输入基线

## 1. 服务边界

Sandbox Manager 是独立受信服务，负责隔离执行环境的创建、租约、进程控制、资源限制、文件导出、凭据撤销、清理和对账。

固定边界：

- API 服务不能访问 Docker Socket、Kubernetes Admin 凭据或宿主执行接口。
- Runtime Worker 只能通过 Sandbox API 操作执行环境。
- Sandbox 不访问平台主数据库。
- Secret 以短期、最小权限、可撤销方式注入。
- MVP 默认每个 Run 一个 Sandbox；Session Sandbox 是 V1 受控能力。

## 2. 逻辑 URI

规范形式：

```text
workspace://tenant/{tenant_id}/user/{user_id}/session/{session_id}/runs/{run_id}/{relative_path}
artifact://tenant/{tenant_id}/artifact/{artifact_id}
bundle://tenant/{tenant_id}/snapshot/{snapshot_id}/runtime/{runtime_type}/{bundle_hash}
```

规则：

- URI 由 WorkspaceUri 类型构造，不允许业务代码字符串拼接。
- `relative_path` 使用 UTF-8 NFC、`/` 分隔，禁止绝对路径、空段、`.`、`..`、NUL 和控制字符。
- 解析后执行路径归一化、根目录包含检查、软链接检查和 TOCTOU 防护。
- 物理路径、Bucket 和 Provider ID 不返回前端或模型。

## 3. SandboxPolicy

权威 Schema：[sandbox-policy-v1.schema.json](./schemas/sandbox-policy-v1.schema.json)。

必须字段：

```text
schema_version
scope: run/session
image_digest
cpu_limit
memory_mb
disk_mb
pids_limit
timeout_seconds
network.mode
filesystem policy
process policy
artifact policy
```

平台策略、租户上限、Agent Policy、用户权限和工具风险取最严格结果。客户端不能通过请求放宽策略。

## 4. 服务认证

- 内部 API 使用 mTLS 或 Workload Identity。
- 每次 Provision 使用短期 `provision_token`，绑定 tenant、run、policy_hash、bundle_hash、过期时间和 nonce。
- Sandbox Manager 校验调用方身份、Token、租户、Hash 和重放状态。
- 所有控制操作记录 caller service、tenant、run、sandbox、trace_id 和结果。

## 5. 内部 API

基础路径：`/internal/v1/sandboxes`。

内部请求/响应使用 Pydantic v2 严格模型，等价于 JSON Schema `additionalProperties: false`。每个接口必须生成并冻结 Golden File，不允许 Provider Adapter 自行增加公共字段。内部契约不另建平行文档，以本节字段、`sandbox-policy-v1.schema.json` 和代码中 `backend/packages/contracts/sandbox_api` 生成的 Schema 为同源制品。

### 5.1 Provision

```text
POST /internal/v1/sandboxes
Idempotency-Key: sandbox/{run_id}/{attempt}/{policy_hash}
```

请求：

```json
{
  "tenant_id": "ten_xxx",
  "user_id": "usr_xxx",
  "session_id": "ses_xxx",
  "run_id": "run_xxx",
  "execution_attempt": 1,
  "scope": "run",
  "policy_ref": "immutable://sandbox-policy/...",
  "policy_hash": "sha256:...",
  "bundle_ref": "bundle://...",
  "bundle_hash": "sha256:...",
  "workspace_uri": "workspace://tenant/.../runs/run_xxx/",
  "provision_token": "opaque",
  "trace_id": "trace_xxx"
}
```

响应 202：`sandbox_id, operation_id, status=ACCEPTED, status_url`，其中 `status_url` 固定指向 `/api/v1/operations/{operation_id}`，最终状态为 `SUCCEEDED/FAILED/CANCELLED`。

相同幂等键和请求 Hash 返回原 operation；不同请求返回 409。

### 5.2 Query/Inspect

```text
GET /internal/v1/sandboxes/{sandbox_id}
GET /internal/v1/sandboxes/{sandbox_id}/inspect
```

Inspect 返回状态、资源使用摘要、主进程状态、Lease、网络策略 Hash、Bundle Hash，不返回 Secret 和宿主物理路径。

### 5.3 Acquire Lease

```text
POST /internal/v1/sandboxes/{sandbox_id}/leases
```

请求包含 `run_id, execution_attempt, execution_fencing_token, ttl_seconds`。

- Run Sandbox 创建成功即绑定唯一 Run Lease。
- MVP 不得为 `scope=run` 的请求查找或复用 Session Sandbox。Session Sandbox 只在对应 V1 增量 Epic 启用后开放，且同时只允许一个活动 Lease。
- 旧 fencing token 无法续期或释放新 Lease。

### 5.4 Start Process

```text
POST /internal/v1/sandboxes/{sandbox_id}/processes
```

请求使用结构化 argv，不接受 Shell 字符串：

```json
{
  "run_id": "run_xxx",
  "execution_attempt": 1,
  "execution_fencing_token": "opaque",
  "process_id": "proc_xxx",
  "argv": ["python", "-m", "runtime_entry"],
  "working_directory": "workspace://.../work/",
  "environment_refs": ["capability://model-gateway/run_xxx"],
  "stdin_mode": "PIPE",
  "stdout_mode": "PIPE",
  "timeout_seconds": 600,
  "trace_id": "trace_xxx"
}
```

- `run_id, execution_attempt, execution_fencing_token` 必须匹配当前未过期活动 Lease 和 RunAttempt；旧 Attempt、过期 Lease 或旧 token 返回 `SANDBOX_FENCING_REJECTED`。
- 默认 `shell=false`。
- argv、工作目录、环境引用必须符合 Policy。
- 不接受明文 Secret 环境变量。

### 5.5 Cancel/Terminate

```text
POST /internal/v1/sandboxes/{sandbox_id}/processes/{process_id}/cancel
POST /internal/v1/sandboxes/{sandbox_id}/terminate
```

Process Cancel 请求必须携带 `run_id, execution_attempt, execution_fencing_token, trace_id`，并匹配当前未过期活动 Lease 和 RunAttempt。取消流程：SIGTERM/协议取消 → 宽限期 → SIGKILL/Provider 强制终止。接口幂等，返回当前有效状态。

Terminate 是管理面或对账使用的强制操作，不代表当前 Lease Holder 的正常控制权；它必须通过独立的 Workload Identity、权限和审计边界调用，不接收或替代 Run fencing proof。

### 5.6 Artifact Export

```text
POST /internal/v1/sandboxes/{sandbox_id}/artifacts:export
```

请求只允许 Workspace URI 和声明的导出策略。Sandbox Manager：

1. 使用安全文件句柄打开目标。
2. 校验路径、软链接、文件类型、大小和配额。
3. 计算 Hash。
4. 上传隔离区对象存储。
5. 创建 Artifact UPLOADING/SCANNING 元数据请求。
6. 完成扫描后返回 Artifact Reference。

### 5.7 Release/Destroy

```text
POST /internal/v1/sandboxes/{sandbox_id}/release
DELETE /internal/v1/sandboxes/{sandbox_id}
```

- Release 请求必须携带 `run_id, execution_attempt, execution_fencing_token, trace_id`。活动 Attempt 必须匹配当前未过期 Lease；Run 已进入终态后，允许使用该 Attempt 最后一次有效 Lease token 幂等完成清理，但旧 Attempt 或其他 token 仍被拒绝。
- Release 撤销 Lease、短期能力和网络访问。
- Run Sandbox Release 后进入 TERMINATING。
- Session Sandbox 清理成功可回 READY；失败进入 QUARANTINED。
- Destroy 与 Terminate 相同，属于独立管理/对账强制操作，不接收 Lease token；调用方必须具有强制清理权限并写审计。
- Destroy 失败不能标记 TERMINATED，必须告警和对账。

## 6. 状态机

```text
REQUESTED -> PROVISIONING -> READY -> IN_USE
REQUESTED/PROVISIONING -> FAILED
IN_USE -> READY                 Session Sandbox 清理成功
IN_USE/READY -> QUARANTINED     清理、安全或完整性失败
IN_USE/READY/QUARANTINED -> TERMINATING -> TERMINATED
TERMINATING -> QUARANTINED      Provider 无法确认销毁
```

每次状态转换记录 expected status、caller、reason、provider observation 和 audit ID。

## 7. Run Sandbox

创建时：

- 只读挂载固定 Digest 的基础镜像和 Runtime Bundle。
- 独立可写 Run Workspace。
- 不挂载其他 Session/Run 目录。
- 独立网络身份和最小 egress。
- Run 完成、失败、取消或超时后撤销能力并销毁。

默认最大值由 Sandbox Profile 给出，平台另设不可突破的全局上限。

## 8. Session Sandbox

只有同时满足以下条件才允许：

- Agent 显式声明低风险并通过安全评审。
- 无高风险生产写工具或常驻后台服务。
- 租户和用户具有使用权限。
- compatibility_hash 完全一致。
- 上一次 Run 清理和完整性检查成功。

Compatibility Hash 输入：

```text
tenant_id + user_id + session_id + deployment_id + bundle_hash
+ permission_policy_hash + sandbox_policy_hash + runtime_target_id
```

任一变化立即 STALE 并重建。Session Sandbox 多个 Run 必须串行获取 Lease。

## 9. 网络策略

- 默认 `none`。
- allowlist 同时限制域名、解析 IP、端口、协议和重定向目标。
- DNS 解析和每次连接均拒绝私网、环回、链路本地和云元数据地址，除非显式受控服务例外。
- HTTP 重定向重新执行完整校验。
- 生产通过 egress proxy/NetworkPolicy 执行，不只依赖应用层检查。
- 网络日志脱敏 Query、Authorization 和敏感 Body。

## 10. 文件与进程安全

- 非 root、只读根文件系统、drop capability、seccomp、AppArmor/SELinux。
- 禁止 privileged、Host Network、Host PID、Host IPC、任意 HostPath。
- 限制 fork、线程、打开文件数、文件数量、单文件和总写入量。
- 禁止设备文件、Socket、FIFO、硬链接和越界软链接导出。
- ZIP/TAR 解包限制文件数、展开大小、压缩比和嵌套层数。
- 后台进程和子进程树必须在清理阶段终止。

## 11. Secret 与能力注入

优先顺序：

1. 本地代理/Unix Socket 提供一次性能力。
2. 短期文件描述符或内存文件。
3. 短期环境变量，仅在无法避免时使用。

短期能力绑定 tenant、run、sandbox、用途、audience、权限和过期时间。Run 结束立即撤销；不得包含长期 Provider Key。

## 12. Workspace 配额和生命周期

Workspace 记录逻辑配额与实际用量：

- 单文件上限。
- 文件数量上限。
- Run 总写入量。
- Session 总存储。
- Artifact 导出总量。

Run Workspace 默认 7 天；Artifact 默认 30 天。删除先撤销下载和引用，再执行对象清理；审计事实保留。

## 13. 错误码

```text
SANDBOX_POLICY_DENIED
SANDBOX_PROVISION_FAILED
SANDBOX_NOT_READY
SANDBOX_LEASE_CONFLICT
SANDBOX_FENCING_REJECTED
SANDBOX_RESOURCE_EXCEEDED
SANDBOX_NETWORK_DENIED
SANDBOX_PROCESS_FAILED
SANDBOX_CANCEL_NOT_CONFIRMED
SANDBOX_CLEANUP_FAILED
SANDBOX_QUARANTINED
WORKSPACE_URI_INVALID
WORKSPACE_PATH_ESCAPE
WORKSPACE_QUOTA_EXCEEDED
ARTIFACT_EXPORT_DENIED
ARTIFACT_SCAN_FAILED
```

## 14. 对账

Reconciler 定期比较数据库状态与 Provider 实际状态：

- Run 终态但 Sandbox IN_USE。
- Sandbox READY/IN_USE 但 Lease 过期。
- Provider 已消失但数据库非终态。
- 数据库 TERMINATED 但 Provider 资源仍存在。
- Secret Capability 未撤销。
- Session Sandbox Compatibility Hash 不一致。
- 对象存储孤儿文件或 Artifact 缺失。

修复动作必须幂等并写 AuditLog。

## 15. 验收

- 两个租户、用户、Session、Run 文件不可互读。
- 路径穿越、软链接、硬链接、Zip Slip 和 TOCTOU 测试全部阻断。
- fork bomb、磁盘填满、内存耗尽和超时被限制。
- DNS rebinding、重定向私网和云元数据访问被阻断。
- API/Runtime Worker 无法获得宿主控制权限。
- Worker 崩溃后 Sandbox 可对账、撤销能力并清理。
- Session Sandbox 残留进程测试失败时进入 QUARANTINED，不复用。
