# agent平台需求规格说明书

> 文档版本：V1.4  
> 文档状态：开发输入基线  
> 目标读者：产品经理、架构师、前后端开发、测试、运维、安全人员  
> 文档索引：[Agent平台开发文档索引](./Agent平台开发文档索引.md)  
> 关联文档：[Agent平台架构与流程设计](./Agent平台架构与流程设计.md)  
> 补充文档：[Agent平台核心接口与事件契约](./Agent平台核心接口与事件契约.md)  
> 补充文档：[Agent平台领域模型与状态机](./Agent平台领域模型与状态机.md)  
> 补充文档：[Agent平台非功能与验收基线](./Agent平台非功能与验收基线.md)  
> 追踪矩阵：[Agent平台需求追踪矩阵](./Agent平台需求追踪矩阵.md)
> AI Coding：[Agent平台AI Coding开发总纲](./Agent平台AI%20Coding开发总纲.md)  
> 机器基线：[agent-platform-baseline.yaml](./agent-platform-baseline.yaml)

## 1. 文档目的

本文定义 `agent平台` 的产品目标、范围、角色、功能、页面、业务规则、非功能要求和验收标准，作为后续产品设计、技术设计、任务拆分、开发和测试的共同基线。

平台是使用 Python 从零建设的通用 Agent 平台，不以迁移、兼容或重构任何既有平台为目标，也不继承外部系统的数据库、接口或历史行为。Agent 执行底座采用 AgentScope，并通过平台 Model Gateway 调用大模型；Codex 通过 ACP 接入。所有实现以本目录中已经评审并纳入基线的需求、契约、状态机和 Schema 为唯一依据。

## 2. 产品定位

`agent平台` 是一个支持私有化部署的通用 Agent 开发与运行平台，面向企业内部 Agent 建设者和业务使用者，提供从资源准备、Agent 编排、版本发布、受控运行、会话交互到监控评测的完整闭环。

平台不是某个垂直业务的专用系统，也不以“智能问数”为产品名称。问数、代码分析、风控归因、运营分析等均作为建立在平台上的 Agent 应用。

### 2.1 产品目标

1. 提供可视化 Agent、Prompt、Skill、MCP、模型、知识库和定时任务管理。
2. 使用 AgentScope 承载经 Model Gateway 调用模型的 Agent Loop、工具调用和运行事件。
3. 通过 ACP 标准协议接入 Codex，不解析不稳定的 CLI 展示文本。
4. 通过 Sandbox 隔离 Agent 的文件、进程、网络和资源使用。
5. 通过 Version、Snapshot、Bundle、Deployment 保证发布可复现、可审计、可回滚。
6. 通过 Temporal 统一承载 Run 生命周期、重试、暂停、恢复、审批等待和定时执行。
7. 使用内部统一 `RunEvent`，在前端出口转换为 AG-UI 事件。
8. 支持后续扩展 A2A、其他 Agent Runtime 和第三方模型平台。

### 2.2 成功指标

| 指标 | 首期目标 |
|---|---|
| Agent 创建到首次成功运行 | 30 分钟内完成 |
| 已发布版本可复现率 | 100% |
| Run 事件可回放率 | 100% |
| SSE 断线重连 | 不丢事件，可按序号续传 |
| Run 取消 | AgentScope 与 Codex 均支持尽力取消 |
| Sandbox 隔离 | 不同租户、用户、会话和 Run 不可互读；Session 复用需通过兼容性校验 |
| 关键配置审计 | Agent、Prompt、Skill、MCP、模型、发布均可追踪 |
| 平台扩展新 Runtime | 不修改核心会话和事件模型即可接入 |
| 状态不一致检测 | Run、Workflow、Event、Sandbox 不一致在 5 分钟内被检测 |
| 预算与资源限制 | Token、费用、并发、文件和 Sandbox 超限均可阻断并审计 |

## 3. 固定技术决策

以下决策作为 V1 开发基线，不在普通功能评审中反复变更：

1. Codex 使用 ACP 接入。
2. MVP 默认按 Run 创建 Sandbox；Session Sandbox 仅对通过隔离测试的低风险 Agent 开放，并绑定租户、用户、Session、Deployment 和权限策略 Hash。
3. 所有交互 Run 统一由 Temporal `AgentRunWorkflow` 编排；Token、文本 Delta 和普通进度事件不进入 Workflow History，直接写 Event Service。
4. Skill 兼容 Codex `SKILL.md`，并增加平台扩展描述文件 `manifest.yaml`。
5. 平台内部使用自己的 `RunEvent`；Web、Chat 和 SDK 出口适配为 AG-UI。
6. AgentScope 与 Codex 共享平台定义和 Snapshot，但分别编译运行 Bundle。
7. Runtime 只能读取已发布 Snapshot，不直接读取可编辑草稿。
8. Secret 只保存引用，不进入 Snapshot、Bundle、事件、日志和前端明文响应。
9. 多租户字段、查询隔离、对象路径隔离和审计从第一天进入数据模型；是否以单租户方式部署不影响该约束。
10. 模型调用统一经过 Model Gateway，Runtime 不直接持有长期模型供应商密钥。
11. Codex V1 仅保证 ACP STDIO Transport；WebSocket 在 STDIO 契约和恢复语义稳定后增加。
12. A2A V1 仅支持远端 Agent 引用和客户端调用，不提供平台对外 A2A Server/Gateway。
13. V1 知识库只内置一个默认适配器并提供扩展接口，不建设通用向量数据库管理平台。
14. RunEvent 使用带版本的强类型 Payload；`dict` 仅允许作为未知 Runtime 原始事件的受限存档格式。
15. 首期后端运行基线为 CPython 3.12，使用 `pyproject.toml + uv.lock`、Black、Ruff、类型检查、pytest 和 Alembic。
16. V1 前端固定使用 Vue 3 Composition API、TypeScript、Vite、Vue Router 4、Pinia 和 `@tanstack/vue-query`；API Client 从 OpenAPI 生成，RunEvent Type 从 JSON Schema 或同一源码生成。
17. Model Gateway V1 必须提供 OpenAI、Qwen 和 DeepSeek Provider Adapter；供应商启用、模型清单、Secret 和费用表通过配置与版本化资源管理。
18. AgentScope 首期运行基线为 2.0.x，精确 patch 版本由 `uv.lock` 和运行镜像 Digest 固定。

