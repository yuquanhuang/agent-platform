# Agent 平台前端页面与交互契约

> 文档版本：V1.3  
> 文档状态：开发输入基线

## 1. 目标

本文定义 V1 前端的路由、页面状态、字段、权限、API 映射和流式事件处理。未在当前迭代启用的功能不得展示可点击入口。

V1 固定技术栈：TypeScript、Vue 3 Composition API、Vite、Vue Router 4、Pinia、`@tanstack/vue-query`、Element Plus。表单使用统一校验层，并根据 OpenAPI/JSON Schema 复用类型与约束。前端框架见 [ADR-001](./ADR-001-前端框架采用Vue3.md)，组件库和封装边界见 [ADR-002](./ADR-002-前端UI组件库采用ElementPlus.md)。

## 2. 全局规则

- 品牌名称和 Logo 统一为“agent平台”；“Agent”仅作为资源类型名称使用。
- tenant_id 不从 URL Query 或表单提交；由登录上下文确定。
- 所有列表使用服务端分页、稳定排序和白名单筛选。
- 所有编辑请求携带 `If-Match`。
- 所有创建和副作用请求生成 `Idempotency-Key`。
- 长任务页面保存 operation/run/release ID，刷新后可恢复。
- 403、404 不泄露资源是否存在。
- Secret 只显示引用、用途和掩码，不进入浏览器状态或日志。
- Vue 组件默认使用 Composition API 和 `<script setup lang="ts">`；公共逻辑提取为强类型 composable。
- 服务端状态、缓存、重试和失效由 `@tanstack/vue-query` 管理；Pinia 只管理身份上下文、UI 会话和跨页面客户端状态，禁止复制 Query Cache 中的服务端实体。
- 路由、权限守卫和 Feature Flag 使用 Vue Router 4 的统一入口，页面不得创建平行路由或权限状态。
- RunEvent/SSE 状态归并逻辑必须是可独立测试的纯函数或 Store Action，不绑定组件生命周期保存唯一运行事实。

## 3. 路由表

| 路由 | 页面 | MVP | 权限动作 |
|---|---|---:|---|
| `/agents` | Agent 列表 | 是 | `agent:list` |
| `/agents/new` | 创建 Agent | 是 | `agent:create` |
| `/agents/:id/edit` | 编辑草稿 | 是 | `agent:update` |
| `/agents/:id/versions` | 版本与发布 | 是 | `agent:read_version` |
| `/agents/:id/chat` | 对话调试 | 是 | `run:create` |
| `/prompts` | Prompt 列表 | 是 | `prompt:list` |
| `/prompts/:id/edit` | Prompt 编辑 | 是 | `prompt:update` |
| `/skills` | Skill 列表 | 是 | `skill:list` |
| `/skills/:id/edit` | Skill 编辑 | 是 | `skill:update` |
| `/mcp-servers` | MCP 列表 | 是 | `mcp:list` |
| `/mcp-servers/:id/edit` | MCP 编辑 | 是 | `mcp:update` |
| `/models/providers` | 模型供应商 | 是 | `model_provider:list` |
| `/models/configs` | 模型配置 | 是 | `model_config:list` |
| `/runs` | Run 监控 | 是 | `run:list` |
| `/runs/:id` | Run 详情 | 是 | `run:read` |
| `/approvals` | 审批中心 | 是 | `approval:list` |
| `/artifacts/:id` | Artifact 详情 | 是 | `artifact:read` |
| `/admin/runtime-targets` | Runtime Target | 是 | `runtime_target:manage` |
| `/admin/sandbox-profiles` | Sandbox Profile | 是 | `sandbox_profile:manage` |
| `/admin/tenants` | 租户 | 是 | `tenant:manage` |
| `/admin/members` | 成员角色 | 是 | `member:manage` |
| `/admin/audit` | 审计日志 | 是 | `audit:list` |
| `/knowledge` | 知识库 | V1 增量 | `knowledge:list` |
| `/schedules` | 定时任务 | V1 增量 | `schedule:list` |
| `/evaluations` | 评测 | V1 增量 | `evaluation:list` |
| `/remote-agents` | A2A 远程 Agent | V1 增量 | `remote_agent:list` |

V1 增量路由必须受 Feature Flag 和服务端 Capability 双重控制。

## 4. 通用页面状态

每个页面必须实现：

```text
initial/loading
ready
empty
permission_denied
not_found
dependency_unavailable
partial_data
submitting
conflict
success
```

- `conflict` 展示服务端最新版本、用户本地变更和重新加载入口，不自动覆盖。
- `partial_data` 明确不可用区域并允许重试，不把部分失败显示为全成功。
- 异步操作提交后禁用重复操作，但允许刷新和通过 ID 恢复。

## 5. Agent 列表

### 5.1 查询

调用 `listAgents`：`cursor, limit, status, keyword`。默认排序 `updated_at desc, id desc`。

### 5.2 卡片字段

