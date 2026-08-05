# Agent 平台非功能与验收基线

> 文档版本：V1.2  
> 文档状态：开发输入基线  
> 关联需求：[Agent平台需求规格说明书](./Agent平台需求规格说明书.md)  
> 关联架构：[Agent平台架构与流程设计](./Agent平台架构与流程设计.md)

## 1. 目的

本文把安全、性能、容量、可用性、灾备、可观测性和测试要求转换为可执行验收标准。所有数值是 MVP 基线，生产上线前应根据真实模型、Sandbox 和硬件压测修订。

## 2. 标准工作负载

没有工作负载定义的性能数字不作为验收依据。压测至少包含：

### 2.1 AgentScope 文本 Run

```text
并发 Run：100/tenant
平均持续时间：30 秒
P95 持续时间：120 秒
平均模型输出：2,000 tokens
平均 RunEvent：100 条（Delta 合并后）
工具调用：每 Run 0～3 次
Artifact：20% Run 生成，平均 2MB
```

### 2.2 AgentScope 工具 Run

```text
并发 Run：50/tenant
平均持续时间：120 秒
每 Run 工具调用：5 次
其中外部 HTTP/MCP：3 次
文件读写：20MB
审批：10% Run 进入一次 WAITING_APPROVAL
```

### 2.3 Codex Run

```text
独立 Worker Pool
初始并发目标：20/cluster
平均持续时间：10 分钟
P95 持续时间：30 分钟
工作区写入：平均 50MB，最大按 Sandbox Profile 限制
```

Codex 容量不能用 AgentScope 指标代替，必须单独测量进程、内存、磁盘和取消耗时。

### 2.4 SSE

```text
在线连接：1,000/cluster
平均事件速率：2 events/s/connection
突发：10 events/s/connection，持续 30 秒
断线重连比例：每分钟 5%
```

## 3. SLI 与 SLO

### 3.1 控制面

| SLI | MVP SLO |
|---|---:|
| 非第三方依赖管理 API P95 | < 500ms |
| 非第三方依赖管理 API P99 | < 1.5s |
| 月可用性 | >= 99.9% |
| 发布请求成功进入 Workflow P95 | < 2s |
| 资源列表错误率 | < 0.5% |

### 3.2 运行面

| SLI | MVP SLO |
|---|---:|
| Run 创建至 `run_queued` P95 | < 3s |
| Event Store 单批写入 P95 | < 100ms |
| SSE 新事件可见延迟 P95 | < 500ms |
| SSE 断线续传正确率 | 100%，无永久丢失和重复展示 |
| Cancel 请求至进入 CANCELLING P95 | < 2s |
| AgentScope Cancel 完成 P95 | < 10s |
| Sandbox 创建 P95 | < 10s，按部署环境复核 |
| 运行编排月可用性 | >= 99.5% |

模型首 Token 延迟单独统计，不混入平台排队和事件传输延迟。

### 3.3 数据正确性

- 已发布 Snapshot 可复现率 100%。
- RunEvent 回放完整率 100%。
- 同一 Run sequence_no 单调且无重复率 100%。
- 跨租户 Session、Workspace、Artifact 和 Event 越权测试阻断率 100%。
- 已确认终态冲突不得静默覆盖。
- Outbox、Workflow 和 Run 的不一致在 5 分钟内被检测。

## 4. 容量与资源准入

Admission Controller 至少支持：

- 集群、租户、用户、Agent、Runtime 类型并发限制。
- Model Gateway QPS、Token 和费用预算。
- Sandbox CPU、内存、磁盘、PID 和并发额度。
- Workspace/Artifact 存储配额。
- Temporal Task Queue 和 Runtime Worker 排队阈值。
- Event Store 和 SSE 背压阈值。

超过硬限制返回 429 或进入有上限队列，不能无限堆积。排队请求必须有最大等待时间并支持取消。

容量报告必须记录：

```text
硬件与集群配置
数据库/Redis/Temporal/Object Storage 版本
Runtime、模型和 Sandbox 类型
测试数据和请求分布
吞吐、延迟、错误、资源峰值
首先达到的瓶颈和安全余量
```

生产容量目标至少保留 30% 安全余量。

## 5. Event Store 验收