## 4. 范围

### 4.1 MVP 必须范围

- 用户、租户、角色和基础 RBAC。
- 模型供应商、模型配置、模型连通性测试。
- Model Gateway 的认证、限流、Token/费用统计、超时和错误归一化。
- Prompt 模板、变量、版本、预览和基础测试。
- Skill 包、版本、静态校验、Sandbox 冒烟测试和 Agent 绑定。
- 一个 Streamable HTTP MCP Server 的配置、能力发现、健康检查和工具授权。
- Agent 创建、编辑、复制、发布、回滚、停用和对话。
- AgentScope Runtime。
- Session、Message、Run、RunEvent、Workspace、Artifact。
- Run 级 Sandbox Profile 和实例生命周期。
- Agent 发布和所有 Run 的 Temporal Workflow。
- Run 监控、事件时间线、取消、重试和回放。
- 单人审批的敏感操作确认和审计日志。
- AG-UI 流式对话页面。

MVP 的退出条件是：一个 AgentScope Agent 可以完成创建、发布、运行、工具调用、文件产出、取消、断线重连、事件回放和失败恢复，并通过租户隔离、安全与容量基线测试。

### 4.2 V1 增量范围

- Codex ACP STDIO Runtime、独立 CODEX_HOME、Session 映射、取消和恢复。
- Session Sandbox 受控复用。
- STDIO 与 SSE MCP Transport。
- 一个默认知识库适配器及基础文档导入、检索、删除和权限传播。
- Temporal 定时任务、离线任务和任务日志。
- 多级审批、审批委托和超时策略。
- Agent 基础评测集、人工反馈和版本对比。
- 远端 A2A Agent 引用和客户端调用。

### 4.3 V1 范围外

- 自研大模型训练与推理引擎。
- 完整数据中台、指标平台和业务语义层。
- 复杂低代码流程画布；V1 采用表单式 Agent 编排。
- 商业计费、在线支付和外部客户 SaaS 结算。
- 多地域容灾和跨地域数据复制。
- Agent 自动强化学习和在线自动调参。
- 平台对外提供 A2A Server/Gateway。
- 通用向量数据库管理、Embedding 训练和复杂知识图谱平台。
- 基于浏览器直接执行任意 Shell 或直接暴露宿主容器能力。

## 5. 用户角色与权限

| 角色 | 主要职责 | 核心权限 |
|---|---|---|
| 平台超级管理员 | 平台初始化和全局治理 | 租户、模型、Runtime、Sandbox、Secret、审计全权限 |
| 租户管理员 | 管理本租户资源 | 用户、角色、Agent、模型引用、MCP、发布、监控 |
| Agent 开发者 | 设计和测试 Agent | Prompt、Skill、Agent 草稿、测试、发布申请 |
| 运营人员 | 业务验证和日常运营 | 对话、定时任务、反馈、评测、只读 Trace |
| 审批人员 | 审批高风险动作 | 查看上下文、批准或拒绝工具和 Sandbox 操作 |
| 普通用户 | 使用已发布 Agent | 创建会话、对话、查看本人文件和历史 |
| 审计人员 | 安全与合规检查 | 只读访问配置变更、Run、审批和操作日志 |

权限至少按 `tenant_id + resource_type + resource_id + action` 判断；前端隐藏仅用于改善体验，后端必须独立鉴权。

## 6. 信息架构与导航

页面参考提供的截图，采用顶部资源域导航、卡片式列表、筛选搜索和直接对话入口。

### 6.1 顶部导航

推荐 V1 顶部导航顺序：

```text
Agent | Prompt | Skill | MCP | 模型供应商 | 运行监控 | 平台管理
```

知识库、定时任务和评测在对应 V1 增量能力上线后增加。敏感词、工具、Hook、Secret、Runtime 和 Sandbox 归入“平台管理”或 Agent 编辑页，避免顶部导航过载。未进入当前迭代范围的菜单和表单步骤必须隐藏或明确标记不可用，不能展示无法完成的入口。

### 6.2 品牌规范

