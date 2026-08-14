# Agent Platform Worker Runbook

适用范围：Event Worker、Reconciliation Worker，以及其 `/metrics` 抓取和告警。

## 快速分流

1. 查看 Pod、最近重启原因和 `agent_platform_process_up`；不要直接删除数据库事实。
2. 检查 PostgreSQL、Redis、Temporal、MinIO、Sandbox Manager 的连通性和证书/Secret 引用。
3. Event Worker 优先检查 Outbox `DEAD`、重试耗尽和对象存储删除/扫描依赖。
4. Reconciliation Worker 优先检查 `AP_RUN_CAPACITY_DOMAIN_SLOTS`、租约过期续租、Sandbox Manager 身份和队列超时。
5. 修复依赖后等待一个完整轮询周期；只在确认进程未恢复时滚动替换 Pod。

## Event Worker down

检查 PostgreSQL、Redis、Temporal 和 MinIO readiness，再检查 Secret Ref 与 NetworkPolicy。不得为恢复抓取而将 metrics Service 暴露到公网。

## Reconciliation Worker down

除通用依赖外，检查 Sandbox Manager TLS/服务身份、Execution Ticket Key 和 `AP_RUN_CAPACITY_DOMAIN_SLOTS`。

## Recovery exhausted

保留最后一次依赖异常。修复依赖或配置后替换 Pod，不要通过扩大无界重试隐藏故障。

## Outbox dead letter / retries

查看原 event type、attempt 和脱敏错误。只能使用原 event ID 幂等重放；不新建替代事件或直接修改业务终态。

## Reconciliation unresolved / Sandbox cleanup

先隔离未能清理的 Sandbox、撤销凭据，再修复 Sandbox Manager 依赖。持久事实修复后等待一个干净对账周期。

## Run queue timeout / capacity lease

核对 runtime target 到 capacity domain 的映射、slots、活动 Lease 和 WAITING deadline。禁止仅因 Lease 过期就手动释放活跃 Run。

## Temporal start

恢复 Temporal 连通性后让原 Outbox 事件幂等重试，核对 Workflow ID/Temporal Run ID 映射，不直接伪造启动成功。

## RunEvent rejected / latency

检查 Candidate Schema、Attempt fencing、Run 终态和 PostgreSQL 锁竞争。不得丢弃或改写已持久事件规避拒绝。

## SSE visibility / delivery degradation

检查 Redis 通知、PostgreSQL 补历史路径、API metrics 抓取和代理/客户端发送速度。Redis 丢通知时应退化为数据库查询，不允许将 Redis 变为事件事实源；持续 `send_timeout` 时应先隔离慢消费者或检查代理缓冲，不得取消发送上限。

## 告警恢复语义

- `*WorkerDown`：进程重新暴露 `/metrics` 且 `agent_platform_process_up == 1` 后恢复。
- `WorkerRecoveryExhausted`：依赖恢复、Pod 被替换并完成初始化后恢复。
- `OutboxDeadLetter`：修复依赖并按原事件 ID 重放后恢复；禁止新建替代事件绕过审计。
- `ReconciliationUnresolved`：持久化事实完成修复并出现干净对账周期后恢复。
- `RunQueueTimeouts`：容量域 slots/租约/队列延迟恢复，且不再产生超时后恢复。
- Event/SSE 延迟告警：PostgreSQL/Redis 恢复，p95 回到阈值以下后恢复。

## 安全边界

Metrics Service 只允许 Prometheus 命名空间访问 9090/TCP；不要将其配置为公网入口。告警和日志中不得加入 tenant、run、workflow 或下载 Token。
