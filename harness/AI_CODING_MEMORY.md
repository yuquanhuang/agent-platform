# Agent Platform AI Coding Memory

> 用途：为后续 AI Coding 任务恢复项目上下文。本文是开发记忆摘要，不替代冻结契约、需求文档、任务 YAML 或测试结果。
>
> 导出时间：2026-08-14

## 权威来源与优先级

1. `docs/agent-platform/agent-platform-baseline.yaml`
2. `docs/agent-platform/` 下的需求、架构、接口、Schema、数据库、Temporal、Sandbox、安全和前端契约
3. `harness/tasks/AP-*.yaml`
4. `harness/AI_CODING_PROGRESS.md`

当前冻结基线：`agent-platform-v1-dev-baseline-2026-08-r22`，冻结日期 2026-08-14，完整性算法为 SHA-256。任何契约或基线完整性不一致都必须停止并先修正文档，不能自行选择冲突方案。

## 项目与技术栈

- 后端 Python 3.12，代码位于 `backend/`；FastAPI、Pydantic、SQLAlchemy Async、Alembic、Temporal、PostgreSQL、Redis、MinIO/Object Storage。
- 前端代码位于 `frontend/`；Vue 3、TypeScript、Vite、Vue Router 4、Pinia、`@tanstack/vue-query`、Element Plus。
- 模型供应商 Adapter 支持 OpenAI、Qwen、DeepSeek；模型供应商正式凭证、在线验证和费用表仍属于部署配置/外部事实。
- AgentScope 版本范围为 2.0.x，当前 Spike 使用 2.0.5 边界；精确 patch 和生产镜像 digest 由锁文件/现有 CI 制品确定。
- OIDC Issuer/Claim 映射当前 local/test 使用 Mock；staging/production 禁止 Mock，真实 OIDC/JWKS 仍是生产前置。
- Secret 使用 `EnvSecretBackend`（`secret://env/AP_SECRET_*`）；Vault Adapter 只保留边界，未配置时 fail-closed。

## 已冻结的重要设计决策

- Agent/Deployment Runtime 只消费已发布 Snapshot 和不可变 Bundle。
- Model Gateway 使用不可变 `model_binding_snapshot`，生产 Binding Reader 不读取可变 Provider Draft。
- BudgetPolicy 支持 HARD/SOFT、USD/CNY，不换汇；币种或可信事实不完整时失败关闭。
- RunEvent 当前使用非分区表；后续生产容量前必须迁移为“全局唯一登记表 + `recorded_at` 月分区事件表”。
- Redis 只作通知/唤醒通道，PostgreSQL 是 RunEvent、Outbox、Queue、Capacity Lease 和 Reconciliation 事实源。
- Artifact 使用私有 MinIO/S3 + Download Gateway，Download API 冻结单 Range 的 200/206/416 语义；不使用不可撤销的对象存储直签 URL。
- Sandbox 生产要求非 root、gVisor 或等价隔离、RuntimeDefault seccomp/AppArmor、只读根、drop ALL、资源/PID/生命周期限制、独立 ServiceAccount、默认拒绝网络和不可变镜像 digest。
- 生产不允许 critical/high/known-exploitable 漏洞风险例外；证据包含 `risk_exceptions` 直接 fail-closed。
- 每次只推进一个用户可见 `AP-E<epic>-NNN` 任务；不要自动拆成多个用户子阶段。未执行的真实验证不得标记为通过。

## 开发完成概况

- Epic 0：工程底座、身份/租户、契约生成、PostgreSQL/RLS、Temporal/Outbox/可观测性已完成。
- Epic 1：Prompt、Model Provider/Config、Model Gateway、Agent Draft、Vue 编辑器已完成；E1-006 的生产预算/费用事实和 E1-009 的生产镜像 digest 仍有外部待办。
- Epic 2：Snapshot、Bundle、Release、Deployment、Preview/Diff、Rollback 已完成。
- Epic 3：Session、不可变 Message 历史、Run、Temporal Workflow/Activity、取消/重试/fencing、Outbox/对账已完成。
- Epic 4：RunEvent、Event Store、SSE、AG-UI、Vue Reducer、断线恢复/回放已完成；非分区 RunEvent 迁移待办保留。
- Epic 5：Sandbox/Workspace/Artifact 生命周期、路径安全、扫描、Download Gateway、清理和权限 E2E 已完成；真实 Sandbox Provider、外部 Malware Scanner 和 Run Attachment 仍待补齐。
- Epic 6：Skill、MCP、Policy、Approval、一次性 Execution Ticket、Tool Gateway、Audit 和 AgentScope Runtime Bridge 已完成；AgentScope Runtime 完整生产执行组合仍缺。
- Epic 7：
  - `AP-E7-001` 已完成。
  - `AP-E7-002` 进行中：Worker/Redis/MinIO/Download Gateway/Secret/Checkpoint/Kubernetes 边界已建设；真实 Sandbox Manager Provider、AgentScope Runtime Worker、OIDC/JWKS 和部分生产 overlay 未完成。
  - `AP-E7-003` 代码与契约已完成：Quota/Storage/Budget/Queue/Retention/Capacity Domain/Lease；真实容量和部分生产组合留待后续。
  - `AP-E7-004` 进行中：低基数指标、Worker metrics、告警规则和 Runbook 已实现；真实 receiver、完整生产 scrape、持久 backlog/saturation Gauge 和完整 Trace propagation 后置。
  - `AP-E7-005` 进行中：容量 runner 和计划模板已完成；真实 100 AgentScope Run、1000 SSE、2000 events/s、20 Codex Run、耐久/尖峰/隔离测试未执行。
  - `AP-E7-006` 进行中：供应链/Sandbox 静态验收工具已完成；真实 Registry/SBOM/签名/provenance、Provider 和动态安全测试未执行。
  - `AP-E7-007` 进行中：恢复证据/RPO-RTO/生产准入静态验收工具和 Runbook 已完成；真实备份恢复、故障注入、季度演练和生产签字未执行。
