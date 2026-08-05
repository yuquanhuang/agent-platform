# Agent 平台测试验收与 AI 任务包

> 文档版本：V1.4  
> 文档状态：开发输入基线

## 1. 测试原则

- 测试与契约同时开发，不在功能完成后补写。
- 每个 FR 至少映射一个正常、一个失败和一个权限/租户场景。
- 公共契约使用 Golden File，Schema 变化必须显式评审。
- 外部依赖使用协议 Fake 进行单元测试，使用真实容器进行集成测试。
- 不以 Mock 全部通过替代 PostgreSQL、Temporal、Redis、S3 和 Sandbox 集成验证。

## 2. CI 阶段

```text
lint/format
→ type-check
→ schema validation and generation diff
→ unit tests
→ contract tests
→ migration tests
→ integration tests
→ Temporal replay
→ Vue frontend lint/typecheck/unit/component tests
→ frontend production build
→ dependency/SBOM/secret scan
→ selected E2E/security tests
```

主分支合并禁止跳过失败阶段。容量、耐久和完整灾备在独立环境定期运行。

Schema 阶段必须使用固定版本工具完成：

1. OpenAPI 3.1 语法和语义校验，检查重复 operationId、无效 Path Parameter、断开 `$ref` 和不可满足 Schema。
2. JSON Schema Draft 2020-12 校验，所有 `examples/*` 必须通过对应 Schema。
3. OpenAPI 生成 Python/TypeScript 类型后执行编译和 Diff，生成结果变化必须显式提交。
4. 校验 `agent-platform-baseline.yaml` 中的 SHA-256，未列入基线或 Hash 不一致的契约禁止进入代码生成。
5. 跨契约对比 RunStatus、RunEvent type、Runtime capability、resource_type、Approval status 和 Error.code。

工具和元 Schema 必须进入 lockfile 或 CI 镜像；私有化/离线 CI 不得在验证时临时从公网下载 JSON Schema meta-schema。

## 3. Golden Files

目录约定：

```text
backend/tests/golden/
├── openapi/
├── run_spec/v1/
├── run_event/v1/
├── ag_ui/v1/
├── skill_manifest/v1/
├── bundle_manifest/v1/
├── errors/v1/
└── temporal_history/
```

每个 Golden File 包含：

- 正常最小对象。
- 正常完整对象。
- 每个重要枚举示例。
- 缺失必填字段失败。
- 未知字段或非法字段失败。
- 敏感字段不得出现的断言。

## 4. RuntimeAdapter Contract Test

同一套测试必须运行 AgentScopeRuntimeAdapter 和 CodexAcpRuntimeAdapter：

1. capabilities 和目标校验。
2. 正常文本流。
3. Tool call arguments 分片和结果。
4. Artifact 输出。
5. Thinking/Plan 能力存在与缺失处理。
6. RuntimeEventCandidate source_event_id 幂等。
7. 正常取消、重复取消和强制终止。
8. 超时。
9. Worker 重启后的 inspect/resume 判断。
10. 未知提交状态不重复 Prompt。
11. 旧 fencing token 拒绝。
12. Runtime 原始敏感字段不进入 RunEvent。
13. 终态冲突拒绝。
14. Session Compatibility Hash 变化后不复用。

缺失能力必须在发布时产生 `RUNTIME_CAPABILITY_MISMATCH`，不能运行时静默跳过。

## 5. API 契约测试

每个 Operation 测试：

- 认证缺失 401。
- 权限不足/不可感知资源 403 或 404。
- 请求 Schema 失败 400/422。
- Idempotency-Key 相同请求返回原结果。
- Idempotency-Key 不同请求 Hash 返回 409。
- If-Match 冲突返回 412。
- 限流/配额返回 429。
- 依赖不可用返回 503/504。
- 响应包含 X-Request-ID。
- 可编辑资源的 GET/CREATE/UPDATE 响应包含格式为 `"rv:<resource_version>"` 的 ETag。
- 所有 `OperationAccepted.status_url` 可查询并最终进入 `SUCCEEDED/FAILED/CANCELLED` 终态。
- Error.code 稳定且不依赖 message。

## 6. 数据库测试

- 空库升级至最新版本。
- 从上一发布版本升级。
- 数据回填和前向修复。
- tenant_id 缺失无法插入。
- 租户内唯一与跨租户同 code。
- 活动主 Run 部分唯一索引。
- Deployment 单 ACTIVE 约束。
- RunEvent 双唯一约束。
- 不可变 Snapshot/Version/Event/Audit Repository 无更新路径。
- 并发 CAS 和旧 resource_version 失败。

## 7. Temporal 测试