- 左上角 Logo 文字固定为 `agent平台`。
- 页面标题、浏览器标题、登录页和空状态不得出现“智能问数”。
- 主色参考截图的蓝色体系，支持浅色和深色主题。
- Agent 是资源名称时可显示为“Agent”或“智能体”，品牌名称始终使用 `agent平台`。

## 7. 页面需求

## 7.1 全局框架

### 页面组成

- 左上角品牌 Logo。
- 顶部资源导航。
- 当前用户头像、主题切换、通知和帮助入口。
- 主内容区域。
- 全局错误提示、确认弹窗和操作反馈。

### 通用列表能力

- 关键词搜索。
- 类型、状态、标签、创建人筛选。
- 分页或无限滚动。
- 卡片和表格视图按页面选择。
- 创建、复制、编辑、查看详情、启停、删除。
- 被 Agent 引用的资源删除前必须显示引用关系和风险提示。

### 通用交互规则

- 页面必须区分加载中、空数据、权限不足、服务异常和部分数据不可用状态。
- 创建、发布、删除、审批和重试操作应防止重复提交，并展示可查询的操作状态。
- 编辑页面使用资源版本号或 ETag；检测到并发修改时禁止静默覆盖。
- 离开存在未保存内容的页面前给出提示；自动保存时展示最近保存状态和失败原因。
- 长时间操作不得依赖浏览器连接完成，页面通过 operation/run/workflow 标识恢复进度。
- 所有危险操作显示实际资源、环境、参数摘要和影响范围，不能只展示通用确认文案。
- 关键页面满足键盘操作、可读错误提示和基础无障碍要求。

## 7.2 Agent 列表页

### 页面结构

1. 顶部说明区：介绍 Agent 由模型、Prompt、Skill、MCP、知识库、Sandbox 和 Runtime 组成。
2. 类型切换：`全部 / 自定义 / A2A`。
3. 标签筛选和 Agent 名称搜索。
4. 创建卡片：
   - 自定义 Agent。
   - A2A WellKnown Agent。
   - A2A 注册中心 Agent 在 V1 增量能力启用前不展示。
5. Agent 卡片网格。

### Agent 卡片字段

- 图标、名称、状态。
- 类型：自定义或 A2A。
- 描述。
- 创建人。
- 标签。
- 当前已发布版本。
- Runtime 类型：AgentScope 或 Codex。
- 最近更新时间。
- `去对话` 按钮。
- 更多操作：编辑、复制、版本、发布、停用、删除。

### 业务规则

- 只有存在已发布版本且 Runtime 健康时才允许普通用户对话。
- 草稿变更不影响正在运行和已经发布的版本。
- 删除 Agent 前检查 Session、定时任务、A2A、MCP 服务入口和子 Agent 引用。

## 7.3 Agent 创建与编辑页

采用六步表单，形成稳定、可逐步校验的 Agent 配置心智模型。

### 第一步：基本信息

- 名称、唯一编码、描述、图标、标签。
- Agent 类型：自定义、A2A 引用。
- 可见范围：私有、租户。跨租户公开市场不在 V1 范围内。
- 默认语言。
- 负责人和协作者。

### 第二步：模型与 Prompt

- Runtime 模式：AgentScope 或 Codex。
- 模型供应商与模型配置。
- 温度、最大 Token、超时、Reasoning Effort。
- 系统 Prompt 模板和版本。
- Prompt 变量默认值。
- 模型 fallback；V1 可限定最多两级。

### 第三步：工具与能力

- Skill 绑定和版本策略。
- 内置工具和自定义工具。
- 敏感词策略。
- Human-in-the-loop 审批规则。
- 工具调用最大次数和并发数。
- 危险能力声明：Shell、写文件、联网、调用生产 API。

### 第四步：知识库与 MCP

- 知识库绑定和检索参数。
- MCP Server 绑定。
- MCP 工具局部授权。
- 子 Agent 绑定。
- A2A 远端 Agent 绑定。

### 第五步：高级设置

- Memory 开关和策略。
- Plan 开关。
- Sandbox Profile。
- Sandbox 粒度：MVP 默认 Run；满足安全与兼容条件时可选择 Session。
- Workspace 配额和保留期。
- 网络访问策略。
- Artifact 导出策略。
- Token、费用和时间预算。

### 第六步：Runtime 与发布

- Runtime Target。
- AgentScope Worker Pool 或 Codex ACP Endpoint。
- 工作目录策略。
- Secret Reference。
- Runtime 环境变量引用。
- 发布说明。
- Dry-run、Bundle Diff、冒烟测试。
- 保存草稿或发布新版本。

发布前页面必须展示本次 Snapshot 与当前 Deployment 的结构化 Diff，包括资源版本、权限、网络、Sandbox、模型和 Secret Reference 变化；Secret 只显示引用，不显示明文。

## 7.4 对话与调试页

页面至少包含：

- Session 列表、新建、重命名、删除和归档。
- 消息输入、文件上传、发送、停止。
- 文本流式输出。
- Thinking 折叠区。
- Plan 展示。
- Tool/MCP 调用时间线。
- 审批卡片。
- Artifact 文件卡片和下载。
- Run 状态、耗时、Token 和费用。
- Prompt、Snapshot、Runtime、Sandbox 只读信息。
- 重新运行、从当前消息分支、反馈。

