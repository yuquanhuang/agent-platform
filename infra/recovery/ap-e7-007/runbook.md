# AP-E7-007 Recovery Runbook

## 安全边界

- 只在隔离恢复环境执行；恢复目标不得复用生产写入凭证或生产租户数据。
- 先冻结演练窗口、责任人、备份快照和证据目录，再执行故障动作。
- 不直接修改 Run、RunEvent、Workflow、Artifact 或 Audit 终态事实；只通过既有恢复、对账和幂等入口处理。
- 任何未知副作用、fencing token、Secret 或跨租户数据异常立即停止演练并升级人工处理。

## 演练顺序

1. PostgreSQL：从已验证备份恢复到隔离目标，校验 Snapshot、Run、RunEvent、Outbox、Lease 和 Audit 的计数/Hash/外键；记录 RPO/RTO。
2. Redis：清空通知数据但不删除 PostgreSQL，验证 SSE 从数据库补历史，确认业务事件无丢失。
3. Worker/Temporal：滚动重启 API、Event Worker、Reconciliation Worker、Runtime Worker 和 Temporal Worker，检查原 Workflow/Outbox 幂等与 Replay。
4. Sandbox：模拟节点不可用，撤销实例凭据，验证 Sandbox 清理、Lease 处理和对账结果；不得手工释放活跃 Lease。
5. Object Storage：注入短暂不可用，验证 Artifact 上传/扫描/下载撤销/删除状态机按有界重试收敛。
6. Model Gateway：注入供应商限流和故障，验证预算、fallback、错误映射和用户可见状态，不重复计费或重复提交。
7. Outbox/DLQ：只按原 event ID 幂等重放，完成状态对账；禁止新建替代事件、直接写终态或 blanket replay。

## 退出条件

- 所有服务达到冻结 RPO/RTO，完整性校验 PASS，证据 URI 和 SHA-256 可追溯。
- 所有必需场景和生产准入项 PASS；没有高危未关闭问题、未解决 open question 或占位证据。
- 记录实际 RPO/RTO、故障时间线、责任人、告警、恢复动作、残余风险和整改截止时间。
- 生产准入前由平台、安全、数据和业务责任人共同签字；任何一方缺失则保持阻断。

## 回滚与升级

- 恢复目标出现不一致时停止向恢复目标写入，保留现场和证据，升级数据库/平台负责人。
- 依赖持续失败由 Kubernetes 有界重启接管；不得扩大无界重试或关闭告警。
- 生产尚未准入时不修改默认 Kustomization；只在环境 overlay 中逐项启用已验证组件。