- Epic 8/9：尚未进入生产开发；下一主线任务按执行计划为 `AP-E8-001`，但不得丢弃 Epic 7 外部生产准入待办。

## AP-E7-007 当前恢复目标

- PostgreSQL：RPO ≤ 5 分钟，RTO ≤ 60 分钟。
- Object Storage 元数据/Artifact：RPO ≤ 15 分钟，RTO ≤ 4 小时。
- Redis：只允许丢通知，RTO ≤ 30 分钟；业务事件必须由 PostgreSQL 恢复。
- Temporal：RTO ≤ 60 分钟。
- 必须演练：数据库恢复、Redis 丢通知后 SSE 补齐、Worker/Temporal 滚动重启、Sandbox 节点故障和凭证撤销、Object Storage 故障、Model Gateway 限流/供应商故障、Outbox/DLQ 原事件重放与对账。
- 生产准入需要真实不可变证据、RPO/RTO、完整性检查、7 个场景、10 个准入项和平台/安全/数据/业务责任人签字。

## 最近验证基线

- AP-E7-007 定向测试：`8 passed`。
- `make backend-check`：`816 passed, 20 skipped`。
- `make frontend-check`：26 个测试文件，`85 passed`，生产构建通过。
- `make contract-check`：R22，31 个完整性文件、190 个 operationId、7 个 Schema、184 个引用、4 个 Golden、17 个生成文件零漂移。
- `make check`：通过。
- AP-E7-007 dry-run 报告：`/private/tmp/ap-e7-007-recovery.md`，状态为 `dry_run`，模板未执行项和阻断项被明确列出。
- 已知环境告警：用户 `.npmrc` 权限告警、Rollup 上游 PURE 注释告警；当前不影响门禁。
- Docker daemon 当前未运行，因此没有执行真实容器/基础设施恢复演练；不能把本地 dry-run 当作生产灾备证据。

## 仍需保留的阻断与待办

- 现有 CI 回填 AP-E1-009/backend/runtime 真实 Registry digest、SBOM、漏洞/许可证/Secret/恶意代码扫描、签名和 provenance。
- AgentScope Runtime Worker 缺生产 SessionFactory、RunSpec/Bundle Loader、Tool Gateway/Executor 和完整 Temporal Activity 组合。
- Sandbox Manager 缺真实 Kubernetes Provider、Policy Resolver、Provision Token Verifier、RuntimeClass/Workload Identity/受控 egress proxy 组合。
- 真实 OIDC/JWKS、外部 Malware Scanner、告警 receiver/排班/通知 Secret、备份责任人和生产 sign-off。
- AP-E7-005 真实容量/耐久/资源池隔离报告；当前 30% headroom 口径仍需容量评审确认。
- RunEvent 全局登记表 + 月分区迁移、AJV standalone codegen、Run Attachment、Audit 归档/source_ip/client、可信 Workflow 终态物化。
- 历史任务 YAML 中仍可能保留已被实现决策 supersede 的 `open_questions`；继续开发时以当前冻结基线、最新任务记录和实际代码为准，并在适当任务中清理历史记录。

## 继续开发规则

1. 开始任务先读根 `AGENTS.md`、`harness/project.json`、common 规则和目标目录 `AGENTS.md`。
2. 先确认影响端、共享契约、数据库迁移、Temporal Replay、RLS、权限、审计和已有测试。
3. 只修改需求范围内文件；不 reset/checkout/覆盖当前工作区已有改动。
4. 后端改动至少跑 `make backend-check`；跨端或高风险改动跑 `make check`，契约相关改动跑 `make contract-check`。
5. 真实依赖缺失时记录阻断，不使用 Stub/Fake/dry-run 代替生产结论。
6. 交付时说明行为变化、契约/迁移影响、验证事实、未完成项和人工确认项。

## 当前工作区注意事项

工作区包含 AP-E7-005、AP-E7-006、AP-E7-007 及进度记录的未提交改动，均属于当前开发成果。继续工作时不得执行 destructive reset/checkout，也不要删除这些未跟踪目录。