SSE 断开重连后从最后一个 `sequence_no` 继续，不允许重复显示已经确认的事件。

## 7.5 Prompt 管理页

- 卡片或表格列表。
- 新建、编辑、复制、启停。
- Markdown 编辑器和变量声明。
- Prompt 版本、Diff、发布和回滚。
- 测试输入和渲染预览。
- 引用该 Prompt 的 Agent 列表。
- 禁止删除被已发布 Snapshot 引用的版本。

## 7.6 Skill 管理页

- 新建空 Skill、本地目录导入、ZIP 导入和 Git 导入。
- 文件树和多文件编辑器。
- `SKILL.md` 必填。
- `manifest.yaml` 必填。
- 版本、Diff、发布、回滚和导出。
- 静态校验、依赖检查和 Sandbox 测试。
- Agent 引用关系。

标准目录：

```text
skills/<skill-name>/
├── SKILL.md
├── manifest.yaml
├── scripts/
├── references/
├── assets/
└── templates/
```

`manifest.yaml` 至少描述名称、版本、入口、依赖、权限、输入输出 Schema、网络要求、Sandbox 要求和适用 Runtime。

## 7.7 MCP 管理页

- 支持 STDIO、SSE、Streamable HTTP。
- Server 名称、描述、协议和 Endpoint。
- 命令与参数；仅 STDIO。
- Header、环境变量和 Secret Reference。
- 连接测试、能力发现和工具列表。
- 工具级启停与 Agent 授权。
- 健康状态、最近探测时间和错误。
- MCP 配置发布时编译进对应 Runtime Bundle。

## 7.8 模型供应商页

- 供应商类型、Base URL、认证方式和状态。
- 模型列表和能力标签：流式、工具调用、视觉、结构化输出、推理等级。
- 默认参数、限流、并发和超时。
- 连通性测试。
- Secret 只显示引用和掩码。
- 统计调用量、Token、错误率和平均延迟。

## 7.9 知识库页

- 本地或第三方知识库适配器。
- 文档上传、同步、解析、分块、向量化和状态。
- 检索测试、TopK、阈值和重排。
- Agent 引用关系。
- 数据权限必须在检索调用时二次校验，不依赖 Agent 静态绑定代替用户权限。

## 7.10 定时任务页

- 基于 Temporal Schedule 创建任务。
- 选择已发布 Agent 版本。
- Cron、时区、输入参数和模型覆盖。
- 并发策略：跳过、排队、替换。
- 超时、重试、通知和停启。
- 查看每次执行对应的 Workflow、Run、事件和产物。

## 7.11 平台管理页

至少包含：

- Runtime Target 管理。
- AgentScope Worker 管理。
- Codex ACP Endpoint 管理。
- Sandbox Profile 和 Sandbox 实例。
- Run 监控和 Run 详情。
- Temporal Workflow 监控入口。
- Session 和 Workspace。
- Artifact 管理。
- 审批中心。
- Secret 管理。
- 用户、角色、租户。
- 审计日志。
- 系统配置和告警。

## 8. 核心功能需求

## 8.1 Agent 生命周期

| 编号 | 需求 |
|---|---|
| FR-AGT-001 | 支持 Agent 草稿创建、编辑、复制和删除。 |
| FR-AGT-002 | 草稿发布时生成不可变 Version 和 Snapshot。 |
| FR-AGT-003 | Snapshot 必须冻结 Prompt、Skill、MCP、模型、子 Agent、Sandbox 和 Runtime 配置版本。 |
| FR-AGT-004 | 支持发布 Dry-run、校验、Bundle Diff、冒烟测试和原子激活。 |
| FR-AGT-005 | 支持历史版本查看和回滚；回滚产生新的发布记录。 |
| FR-AGT-006 | 已发布 Snapshot 不允许修改。 |

## 8.2 Runtime

| 编号 | 需求 |
|---|---|
| FR-RT-001 | 平台提供统一 RuntimeAdapter 接口。 |
| FR-RT-002 | AgentScope Runtime 通过 Model Gateway 对接模型供应商。 |
| FR-RT-003 | Codex Runtime 通过 ACP 接入；V1 只实现 STDIO Transport，WebSocket 必须通过后续版本 ADR 和兼容性评审后增加。 |
| FR-RT-004 | Runtime 原始事件转换为平台 RunEvent。 |
| FR-RT-005 | 支持运行、取消、超时、会话复用和资源释放。 |
| FR-RT-006 | 新增 Runtime 不得修改 Session、Message 和 RunEvent 核心模型。 |

## 8.3 Session、Run 与事件

| 编号 | 需求 |
|---|---|
| FR-RUN-001 | 用户请求先持久化 Session、Message 和 Run，再启动 Runtime。 |
| FR-RUN-002 | Run 状态至少支持 CREATED、QUEUED、PREPARING、RUNNING、WAITING_APPROVAL、CANCELLING、SUCCEEDED、FAILED、CANCELLED、TIMEOUT。 |
| FR-RUN-003 | 每个 RunEvent 具有单调递增的 sequence_no。 |
| FR-RUN-004 | 支持实时订阅、历史回放和断点续传。 |
| FR-RUN-005 | 平台 Session 与 Runtime Session 分开管理。 |
| FR-RUN-006 | Snapshot 或 Runtime Target 变化后不得复用不兼容的 Runtime Session。 |