- Run 正常完成。
- CREATED、QUEUED、PREPARING、RUNNING、WAITING_APPROVAL 阶段取消。
- Activity 心跳丢失和 Worker 重启。
- Signal 重复、乱序和过期。
- Runtime 未知状态不重放 Prompt。
- 发布 Activity 重试不产生两个 ACTIVE Deployment。
- Continue-As-New 保留业务状态。
- Outbox 重复启动只产生一个 Workflow。
- 当前和上一生产 History Replay。

## 8. Event/SSE 测试

- 100 个并发候选事件分配唯一连续 sequence_no。
- 重复 source_event_id 只生成一个事件。
- 不同 attempt 相同 source_event_id 按契约处理。
- 慢消费者断开，不阻塞 Writer。
- Redis 通知丢失后从 PostgreSQL 补齐。
- Last-Event-ID 优先于 query after。
- 历史到实时切换无缺失和重复展示。
- 终态唯一，终态后不接受业务事件。
- 256KB 上限和大 Payload Artifact 化。

### 8.1 Vue 前端测试

- ESLint、Prettier 和 `vue-tsc --noEmit` 必须通过，禁止使用 `@ts-ignore` 或关闭规则掩盖契约错误。
- Vitest 覆盖 RunEvent 状态归并函数、composable、Pinia Store 和 Query Cache 隔离。
- Vue Test Utils 覆盖路由权限、Feature Flag、表单并发冲突以及 loading/empty/error/partial_data 状态。
- SSE 测试必须覆盖重复事件、乱序、序号缺口、Last-Event-ID 重连、终态补齐和组件卸载后的订阅释放。
- 两个租户、Session 和 Run 之间不得复用 Pinia 状态或 `@tanstack/vue-query` Cache Key。
- Playwright 覆盖登录、Agent 创建与发布、流式对话、取消、刷新恢复、审批和 Artifact 下载。
- OpenAPI 和 RunEvent 生成类型变化后必须执行前端编译与生成 Diff；页面不得使用手写 DTO 绕过失败。

## 9. Sandbox 安全测试

- 路径穿越、NUL、Unicode 归一化、软硬链接和 TOCTOU。
- Zip Slip、压缩炸弹、嵌套归档和大量小文件。
- 非 root、Capability、seccomp、只读根文件系统。
- fork bomb、磁盘、内存、PID、文件数和执行超时。
- DNS rebinding、HTTP redirect、私网和云元数据。
- `/proc`、环境变量和其他 Run 文件读取。
- 后台进程残留和 Session Sandbox 清理失败。
- Sandbox Provider 状态与数据库对账。

任一逃逸、跨租户或 Secret 泄漏测试失败阻断上线。

## 10. E2E 场景

### E2E-001 AgentScope 完整闭环

```text
创建模型配置
→ 创建 Prompt/Skill/MCP
→ 创建 Agent
→ 发布
→ 创建 Session/Run
→ 工具调用
→ 生成 Artifact
→ SSE 展示
→ 完成并回放
```

### E2E-002 取消和恢复

Run RUNNING 时关闭浏览器、重启 API/Runtime Worker，重新连接后事件完整；取消进入 CANCELLING，最终确认 CANCELLED，Sandbox 已销毁。

### E2E-003 审批

高风险工具触发 Approval；批准后 Ticket 只能消费一次；拒绝、过期、参数变化和自审批均符合策略。

### E2E-004 发布失败与回滚

新版本冒烟失败时旧 Deployment 继续服务；成功发布后回滚产生新 Release，历史 Run 仍指向原 Snapshot。

### E2E-005 两租户隔离

两个租户分别创建 Agent、Session、Run、Sandbox、Artifact；API、SSE、下载、MCP、缓存和对象访问全部阻断跨租户请求。

### E2E-006 Codex ACP

独立 CODEX_HOME、ACP STDIO、Session 映射、Skill/MCP、文件、取消和不可恢复错误符合契约，两个 Session 不串用。

### E2E-007 知识库权限与失效

导入文档、解析分块、建立索引并检索；不同 ACL 用户得到不同结果。更新/删除后旧 Chunk 和向量最终失效，对账任务无残留，检索内容标记为 untrusted 且不扩大工具权限。

### E2E-008 评测与反馈

对指定 Snapshot 和 EvaluationSet Version 执行离线评测，结果冻结模型、Judge 模型和 Judge Prompt 版本。敏感样本不得未授权外发；用户对 Run 提交赞/踩、标签和评论后可按权限查询。

### E2E-009 Schedule