```text
name, description, runtime_type, status, visibility,
tags, active_deployment_id, updated_at
```

按钮状态：

- 去对话：存在 ACTIVE Deployment 且用户有 `run:create`。
- 编辑：有 `agent:update` 且 Agent 非 DELETING/DELETED。
- 发布：草稿有效且有 `agent:publish`。
- 删除：有 `agent:delete`；点击后先加载引用检查。

## 6. Agent 六步编辑器

表单保存为 Agent Draft，不直接创建 Snapshot。

### 6.1 基本信息

| 字段 | 类型 | 必填 | 校验 |
|---|---|---:|---|
| code | string | 创建时是 | `^[a-z][a-z0-9_-]{2,63}$`，创建后不可修改 |
| name | string | 是 | 1..100 |
| description | string | 否 | <=2000 |
| icon_artifact_id | string | 否 | AVAILABLE Artifact |
| tags | string[] | 否 | <=20，每项 <=32 |
| visibility | enum | 是 | private/tenant |
| default_language | string | 是 | BCP 47，默认 zh-CN |
| owner_user_id | string | 是 | 当前租户成员 |

### 6.2 Runtime、模型和 Prompt

- runtime_type：agentscope/codex。
- model binding：AgentScope 必填；Codex 根据 Runtime Target Policy 解析。
- prompt binding：必填固定或 publish-time resolve 策略。
- 模型参数只展示模型能力允许的字段。
- Codex V1 只展示 ACP STDIO Target。

### 6.3 Skill、工具和审批

- Skill 多选，显示版本策略、权限摘要、扫描状态。
- 工具显示风险等级、最大调用次数和审批规则。
- 高风险能力必须有明确提示，不允许只展示通用确认框。

### 6.4 MCP、知识和子 Agent

- MVP 支持一个或多个已发布 MCP 配置，至少一个 Streamable HTTP MCP。
- 子 Agent 绑定使用 resource_type=`agent`，只能绑定有可用 Deployment 的 Agent。
- 知识库区域在 V1 增量前隐藏。

### 6.5 Sandbox、Workspace 和预算

- Sandbox Profile 必填。
- MVP scope 固定为 run；无权用户看不到 session 选项。
- Token/费用/时间预算显示平台和租户上限，客户端不能提交更高值。

### 6.6 发布

发布前必须显示：

- Draft 校验错误。
- 资源解析后的具体版本。
- Snapshot 与当前 Deployment Diff。
- 权限、网络、Sandbox、模型、Secret Reference Diff。
- Bundle Compiler、扫描和冒烟测试选项。

提交调用 `publishAgent`，使用 `Idempotency-Key` 和当前 `resource_version`。进入发布状态页后轮询 `getRelease`，不依赖原 HTTP 连接。

## 7. Prompt 页面

- 编辑器内容和变量 Schema 分开保存。
- 变量定义包含类型、必填、默认、最大长度、敏感标记和转义策略。
- 预览请求只发送测试值；敏感值不进入浏览器持久缓存。
- 发布前展示渲染、Token 估算、Diff 和引用 Agent。
- 被 Snapshot 引用的版本禁止删除。

## 8. Skill 页面

页面区域：元数据、文件树、SKILL.md、manifest.yaml、依赖、权限、测试、扫描、版本和引用。

导入流程：

```text
选择本地/ZIP/Git
→ 上传隔离区
→ 服务端解析文件清单
→ Schema 与路径校验
→ 显示依赖/权限 Diff
→ Sandbox 测试
→ 发布不可变版本
```

前端不得在浏览器执行 Skill 脚本或解析不可信压缩包。

## 9. MCP 页面

MVP 创建表单只开放 Streamable HTTP。STDIO/SSE 在 V1 增量 Feature Flag 开启后显示。

字段：`name, transport, endpoint/command, headers refs, secret refs, timeout, health policy`。

- Header 值中的 Secret 只选择 Secret Reference。
- 能力发现结果显示 tool name、description、input schema hash、风险等级和授权状态。
- 测试调用使用专用 Sandbox/Tool Gateway，不由浏览器直接调用 MCP。

## 10. 对话与调试页

### 10.1 布局

- 左：Session 列表。
- 中：消息、输入、文件和流式响应。
- 右：Run 信息、事件时间线、工具、计划、审批、Artifact、用量和调试信息。

### 10.2 创建流程

```text
上传附件并等待 AVAILABLE
→ POST /sessions（首次）
→ POST /runs，Header Idempotency-Key
→ 保存 run_id/events_url/stream_url
→ GET 历史事件
→ 建立 SSE
```

`client_request_id` 只用于 UI 关联，不作为幂等键。

Session 列表和历史的权威 API 是 `listSessions/getSession/listSessionMessages/listSessionRuns`；重命名使用 `updateSession + If-Match`，归档使用 `archiveSession`，删除使用 `deleteSession` 并跟踪 `getOperation`。两个租户之间不得复用 Session Query Cache。

### 10.3 SSE 状态归并器（Reducer）