## 8.4 Sandbox 与 Workspace

| 编号 | 需求 |
|---|---|
| FR-SBX-001 | MVP 默认一个 Run 一个 Sandbox；创建失败时 Run 不得进入 RUNNING。 |
| FR-SBX-002 | Session Sandbox 仅对显式授权的低风险 Agent 开放，并在 Deployment、权限、用户或 Sandbox Policy 变化后重建。 |
| FR-SBX-003 | 提供 CPU、内存、磁盘、PID、超时和网络限制。 |
| FR-SBX-004 | Workspace 按租户、用户、Session、Run 分层隔离。 |
| FR-SBX-005 | Secret 仅在启动时临时注入 Sandbox。 |
| FR-SBX-006 | Sandbox 不直接访问平台主数据库。 |
| FR-SBX-007 | Run 完成后按策略归档 Artifact，并按 TTL 回收 Sandbox。 |

## 8.5 Temporal 运行编排

| 编号 | 需求 |
|---|---|
| FR-TMP-001 | 所有 Run 和发布使用 Temporal Workflow；定时任务、离线任务和审批等待在对应范围启用。 |
| FR-TMP-002 | Activity 必须幂等并配置超时和重试策略。 |
| FR-TMP-003 | Workflow ID 与平台业务 ID 可关联查询。 |
| FR-TMP-004 | 支持 Signal 传递取消、审批结果和外部任务结果。 |
| FR-TMP-005 | 服务重启后 Workflow 能继续执行，不依赖单机内存状态。 |
| FR-TMP-006 | 文本 Delta、Token 和普通进度事件不得逐条写入 Temporal Workflow History。 |
| FR-TMP-007 | Workflow 和 Activity 代码升级必须通过 Replay Test，并为不兼容变更提供版本标记。 |

## 8.6 Model Gateway 与预算

| 编号 | 需求 |
|---|---|
| FR-MDL-001 | Runtime 通过 Model Gateway 调用模型，供应商长期密钥不直接注入普通 Runtime。 |
| FR-MDL-002 | Gateway 统一处理供应商认证、超时、限流、错误归一化和可观测性。 |
| FR-MDL-003 | 记录输入、输出、缓存和推理 Token；不支持精确 Token 的供应商必须标记为估算。 |
| FR-MDL-004 | 支持租户、Agent、用户和 Run 级并发、Token 与费用预算。 |
| FR-MDL-005 | 模型 fallback 只能在明确的可重试错误上执行，禁止在未知提交状态下盲目重复用户请求。 |
| FR-MDL-006 | 模型配置发布时冻结供应商、模型标识、参数、能力和 Secret Reference，不冻结 Secret 明文。 |
| FR-MDL-007 | Model Gateway 提供 OpenAI、Qwen、DeepSeek Adapter；可复用兼容协议，但能力、错误、限流和费用规则分别配置和测试。 |

## 8.7 审批与高风险操作

| 编号 | 需求 |
|---|---|
| FR-APR-001 | Policy Service 在工具执行前根据 Agent 权限、用户权限、工具风险和参数摘要决策。 |
| FR-APR-002 | Approval 必须绑定 tenant、run、tool、参数摘要、申请人、有效期和单次使用标识。 |
| FR-APR-003 | 工具参数、Deployment 或权限变化后，原 Approval 自动失效。 |
| FR-APR-004 | 审批支持批准、拒绝、过期、取消四种结果，并配置 Run 后续行为。 |
| FR-APR-005 | 默认禁止申请人审批自己的高风险生产写操作。 |
| FR-APR-006 | 审批等待期间不得无限占用 Sandbox；超过保留时间后应挂起或重建执行环境。 |

## 8.8 Artifact、Workspace 与数据生命周期

| 编号 | 需求 |
|---|---|
| FR-DAT-001 | Workspace URI 必须包含 tenant、user、session 和 run 隔离信息，物理路径不直接暴露。 |
| FR-DAT-002 | Artifact 创建前校验路径、大小、MIME、扩展名、软链接和恶意文件。 |
| FR-DAT-003 | Artifact 元数据记录来源 Run、创建者、Hash、大小、内容类型、状态和保留期。 |
| FR-DAT-004 | 下载 URL 短期有效、单资源授权，并在生成时重新校验访问权限。 |
| FR-DAT-005 | Session、Message、RunEvent、Workspace、Artifact 和审计日志分别配置保留策略。 |
| FR-DAT-006 | 删除业务资源采用可审计流程；已发布 Snapshot 和审计记录不得物理修改。 |

## 8.9 幂等、并发和重试

| 编号 | 需求 |
|---|---|
| FR-CON-001 | 创建 Run、发布、审批决策和危险工具执行支持 Idempotency-Key。 |
| FR-CON-002 | Agent、Prompt、Skill 等编辑接口使用版本号或 ETag 防止覆盖并发修改。 |
| FR-CON-003 | `retry run` 必须创建新 Run，并记录 `retry_of_run_id`，不得重置原 Run。 |
| FR-CON-004 | 同一 Session 默认只允许一个主 Run；分支运行创建新的逻辑分支标识。 |
| FR-CON-005 | Runtime 写事件和更新状态必须携带 execution fencing token，拒绝旧 Worker 写入。 |
| FR-CON-006 | 所有重试策略明确最大次数、退避、可重试错误和副作用幂等条件。 |