使用 IANA 时区创建 Schedule，覆盖 DST、misfire 和 SKIP/QUEUE/REPLACE。每次触发只创建一个独立 Run，REPLACE 先受控取消旧 Run；停用 Schedule 默认不取消已开始 Run，Secret 明文不进入日志和事件。

### E2E-010 A2A Client

创建远程 Agent Binding，验证 Agent Card Hash、认证、超时、远程错误和事件映射。SSRF、私网、重定向绕过和超出 `allowed_data_classes` 的外发必须阻断并审计；不启动 A2A Server/Gateway。

## 11. 容量验收

使用非功能基线工作负载，最低验证：

- 100 个并发 AgentScope Run/tenant。
- 1,000 个 SSE 连接/cluster。
- Event Store 2,000 events/s 持续写入。
- 20 个并发 Codex Run/cluster 独立资源池。
- 30% 生产容量安全余量。

报告必须包含硬件、版本、数据分布、吞吐、延迟、错误、资源峰值、瓶颈和安全余量。

## 12. 故障注入

- PostgreSQL 短时不可用和主从切换。
- Redis 清空和通知丢失。
- Temporal Server/Worker 重启。
- Runtime Worker Kill -9。
- Sandbox 节点失效。
- Object Storage 超时和部分失败。
- Model Gateway 限流、超时和未知提交状态。
- Outbox 积压和 Dead Letter。

验证无重复副作用、状态可对账、事件不永久丢失、Secret 被撤销。

## 13. AI Coding Task Definition of Ready

任务开始前必须满足：

- 已指定 baseline_id。
- FR、AC 和非目标明确。
- API operationId/Schema 已存在。
- 数据字段、约束和迁移已明确。
- 状态转换、幂等、取消、重试、补偿明确。
- 权限、租户、Secret、Sandbox 和审计明确。
- 正常、失败、权限、并发验收用例明确。
- 文件修改范围和依赖任务明确。
- `open_questions` 为空。

## 14. AI Coding 任务包示例

```yaml
task_id: AP-E3-004
title: 实现 RunEvent 批量写入和序号分配
baseline: agent-platform-v1-dev-baseline-2026-08-r4
baseline_integrity:
  hash_algorithm: sha256
  verified_files:
    - agent-platform-openapi-v1.yaml
    - schemas/run-event-v1.schema.json
scope:
  includes:
    - RuntimeEventCandidate 批量校验
    - fencing 校验
    - sequence_no 原子分配
    - 幂等返回
    - Event Outbox
  excludes:
    - SSE Web 层
    - AG-UI 转换
requirements: [FR-RUN-003, FR-CON-005]
acceptance: [AC-002, AC-003]
authoritative_contracts:
  endpoints:
    - POST /internal/v1/runs/{run_id}/events:batch
  schemas:
    - schemas/run-event-v1.schema.json
  state_machines:
    - AgentRun
data:
  entities: [agent_run, run_attempt, run_event, outbox_event]
security:
  actions: [internal:event_write]
  audit_events: [execution_fencing_rejected, terminal_conflict]
failure_semantics:
  retryable_errors: [DEPENDENCY_UNAVAILABLE]
  non_retryable_errors: [CONTRACT_VALIDATION_FAILED, EXECUTION_FENCING_REJECTED]
file_scope:
  allowed:
    - backend/packages/contracts/events/
    - backend/packages/application/event_service/
    - backend/tests/contract/events/
  forbidden:
    - frontend/
    - backend/packages/runtimes/
verification:
  commands:
    - uv run ruff check .
    - uv run pyright
    - uv run pytest backend/tests/contract/events backend/tests/integration/event_store
  expected_results:
    - 100 并发写入序号唯一连续
    - 重复候选返回 duplicate
    - 旧 fencing token 返回 409
open_questions: []
```

## 15. AI 代码审查清单

- 是否新增了未在 Schema 中定义的字段或枚举？
- 是否出现裸 dict/Any 公共契约？
- 是否绕过 TenantContext、Policy、Temporal、Event Service 或 Sandbox？
- 是否在重试中重复外部写副作用？
- 是否在日志、事件和异常中暴露 Secret 或敏感内容？
- 是否错误地把 Redis/内存状态当作事实来源？
- 是否缺少非法状态和并发测试？
- 是否修改了任务范围外文件？
- 是否同步更新 Golden File 和追踪矩阵？

## 16. 发布阻断

以下任一失败不得上线：

- 跨租户越权或权限绕过。
- Secret 泄漏。
- Sandbox 逃逸或宿主敏感资源访问。
- RunEvent 丢失、重复展示、序号冲突或终态覆盖。
- Temporal 自动重试产生重复写副作用。
- 发布失败影响旧 Deployment。
- 无备份恢复、状态对账、资源上限或关键告警。