- text_delta 以 50～200ms 窗口合并，验证合并不改变消息文本。
- 支持至少 2,000 events/s 的持续写入基准，最终数值按压测调整。
- RunEvent 表具有分区、归档和索引方案。
- 单事件默认最大 256KB，大数据转换为 Artifact。
- Event Service 重启后 Runtime 可以重试未确认批次且不重复展示。
- Redis 通知丢失时，客户端通过 PostgreSQL 序号补齐。
- 慢 SSE 客户端不会拖慢 Event Writer 或其他连接。
- 终态事件优先持久化且不能被普通 Delta 长时间阻塞。

## 6. Temporal 验收

- 每个 Run 有确定、可重复启动的 Workflow ID。
- Token/Delta 不进入 Workflow History。
- 长 Activity 周期性 heartbeat，并响应取消。
- Worker 在 RUNNING、WAITING_APPROVAL、CANCELLING 阶段重启后流程可恢复。
- Workflow Replay Test 覆盖当前版本和前一个生产版本的历史样本。
- History 达到阈值前 Continue-As-New。
- Activity 重试不会重复发布 Deployment、执行写工具或提交未知状态 Prompt。
- Temporal 暂时不可用时 Run 保持可对账状态，恢复后由 Outbox 启动。

## 7. Sandbox 安全基线

### 7.1 生产硬要求

- 非 root 用户。
- 禁止 privileged、Host Network、Host PID、Host IPC 和任意 HostPath。
- 只读根文件系统，按需挂载受控可写目录。
- Drop Linux capabilities，配置 seccomp 和 AppArmor/SELinux。
- CPU、内存、磁盘、PID、执行时间和网络配额。
- 独立 ServiceAccount，不具备平台数据库和控制面权限。
- 镜像固定 Digest，具有签名、SBOM 和漏洞扫描记录。
- Egress 通过代理或网络策略执行域名/IP 白名单，防 DNS rebinding 和重定向绕过。
- Secret 使用短期、最小权限、可撤销能力；Run 结束立即撤销。
- Sandbox Manager 不向 API 暴露 Docker Socket 或集群管理员能力。

### 7.2 安全测试

- 路径穿越、Zip Slip、软链接与 TOCTOU 逃逸。
- 容器逃逸常见路径、特权提升和敏感挂载探测。
- fork bomb、磁盘填满、内存耗尽和大量文件。
- SSRF、DNS rebinding、HTTP 重定向到私网和云元数据地址。
- 子进程读取环境变量、`/proc`、临时文件和其他 Run 文件。
- 后台进程残留及 Session Sandbox 清理失败。
- Prompt Injection 诱导调用未授权工具和外发数据。

任一租户隔离、Secret 泄漏或 Sandbox 逃逸测试失败均为上线阻断项。

## 8. 供应链安全

Skill、MCP、Bundle、容器镜像和 Python 依赖必须记录：

- 来源 URI、提交或内容 Hash。
- 构建器和 Compiler Version。
- 锁定依赖和受控镜像源。
- SBOM、许可证、漏洞和恶意代码扫描。
- 签名与验证结果。
- 发布人、评审人和发布时间。

严重或已知可利用漏洞默认阻断发布；例外必须有风险接受人、期限和补偿控制。

## 9. 身份、权限和租户隔离验收

- 普通用户不能通过请求体、Header 或 URL 切换 tenant_id。
- 所有 Repository 查询具有强制 Tenant Scope；原生 SQL 通过专项审查。
- Redis Key、对象存储路径、Workflow ID、Runtime Session 和 Sandbox 名称隔离。
- MCP 和知识库调用同时校验 Agent 授权与用户数据权限。
- Tool Gateway 不信任模型输出的“已授权”描述。
- 超级管理员跨租户操作有显式入口、二次确认、原因和审计。
- 审批人不能默认审批自己发起的生产写操作。
- Artifact 下载链接不能访问其他用户、Run 或过期对象。

至少构造两个租户、两个用户、两个 Session 并执行自动化越权矩阵。

## 10. 可用性、灾备和恢复

### 10.1 目标

| 数据/服务 | RPO | RTO |
|---|---:|---:|
| PostgreSQL | <= 5 分钟 | <= 60 分钟 |
| Object Storage 元数据与 Artifact | <= 15 分钟 | <= 4 小时 |
| Redis | 可丢通知 | <= 30 分钟 |
| Temporal | 按集群持久化保证 | <= 60 分钟 |