## 8.10 审计与治理

| 编号 | 需求 |
|---|---|
| FR-AUD-001 | 发布、回滚、删除、审批、Secret 使用、权限变更和危险工具调用必须写审计日志。 |
| FR-AUD-002 | 审计记录包含操作者、租户、资源、动作、结果、时间、来源和变更摘要。 |
| FR-AUD-003 | 审计日志不可由普通业务管理员修改或删除。 |
| FR-AUD-004 | 配置 Diff 必须脱敏，不保存 Secret 明文和完整敏感 Prompt。 |
| FR-AUD-005 | 支持按租户、操作者、资源、Run、时间和动作检索审计记录。 |

## 8.11 知识库 V1 限定能力

| 编号 | 需求 |
|---|---|
| FR-KNW-001 | V1 只保证一个默认知识库适配器，并保持检索接口可替换。 |
| FR-KNW-002 | 文档、Chunk 和向量记录保留来源、版本、权限主体和索引版本。 |
| FR-KNW-003 | 文档更新或删除后，旧 Chunk 和向量必须最终失效并可对账。 |
| FR-KNW-004 | 检索时重新校验用户数据权限，不以 Agent 静态绑定代替用户权限。 |
| FR-KNW-005 | Prompt 中引用检索内容时明确标记为不可信上下文，不赋予工具权限。 |

## 8.12 基础评测与反馈

| 编号 | 需求 |
|---|---|
| FR-EVL-001 | 支持为 Agent Version 维护最小评测数据集和期望检查项。 |
| FR-EVL-002 | 发布前可选执行离线回归，结果绑定 Snapshot、模型和评测集版本。 |
| FR-EVL-003 | 用户可对 Run 提交赞、踩、标签和文字反馈。 |
| FR-EVL-004 | LLM-as-Judge 结果必须标记评测模型和 Prompt 版本，不能作为唯一发布门禁。 |
| FR-EVL-005 | 评测样本和生产消息之间必须进行权限与敏感数据隔离。 |

## 8.13 身份、租户与权限

| 编号 | 需求 |
|---|---|
| FR-IAM-001 | 支持 OIDC/OAuth2 登录，并以外部 subject 映射平台用户。 |
| FR-IAM-002 | 支持租户成员、角色、资源范围和动作权限绑定。 |
| FR-IAM-003 | 所有业务查询、缓存、对象、Workflow 和 Sandbox 强制租户隔离。 |
| FR-IAM-004 | 支持服务身份和短期工作负载凭据，禁止内部服务共享管理员 Token。 |
| FR-IAM-005 | 超级管理员跨租户操作必须显式选择租户、填写原因并记录审计。 |
| FR-IAM-006 | 用户、成员或角色停用后，新请求立即失效，存量 Run 按安全策略取消或限制。 |

## 8.14 Prompt、Skill 与 MCP 资源

| 编号 | 需求 |
|---|---|
| FR-RES-001 | Prompt 支持变量 Schema、版本、预览、Diff、发布、回滚和引用查询。 |
| FR-RES-002 | Prompt 编译定义来源优先级、Token 分配、截断和敏感数据处理。 |
| FR-RES-003 | Skill 支持 SKILL.md、manifest、版本、依赖锁定、权限声明、安全扫描和 Sandbox 测试。 |
| FR-RES-004 | MCP 支持能力发现、工具 Schema Hash、健康检查、工具级授权和 Secret Reference。 |
| FR-RES-005 | STDIO MCP 只能在 Sandbox 中运行；API 进程不得直接执行其命令。 |
| FR-RES-006 | Agent 发布时把 Prompt、Skill、MCP 解析到具体不可变版本。 |
| FR-RES-007 | 被已发布 Snapshot 引用的版本不能物理删除。 |

## 8.15 定时任务

| 编号 | 需求 |
|---|---|
| FR-SCH-001 | Schedule 只能引用已发布 Snapshot/Deployment 和经过校验的输入 Schema。 |
| FR-SCH-002 | Cron 必须包含时区，并定义 DST、misfire 和补跑策略。 |
| FR-SCH-003 | 并发策略支持 SKIP、QUEUE、REPLACE，REPLACE 必须执行受控取消。 |
| FR-SCH-004 | 每次触发创建独立 Run，并记录 schedule_id、scheduled_at 和实际开始时间。 |
| FR-SCH-005 | Schedule 停用不取消已开始的 Run，除非用户显式选择取消。 |
| FR-SCH-006 | 定时任务输入中的 Secret 只使用引用，任务日志和事件不得包含明文。 |

## 9. 非功能需求

### 9.1 安全

- 所有请求进行身份、租户和资源权限校验。
- Secret 使用 Vault、KMS 或等价密钥系统存储。
- Sandbox 默认禁止公网访问，按白名单开放。
- 禁止路径穿越、Zip Slip、软链接逃逸和绝对路径写入。
- Shell、生产写操作、外发数据等高风险工具必须支持审批。
- Prompt Injection 检测不能代替权限控制；工具权限由后端独立判断。
- 所有发布、审批、Secret 使用和危险工具调用写审计日志。