前端状态只通过 `(run_id, sequence_no)` 推进：

- 小于等于 last_sequence 的事件忽略。
- 出现序号缺口时暂停应用新事件，调用历史接口补齐。
- SSE 断开使用 Last-Event-ID 重连并指数退避。
- 终态事件后补齐到 latest_sequence_no，再关闭流。
- Redis/实时通知不影响客户端事实判断。

Vue 实现约束：

- Run 投影状态必须按 `(tenant_id, session_id, run_id)` 复合键隔离，或使用等价的 Store 工厂；切换租户、Session 或 Run 时不得复用错误状态。
- 事件应用函数接收当前快照和单个 RunEvent，输出新快照；组件只订阅派生状态，不直接拼接协议字段。
- SSE 建连、补历史、序号缺口恢复和终态关闭封装为 composable/service，组件卸载时只释放订阅，不改变服务端 Run 生命周期。

事件处理：

| RunEvent | UI 行为 |
|---|---|
| run_created/run_queued | 更新状态和队列信息 |
| run_started | 显示 Runtime/Sandbox 摘要 |
| text_message_start | 创建临时消息块 |
| text_delta | 追加到 message_id 对应块 |
| text_message_end | 固化消息并显示 finish_reason |
| thinking_delta | 有权限时追加折叠区，否则忽略内容 |
| plan_updated | 以 plan_id 覆盖计划快照 |
| tool_call_start/args/result | 更新 tool_call_id 时间线 |
| approval_required/resolved | 创建/更新审批卡片 |
| task_progress | 更新 task_id 进度，不当作 Run 状态 |
| artifact_created | 添加 Artifact 卡片 |
| warning | 显示非阻断告警 |
| run_succeeded/failed/cancelled/timeout | 写入唯一终态 |

### 10.4 停止与重试

- Stop 调用 cancelRun；按钮进入“取消中”，只有 Run CANCELLED 才显示已取消。
- Retry 总是创建新 Run，页面展示新旧 Run 关联。
- 选择 current_deployment 重试时必须展示 Snapshot 差异并二次确认。

## 11. Run 监控与详情

列表字段：Run ID、Agent、用户、Session、Runtime、状态、创建/开始/结束时间、耗时、Token、费用、错误码。

详情 Tab：

1. 概览与状态。
2. 事件时间线。
3. Message/Prompt 引用和 Hash。
4. Runtime Session 与 Attempt。
5. Sandbox 和资源峰值。
6. Artifact。
7. 审批与工具。
8. Trace/日志跳转。

普通用户不能查看完整 Prompt、Thinking、原始 Trace 和其他用户敏感参数。

## 12. 审批中心

- 列表默认显示当前用户可审批且 PENDING 的请求。
- 审批卡片显示 Agent、Run、工具、环境、参数脱敏摘要、风险、申请人、过期时间和策略原因。
- 决策携带 `If-Match` 和 `Idempotency-Key`。
- 重复提交返回第一次有效结果。
- 申请人默认不能审批自己的高风险生产写操作。

异步删除、连通性测试、MCP 发现和扫描不得仅在本地维护 loading 状态；必须根据响应 `status_url` 调用 `getOperation`，处理 `ACCEPTED/RUNNING/SUCCEEDED/FAILED/CANCELLED`。

## 13. 管理页面

Runtime Target、Sandbox Profile、租户、成员、角色、Secret Reference 和审计页面均必须：

- 使用独立管理权限。
- 显示变更影响和引用关系。
- 危险变更要求原因和审计。
- 不展示 Secret 明文和内部集群管理员凭据。

V1 增量页面使用现有资源 OpenAPI：知识库使用 KnowledgeBase/Document/Retrieve 操作，评测使用 EvaluationSet/Case/Run/Feedback 操作，定时任务使用 Schedule 操作，A2A 使用 RemoteAgentBinding/test/invoke 操作。Feature Flag 关闭时不注册路由，不请求后端占位数据。

## 14. API Client 生成规则

- TypeScript Client 从评审后的 OpenAPI 生成。
- 禁止页面手写与 OpenAPI 不一致的 DTO。
- AG-UI/RunEvent Type 从 JSON Schema 生成或由同一源码导出。
- API Error 使用 `code` 判断，禁止匹配 message。
- 每次 CI 比较生成代码，Schema 变化必须显式提交。

## 15. 前端测试

- 使用 Vitest + Vue Test Utils 验证 composable、Pinia Store、路由守卫和组件行为。
- 使用 Playwright 验证登录、资源管理、发布、对话、断线恢复和审批等关键用户流程。
- 路由权限和 Feature Flag。
- ETag 冲突和表单未保存提示。
- Run SSE 断线、重复、乱序、缺口和终态。
- 审批重复、过期和自审批阻断。
- Artifact 未就绪、过期和越权下载。
- 两租户浏览器会话切换不能复用缓存数据。
- Secret、Thinking 和原始 Trace 不进入普通用户 DOM、日志和浏览器持久存储。