Redis 丢失不允许导致业务事件丢失。Secret 后端、OIDC 和模型供应商故障分别定义降级行为。

### 10.2 演练

- PostgreSQL 从备份恢复并核对 Snapshot、Run 和 RunEvent。
- Redis 清空后 SSE 通过数据库恢复。
- Runtime Worker 和 Temporal Worker 滚动重启。
- Sandbox 节点失效并撤销实例凭据。
- Object Storage 暂时不可用后 Artifact 重试。
- Model Gateway 限流和供应商故障。
- Outbox 积压、Dead Letter 重放和状态对账。

恢复演练至少每季度一次，记录实际 RPO/RTO 和问题闭环。

## 11. 数据生命周期与隐私

- 每类数据有 Owner、分类、保留期限、删除方式和备份策略。
- Prompt、Message、文件和工具参数进入日志/Trace 前脱敏。
- Runtime 原始 Trace 默认仅安全和平台管理员可访问，且保留期短于业务事件。
- 用户删除请求不会修改不可变审计事实，但应移除或匿名化允许删除的业务内容。
- 模型供应商的数据保留、训练使用和地域策略作为 Model Provider 配置的一部分。
- 知识库文档删除后，Chunk、向量、缓存和索引引用最终清理并可对账。

## 12. 可观测性与告警

### 12.1 Trace

传播：

```text
trace_id, tenant_id, agent_id, snapshot_id, deployment_id
session_id, run_id, workflow_id, runtime_session_id, sandbox_id
```

高基数 ID 用于 Trace/Log 查询，不直接作为长期 Metrics Label。

### 12.2 必备告警

- Run 创建或首事件延迟超 SLO。
- Run、Workflow、Sandbox 状态不一致。
- Outbox/Inbox 积压和 Dead Letter。
- Event Store 写入失败、序号冲突和终态冲突。
- Model Gateway 错误率、限流、预算拒绝和费用异常。
- Temporal Task Queue 堆积、Activity 重试和 Workflow Failure。
- Sandbox 创建失败、销毁失败、QUARANTINED 和资源超限。
- 跨租户访问拒绝异常增长和敏感工具调用异常。
- Artifact 扫描失败、对象存储错误和配额不足。

每个告警定义严重等级、阈值、持续时间、责任人、Runbook 和自动恢复条件。

## 13. 测试金字塔

### 13.1 单元测试

- 领域状态转换和非法转换。
- Prompt Compiler、Hash 和截断。
- 权限、Policy 求交集和配额计算。
- Manifest、路径、事件 Payload 和错误映射。
- Runtime 原始事件到 RunEvent 的转换。

### 13.2 契约测试

- OpenAPI 请求、响应和错误 Golden Files。
- RuntimeAdapter 同套测试运行 AgentScope 和 Codex。
- RunSpec、RunEvent、Bundle 和 Skill Manifest 版本兼容。
- MCP、ACP、AG-UI 和 Model Provider Adapter。
- Temporal Workflow Replay。

### 13.3 集成测试

- PostgreSQL + Outbox + Temporal 创建 Run。
- Event Store + Redis + SSE 断点续传。
- Sandbox + Workspace + Artifact。
- Approval + Ticket + Tool Gateway。
- Model Gateway 预算、限流和 fallback。

### 13.4 端到端测试

- Agent 创建、发布、运行、工具、文件、取消、回放和回滚。
- Worker 重启和依赖故障恢复。
- 两租户全链路隔离。
- Codex ACP Session、文件、取消和恢复。
- Vue Router 权限和 Feature Flag 在刷新、深链和租户切换后保持一致。
- RunEvent/SSE 在断线、重复、乱序和序号缺口后由 Vue 状态归并器恢复一致。
- 前端生产构建、OpenAPI 生成 Client 和 RunEvent 生成类型通过 `vue-tsc` 编译。

### 13.5 安全与性能测试

- OWASP API、SSRF、路径、供应链、权限和 Prompt Injection 场景。
- 标准工作负载压测、耐久测试、尖峰、背压和资源耗尽。

## 14. MVP 产品验收场景

### AC-001 创建与发布