### 9.2 性能

- 普通管理 API P95 小于 500ms，不含第三方依赖调用。
- Run 创建到首个平台事件 P95 小于 3 秒，不含模型首 Token 延迟。
- Event Service 支持批量写入和文本 Delta 合并；单条事件持久化失败不得静默丢失关键事件。
- MVP 基准容量为单租户 100 个并发 AgentScope Run、1,000 个在线 SSE 连接；具体工作负载见补充验收基线。
- Codex 并发容量单独核算，不与 AgentScope 共用同一 Worker Pool 指标。
- 列表 API 默认分页，单页上限 200；上传、Artifact、Prompt、事件 Payload 均配置大小限制。

### 9.3 可用性与恢复

- 平台 API、Runtime Worker、Temporal Worker 可独立扩容。
- API 或浏览器重启不影响已经提交的长任务。
- Run、Workflow、Runtime Session、Sandbox Instance 必须有持久化映射。
- 发布失败继续使用旧 Deployment。
- 事件重复投递时消费者必须幂等。
- 生产基线目标：控制面月可用性不低于 99.9%，运行编排月可用性不低于 99.5%。
- 单地域部署的数据库 RPO 不高于 5 分钟、RTO 不高于 60 分钟；必须定期执行恢复演练。
- 必须提供卡住的 Run、Workflow、Sandbox、Deployment 和 Outbox 的对账与修复任务。

### 9.4 可观测性

- 全链路使用 trace_id、session_id、run_id、workflow_id。
- 记录模型延迟、Token、费用、工具延迟、Sandbox 资源和失败原因。
- 支持按 Agent、版本、Runtime、模型、租户查询成功率。
- 日志禁止打印 API Key、Token、完整敏感 Prompt 和用户私密文件。
- 指标标签禁止直接使用高基数 Prompt、文件名或完整用户输入。
- 每个生产告警必须关联责任模块、严重等级和处置 Runbook。

### 9.5 数据保留与合规

- RunEvent、Message、Artifact、Workspace、模型调用记录和审计日志分别配置保留期限。
- 用户删除请求必须区分业务软删除、延迟物理删除和依法必须保留的审计记录。
- 数据静态存储和传输必须加密；对象存储、备份和日志遵循相同租户隔离要求。
- 敏感数据进入模型前按策略脱敏，并记录所用策略版本。
- 导出、下载、跨租户共享和外部 MCP 调用必须进入审计范围。

### 9.6 兼容性

- API、RunSpec、RunEvent、Bundle Manifest 和 Skill Manifest 均包含显式版本。
- 新版本至少兼容当前生产版本和前一个已发布版本的 Worker/Bundle。
- 数据库迁移使用向前兼容的 expand/migrate/contract 流程，不在同一发布中直接删除仍被旧代码读取的字段。
- Runtime Adapter 和 AG-UI Adapter 使用契约测试验证兼容性。

### 9.7 运维与部署

- API、Temporal Worker、Runtime Worker、Event Worker 和 Sandbox Manager 独立健康检查、扩缩容和发布。
- 数据库迁移、Bundle Compiler 和 Runtime Worker 版本必须可以追踪。
- 生产发布支持灰度、回滚和 Feature Flag；安全策略默认失败关闭。
- Sandbox 镜像必须固定 Digest，并记录 SBOM、漏洞扫描和签名结果。
- 禁止 API 服务直接持有宿主 Docker Socket 或 Kubernetes 集群管理员权限。

## 10. 产品验收场景

### 10.1 AgentScope Model Gateway 场景

1. 管理员创建模型供应商，经 Model Gateway 完成连通性测试。
2. 开发者创建 Prompt、Skill 和 MCP。
3. 创建 Agent，Runtime 选择 AgentScope。
4. 发布生成 Snapshot、AgentScope Bundle 和 Deployment。
5. 用户创建会话并触发工具调用。
6. 前端显示文本、工具、文件和终态事件。
7. Run 详情可以完整回放。

### 10.2 Codex ACP 场景

1. 管理员配置 Codex ACP Runtime Target。
2. 创建 Codex Agent 并绑定 Skill、MCP、模型和 Sandbox。
3. 发布生成独立 `.codex`、`AGENTS.md`、`config.toml`、`agents/` 和 `skills/`。
4. Codex 在 Session Sandbox 中启动并建立 ACP Session。
5. 用户对话可以执行文件和工具操作。
6. 取消 Run 后平台发送 ACP cancel 并终止必要进程。
7. 不同 Session 的 Workspace、Skill 和 Prompt 不发生串用。

### 10.3 长任务恢复场景

1. Agent 触发超过五分钟的离线任务。
2. Temporal Workflow 记录 Activity 状态。
3. API 服务重启。
4. Workflow 和 Run 继续执行。
5. 用户重新进入页面后从事件序号恢复进度。
6. 最终 Artifact 可下载并具有权限和有效期。

## 11. 实施优先级

