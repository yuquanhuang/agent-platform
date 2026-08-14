# AP-E7-005 容量验收工具

`backend/packages/capacity_testing` 是配置驱动的真实 HTTP 负载工具，支持：

- AgentScope Run 并发与终态耗时；
- Codex Run 独立资源池并发（通过 `scenario: codex_runs`）；
- Event Store 持续批量写入；
- SSE 在线连接、帧读取和慢消费者延迟。

SSE runner 按 `reconnect_ratio_per_minute` 对连接做错峰重连，并以同时打开连接峰值而不是
累计重连次数判断容量，避免重连次数把容量结果虚高。

运行前必须在独立环境提供真实 API/Worker/数据库/Redis/Temporal/Runtime 组合、有效的
session/run/fencing facts 和环境变量凭证。配置中只写环境变量名，不写 Token、Cookie 或
fencing token 明文。

```bash
cd backend
../.venv/bin/python3 -m apps.capacity_runner.main \
  ../infra/capacity/ap-e7-005-agentscope-smoke.yaml \
  --output ../artifacts/ap-e7-005/agentscope-smoke
```

示例配置默认 `dry_run: true`，只校验计划和报告格式，不产生任何网络流量。将其改为
`false` 之前，必须完成环境检查并记录硬件、版本、数据分布、采样窗口和故障注入边界。

报告中的 30% 余量按 `required = target * (1 + headroom_ratio)` 计算；生产最终验收仍需
由平台负责人确认 capacity definition，并附 Prometheus 资源峰值、瓶颈和回滚记录。

当前工具不会自动注入 PostgreSQL/Redis/Temporal/Runtime 故障，也不会用 Fake、单元测试或
dry-run 声称容量达标。AgentScope Runtime 尚未进入生产组合、Event 写入侧功能缓冲/Delta
合并/终态优先语义和 RunEvent 月分区仍是 AP-E7-005 前置待办。