1. 开发者创建模型、Prompt、Skill、MCP 和 Agent。
2. 并发编辑冲突被 ETag 阻止。
3. 发布生成不可变 Snapshot 和 AgentScope Bundle。
4. 安全扫描和 Sandbox 冒烟测试通过后原子激活。
5. 草稿再次修改不影响已激活 Deployment。

### AC-002 Run 与事件

1. 用户创建 Session 和 Run。
2. Run、Message 和 Outbox 在同一事务提交。
3. Temporal Workflow 启动，Run Sandbox 创建。
4. 文本、工具和 Artifact 事件按序持久化。
5. 浏览器断开后 Run 继续。
6. 重连从最后 sequence_no 恢复，无永久丢失和重复展示。

### AC-003 取消与 Worker 重启

1. RUNNING 状态发送取消。
2. Run 先进入 CANCELLING。
3. Runtime 正常取消或超过宽限期后强制终止 Sandbox。
4. 重复取消返回相同结果。
5. 旧 Worker 使用旧 fencing token 写事件被拒绝。

### AC-004 审批

1. 高风险工具触发 Approval。
2. Run 进入 WAITING_APPROVAL。
3. 审批参数摘要和工具信息可见，Secret 被脱敏。
4. 批准生成一次性票据并成功执行一次。
5. 重放票据被拒绝，过期或参数变化后的票据失效。

### AC-005 租户隔离

1. 创建两个租户及各自 Agent、Session、Workspace 和 Artifact。
2. 跨租户访问 API、SSE、下载 URL、MCP、知识库和 Sandbox 全部失败。
3. 审计日志记录拒绝事件，但不泄露目标资源内容。

### AC-006 故障恢复

1. Run 运行中重启 API、Runtime Worker 和 Temporal Worker。
2. Workflow 恢复；未知副作用不被盲目重放。
3. Redis 通知丢失后事件从数据库补齐。
4. 卡住状态被 Reconciliation Worker 检测并修复或转人工处理。

### AC-007 发布回滚

1. 新版本发布冒烟测试失败，旧 Deployment 继续服务。
2. 成功发布后执行回滚。
3. 回滚产生新的 Release/Deployment，历史 Run 仍指向原 Snapshot。

## 15. Codex V1 验收

- 仅通过 ACP STDIO 接入，不解析 CLI 展示文本。
- 每个 Deployment/Sandbox 使用独立 CODEX_HOME。
- Runtime Session 映射和 Compatibility Hash 正确。
- Deployment、Bundle、权限或 Sandbox 变化后不复用旧 Session。
- ACP Cancel、子进程宽限终止和 Sandbox 强制终止均可观测。
- Worker 崩溃后不盲目重复 prompt；无法恢复时返回明确错误并允许用户创建 Retry Run。
- 两个 Session 的文件、Skill、Prompt 和 MCP 不串用。

## 16. 上线阻断项

以下任一项不满足时不得生产上线：

- 租户隔离或权限越权测试失败。
- Secret 进入日志、事件、Bundle 或前端明文。
- Sandbox 可以访问宿主敏感资源或平台主库。
- RunEvent 丢失、序号冲突或终态可被覆盖。
- 发布不能保证旧版本继续服务和可回滚。
- Temporal Activity 存在未经幂等保护的自动副作用重试。
- 没有数据库备份恢复和状态对账能力。
- 无法限制 Run、模型、Sandbox、文件和事件资源使用。
- 高危 Skill、MCP、镜像或依赖未经过供应链扫描。
- Vue 前端 lint、`vue-tsc`、单元/组件测试、生产构建或关键 Playwright 流程失败。

## 17. 上线交付物

- 经评审的 OpenAPI 和契约 Golden Files。
- 数据库 ERD、字段字典、索引与 Alembic 迁移。
- RuntimeAdapter、RunEvent、AG-UI、MCP、ACP 契约测试报告。
- 威胁模型和安全测试报告。
- 容量、耐久、故障和恢复测试报告。
- 镜像 SBOM、漏洞扫描和签名记录。
- SLO Dashboard、告警和 Runbook。
- 数据保留、备份、恢复和删除策略。
- 已知风险、风险接受人和整改期限。
- 评审后的 `agent-platform-baseline.yaml`，其引用的 OpenAPI、JSON Schema 和 Golden 示例全部通过自动校验。
- AI Coding 任务追踪中不存在未关闭的 `open_questions` 或越权修改范围。
