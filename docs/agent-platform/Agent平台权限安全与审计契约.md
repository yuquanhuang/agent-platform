# Agent 平台权限、安全与审计契约

> 文档版本：V1.2  
> 文档状态：开发输入基线

## 1. 安全模型

平台使用“身份 + 租户 + RBAC + 资源范围 + Policy”联合授权：

```text
认证身份
∩ 租户成员状态
∩ 角色动作权限
∩ 资源 Owner/Scope
∩ Agent 权限声明
∩ 用户数据权限
∩ 工具风险策略
∩ 环境与 Sandbox 上限
```

任何下游组件只能收窄权限，不能扩大上游授权。前端隐藏不构成授权。

## 2. 身份

- 用户使用 OIDC Authorization Code + PKCE。
- local/test 暂时使用 Mock OIDC，固定生成 `sub`、tenant、role、email 和 `membership_version` Claim；Mock 身份不得接受浏览器任意提交的 tenant/role，必须由服务端测试配置生成。
- staging/production 禁止启用 Mock OIDC；启动时必须校验真实 Issuer、audience、签名算法和 Claim 映射配置。
- V1 前端直接与 IdP 完成 Authorization Code + PKCE，API 使用 audience 受限的短期 Bearer Access Token；平台 API 不保存 IdP Refresh Token，不实现第二套密码登录。
- `GET /api/v1/me` 是当前平台用户、租户成员、角色和 `membership_version` 的权威查询入口。
- 退出时前端清理本地 Token 并使用 IdP End Session Endpoint；如后续启用 Cookie/BFF 模式，必须通过 ADR 启用 HttpOnly/Secure/SameSite 和 CSRF 防护。
- 内部服务使用 mTLS、SPIFFE/Workload Identity 或等价短期身份。
- Sandbox 和 Runtime 使用 audience 受限的一次性能力票据。
- 禁止内部服务共享管理员 Token。

### 2.1 成员和角色变更失效

- 每个租户成员维护单调递增 `membership_version`，角色、资源范围、状态变更时原子递增。
- API 不仅信任 Access Token 中的历史角色；每个请求使用服务端 TenantContext 和当前 `membership_version` 授权。
- 授权路径不使用只靠 TTL 过期的成员缓存。成员/角色变更事务必须同步更新权威 `membership_version` 并广播失效；服务无法确认最新版本时授权失败关闭。页面展示类非授权元数据可缓存 30 秒。
- 成员或角色停用事务提交后，所有新请求立即失效；存量 Run 按策略取消、撤销工具 Ticket 或限制为只读。

## 3. TenantContext

```python
class TenantContext(BaseModel):
    tenant_id: str
    subject_type: Literal["user", "service"]
    subject_id: str
    membership_version: int | None
    auth_time: datetime
    request_id: str
    trace_id: str
```

- tenant_id 从认证和显式管理上下文解析，不读取普通业务请求字段。
- 超级管理员跨租户必须使用管理入口、选择目标租户、填写原因并写审计。
- Repository、Cache Key、Object URI、Workflow ID、Runtime Session、Sandbox 名称必须携带或可解析 tenant_id。

## 4. 标准动作

```text
create, read, list, update, delete,
publish, rollback, disable, execute,
approve, cancel, retry, download,
manage, audit, debug, view_sensitive
```

资源类型：

```text
tenant, member, role, secret,
agent, prompt, skill, mcp, model,
runtime_target, sandbox_profile,
session, message, run, run_event,
approval, artifact, audit,
knowledge, schedule, evaluation
```

## 5. 默认角色矩阵

| 角色 | 主要允许 | 明确禁止 |
|---|---|---|
| platform_admin | 平台配置、租户管理、全局运行治理 | 默认读取租户业务内容和 Secret 明文 |
| tenant_admin | 本租户成员、角色、资源、发布、运行管理 | 跨租户操作、平台级 Secret Backend |
| agent_developer | Agent/Prompt/Skill/MCP 草稿、测试、发布申请 | 成员管理、高危生产审批、自行扩大 Sandbox 策略 |
| operator | 对话、Run 查看、定时任务、反馈 | 修改资源定义、查看 Secret/原始 Trace |
| approver | 查看允许范围审批上下文并决策 | 修改工具参数、默认自审批高风险操作 |
| user | 使用已发布 Agent、本人 Session/Artifact | 发布、查看他人 Session、调试敏感信息 |
| auditor | 只读审计、发布、审批和安全记录 | 执行 Run、修改资源、获取 Secret |

角色只是默认模板，最终以 permission action 和 condition 判断。

## 6. 关键操作权限

| 操作 | Action | 附加条件 |
|---|---|---|
| 创建 Agent | agent:create | 租户有效、未超配额 |
| 编辑 Agent | agent:update | Owner/协作者或管理员，If-Match |
| 发布 Agent | agent:publish | 所有绑定可读，安全校验通过 |
| 创建 Run | run:create | ACTIVE Deployment、预算和并发允许 |
| 查看 Run | run:read | 本人、资源授权或运维范围 |
| 查看 Thinking | run:view_sensitive | 供应商政策允许且用户有调试权限 |
| 取消 Run | run:cancel | 发起者、Owner、运维或管理员 |
| 决策审批 | approval:approve | 审批策略匹配，不能违规自审批 |
| 下载 Artifact | artifact:download | 当前仍有来源 Run/Session 权限 |
| 查看 Audit | audit:list | 审计范围过滤 |
| 管理 Secret Ref | secret:manage | 只能管理引用和轮换，不能普通读取明文 |

