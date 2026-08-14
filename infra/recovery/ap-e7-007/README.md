# AP-E7-007 灾备、恢复演练与生产准入

本目录提供恢复目标、演练证据模板和生产准入静态验收工具的输入。它不会创建备份、
恢复数据库、清空 Redis、停止 Worker 或调用任何生产依赖。

```bash
cd backend
../.venv/bin/python3 -m apps.recovery_acceptance.main \
  ../infra/recovery/ap-e7-007/plan.yaml \
  --output /private/tmp/ap-e7-007-recovery
```

示例计划固定为 `mode: dry_run`，模板中的所有演练和准入项均为 `NOT_RUN`，因此报告会
明确列出阻断项，但状态仍为 `dry_run`，不能作为生产准入证据。

生产执行必须在隔离环境完成，并回填不可变的备份、恢复、校验、指标、日志和审批证据。
生产验收固定目标：PostgreSQL RPO/RTO 为 5 分钟/60 分钟，对象存储元数据与 Artifact
为 15 分钟/4 小时，Redis 只允许丢通知且 RTO 为 30 分钟，Temporal RTO 为 60 分钟。
任一必需场景、准入项、RPO/RTO、完整性校验或证据缺失时保持 fail-closed。

操作边界和回滚顺序见 [runbook.md](./runbook.md)。