| 阶段 | 主要交付 |
|---|---|
| P0 开发准入 | ADR、领域模型、OpenAPI、RunEvent Schema、状态机、权限模型、威胁模型、容量模型 |
| P1 MVP 核心 | 多租户数据基线、登录、Model Gateway、Prompt、Agent、AgentScope、Run Sandbox、RunEvent、SSE |
| P2 发布闭环 | Skill、MCP、Snapshot、Bundle、Deployment、回滚、Artifact、基础审批、审计 |
| P3 可靠性 | 在所有 Run 和发布已使用 Temporal 最小工作流的基础上，补齐复杂重试、恢复、对账、配额、压测、告警和生产 Sandbox |
| P4 Codex | ACP STDIO、Codex Bundle、Session 映射、取消、恢复和独立 Worker Pool |
| P5 V1 增量 | Session Sandbox、知识库、定时任务、基础评测、A2A 客户端、多级审批 |

## 12. 已冻结的歧义决策

1. **多租户**：数据模型、查询、缓存、对象存储、Workflow 和 Sandbox 从第一天具备租户隔离；部署形态可先单租户。
2. **Temporal**：所有 Run 进入 AgentRunWorkflow；流式 Token/Delta 不进入 Workflow History。
3. **Codex Transport**：V1 只承诺 ACP STDIO，WebSocket 后续增加。
4. **Sandbox 默认粒度**：MVP 默认 Run；Session Sandbox 为 V1 增量能力。
5. **敏感任务判定**：由平台最大策略、租户策略、Agent 配置、用户权限和工具风险取最严格结果。
6. **知识库**：V1 只提供一个默认适配器和统一检索接口。
7. **Secret 后端**：接口必须支持 Vault/KMS；开发环境可使用本地实现，生产不得使用明文数据库字段。
8. **Artifact 保留**：默认 30 天，租户可在平台上下限内配置；审计记录不随 Artifact 删除。
9. **A2A**：V1 只提供远端引用和客户端调用，不提供服务端 Gateway。
10. **Run 重试**：创建新 Run，继承原 Snapshot 和输入，除非用户明确选择当前 Deployment；原 Run 保持不可变。
11. **Artifact 失败**：若 Artifact 是声明的必需输出，则 Run 失败；非必需 Artifact 进入 `SUCCEEDED_WITH_WARNINGS` 结果语义，但 Run 状态仍为 SUCCEEDED 并附告警事件。
12. **事实来源**：AgentRun 表是可查询业务状态事实，Temporal History 是编排事实，RunEvent 是用户可见历史；由对账任务修复不一致。
13. **前端框架**：冻结为 Vue 3 + TypeScript + Vite；路由使用 Vue Router 4，客户端状态使用 Pinia，服务端状态使用 `@tanstack/vue-query`。框架变更必须更新 ADR 和冻结基线，不得由单个页面任务隐式替换。

## 13. 全局业务规则

- 所有创建类 API 使用客户端请求 ID 或 `Idempotency-Key`。
- 所有资源查询在服务端重新执行租户和资源权限校验。
- 已发布 Snapshot、Bundle 和历史 Run 不允许原地修改。
- 删除被 Snapshot、Schedule、Agent 或 Session 引用的资源时必须拒绝或进入受控停用流程。
- 所有时间使用 UTC 存储，API 使用 ISO 8601；用户界面按用户时区展示。
- 所有金额使用明确币种和 Decimal，禁止二进制浮点作为账务事实。
- 所有列表接口使用稳定排序和游标或页码分页。
- 所有异步操作返回可查询的 operation/workflow/run 标识，不依赖单次 HTTP 连接完成。
- 用户取消只表示平台开始取消流程；只有 Run 进入 CANCELLED 后才表示取消完成。
- Runtime、Worker 或 Sandbox 的旧执行实例不得覆盖新实例状态，使用 fencing token 防止脑裂写入。

## 14. 开发准入条件

进入功能开发前必须完成并评审：

1. 目标迭代的功能清单、非目标和验收场景。
2. 涉及实体的数据模型、约束、索引和迁移方案。
3. 涉及 API、RunSpec、RunEvent 或内部消息的契约。
4. 状态变化、幂等键、重试、取消和补偿语义。
5. 租户、权限、Secret、网络、文件和审计影响。
6. 性能预算、资源上限、监控指标和故障恢复路径。
7. 单元、契约、集成、安全和容量测试范围。

任何一项未明确时，可以实现隔离的技术 Spike，但不得作为生产功能合并。

## 15. AI Coding 输入规则

每个 AI Coding 任务必须引用一个明确的文档基线版本，并满足以下规则：

1. 不允许 AI 根据其他项目、框架示例或历史代码推断平台业务规则。
2. 同一任务只能有一个权威 API Schema、一个权威领域状态机和一个权威数据字段定义。
3. 自然语言示例与机器可读 Schema 冲突时停止编码，由负责人修正文档；不得自行选择。
4. 任务必须声明允许修改的目录、禁止修改的目录、依赖任务和验收命令。
5. 未进入当前迭代的页面、接口和字段不得以占位实现进入生产代码。
6. 技术 Spike 必须位于独立模块或分支，不得形成绕过 Temporal、Event Service、Policy Service 或 Sandbox 的生产路径。
7. 所有新增公共契约必须同时提供 Schema、Golden File、兼容性说明和至少一个失败用例。
8. 代码生成前使用[Agent平台AI Coding开发总纲](./Agent平台AI%20Coding开发总纲.md)中的任务包模板完成输入检查。