## 7. Policy Service

输入：

```text
tenant_context
agent_snapshot_id
deployment_id
run_id/execution_attempt
tool_id/tool_schema_hash
normalized_parameter_digest
requested_effects
environment
sandbox_policy_hash
```

输出：

```python
class PolicyDecision(BaseModel):
    decision: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
    policy_version: str
    reason_codes: list[str]
    effective_constraints: EffectiveConstraints
    approval_policy: ApprovalPolicy | None
    decision_id: str
    expires_at: datetime
```

Policy Decision 不能包含“模型声称已授权”等输入。工具执行前 Tool Gateway 必须重新校验 Decision、参数摘要和 execution ticket。

## 8. 工具风险

| 等级 | 示例 | 默认策略 |
|---|---|---|
| LOW | 只读计算、受控 Workspace 读取 | 允许，受资源限制 |
| MEDIUM | 外部只读 API、写 Workspace | Policy 校验，可配置审批 |
| HIGH | 外发数据、生产写 API、Shell、安装依赖 | 默认审批，Run Sandbox |
| CRITICAL | 管理员变更、删除生产数据、宿主控制 | 默认禁止，需平台级例外流程 |

Skill/MCP 声明的风险只能提高，不能降低平台识别的风险。

## 9. Approval 与 Execution Ticket

Approval 绑定：

```text
tenant_id, run_id, execution_attempt,
tool_name, tool_schema_hash, parameter_digest,
requester_id, policy_version, deployment_id,
expires_at, single_use
```

批准后生成一次性 Ticket：

- 只保存 nonce Hash。
- Tool Gateway 通过原子条件更新消费。
- 参数、Schema、Deployment、权限、Policy Version 或 Attempt 变化即失效。
- Ticket 过期、重放和跨 Run 使用返回 403/409 并写安全审计。

## 10. Secret

- 配置数据库只保存 secret_ref、backend、purpose、version、rotation metadata。
- Secret 不进入 Snapshot、Bundle、RunSpec、RunEvent、Prompt Diff、Trace 和前端响应。
- Bundle 可以保存 secret_ref 和用途，不保存解析值。
- Runtime 通过 Model Gateway、Tool Gateway 或 Secret Broker 获取短期能力。
- Secret 使用、失败、轮换、撤销和异常访问全部审计。

## 11. Prompt Injection 与不可信数据

以下内容均标记为 untrusted：用户输入、模型输出、知识库内容、MCP 返回、Skill 文件、上传文件、网页内容、Codex 输出。

控制要求：

- 不可信内容不能修改系统策略、权限和 Sandbox Policy。
- Tool Schema 和授权来自 Snapshot，不从模型文本解析。
- 检索内容使用明确边界标签，且不赋予工具权限。
- Prompt Injection 检测只提供风险信号，不作为唯一授权控制。
- 外发数据前重新执行数据分类和 Policy 判断。

## 12. Artifact 与数据访问

- Artifact 权限从 tenant、owner、Session、Run、来源资源和当前成员状态共同判断。
- 预签名 URL 短期有效、绑定单对象和响应 Header。
- URL 生成时重新鉴权，不因为曾经生成过而永久授权。
- 用户或成员停用后，新下载立即失败；已签 URL 应尽可能通过代理或短 TTL 降低风险。

## 13. 审计事件

必须审计：

```text
auth.login/auth.failure
tenant.switch_admin_context
role.bind/role.unbind
secret.create/rotate/use/revoke/failure
resource.create/update/delete/disable
agent.publish/rollback/deployment.activate
run.create/cancel/retry/force_terminate
approval.request/approve/reject/expire/ticket.consume/ticket.replay
tool.execute/tool.denied
sandbox.provision/quarantine/destroy/failure
artifact.export/download/delete/denied
security.cross_tenant_denied
```

AuditLog 字段：

```text
event_id, occurred_at, tenant_id,
actor_type, actor_id, source_ip/client,
action, resource_type, resource_id,
result, reason_codes, change_digest,
request_id, trace_id, run_id,
metadata_schema_version, metadata
```

Change Digest 脱敏；不保存 Secret、完整 Prompt、完整工具参数和文件内容。

## 14. 管理员与 Break-glass

紧急权限必须：

- 单独身份或强认证。
- 明确时间窗口和资源范围。
- 填写工单/原因。
- 实时告警和完整审计。
- 默认只读；写操作需要二人复核。
- 到期自动撤销并复盘。

## 15. 安全错误语义

- 无权感知资源时统一返回 404。
- 已知资源但动作禁止返回 403。
- 不返回策略源码、其他租户 ID、内部地址、SQL、堆栈和 Secret。
- 高频拒绝、Ticket 重放、跨租户尝试和 Sandbox 越界触发安全告警。

## 16. 自动化安全测试矩阵

至少覆盖：

- 每个资源的跨租户 read/list/update/delete/execute/download。
- tenant_id Header/Body/Query 注入。
- Cache、SSE、对象 URL、Workflow、Runtime Session 和 Sandbox 串租户。
- 自审批、过期审批、参数变化、Ticket 重放。
- Prompt Injection 调用未授权工具和外发数据。
- Secret 出现在 API、日志、事件、Bundle、异常和浏览器状态。
- 角色停用、成员停用和 Token 过期后的存量 Run 行为。
