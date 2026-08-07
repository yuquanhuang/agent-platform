# Agent 平台数据库详细设计

> 文档版本：V1.2
> 文档状态：开发输入基线  
> 数据库：PostgreSQL 16+  
> ORM：SQLAlchemy 2.x Async  
> 迁移：Alembic

## 1. 目标与约束

本文定义首期开发需要的表、字段类型、约束、索引和数据生命周期。实现不得根据 ORM 默认行为自行改变 Null、唯一性、外键和删除语义。

固定约束：

- 主键使用 `uuid`，API 对外以不透明字符串表示。
- 所有租户业务表包含 `tenant_id uuid not null`。
- 时间使用 `timestamptz`，数据库和应用统一 UTC。
- 金额使用 `numeric(20,8)` 和 `char(3)` 币种。
- Hash 保存 `varchar(80)`，格式为 `sha256:<64 hex>`。
- 可编辑资源使用 `resource_version bigint not null default 1`。
- 不可变事实表不提供普通 Update/Delete Repository 方法。
- JSONB 字段必须在应用层绑定明确 Pydantic Schema 和 `schema_version`。
- 外键默认 `ON DELETE RESTRICT`；只有明确标注时允许 CASCADE 或 SET NULL。
- API 删除默认软删除；物理清理由保留策略 Workflow 执行。

## 2. PostgreSQL 扩展与命名

允许扩展：

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
```

命名规则：

- 表名、列名、索引名使用 `snake_case`。
- PK：`pk_<table>`。
- FK：`fk_<table>__<column>__<target>`。
- 唯一约束：`uq_<table>__<columns>`。
- 普通索引：`ix_<table>__<columns>`。
- Check：`ck_<table>__<meaning>`。

## 3. 公共字段

### 3.1 可编辑租户资源

```text
id uuid PK
tenant_id uuid NOT NULL
resource_version bigint NOT NULL DEFAULT 1
created_at timestamptz NOT NULL
created_by uuid NOT NULL
updated_at timestamptz NOT NULL
updated_by uuid NOT NULL
deleted_at timestamptz NULL
deleted_by uuid NULL
```

### 3.2 不可变事实

```text
id uuid PK
tenant_id uuid NOT NULL
created_at timestamptz NOT NULL
created_by uuid/service identity NOT NULL
```

不可变事实可以追加状态物化字段，但必须通过状态机 CAS 更新，不能修改事实内容。

## 4. 身份与租户

### 4.1 tenant

| 字段 | 类型 | Null | 说明 |
|---|---|---:|---|
| id | uuid | 否 | PK |
| code | citext | 否 | 全局唯一，`^[a-z][a-z0-9_-]{2,63}$` |
| name | varchar(100) | 否 | 显示名称 |
| status | varchar(20) | 否 | ACTIVE/SUSPENDED/DELETING/DELETED |
| quota_policy_id | uuid | 是 | 配额策略 |
| created_at/updated_at | timestamptz | 否 | UTC |

约束和索引：

- `uq_tenant__code(code)`。
- `ix_tenant__status(status)`。

### 4.2 app_user

| 字段 | 类型 | Null | 说明 |
|---|---|---:|---|
| id | uuid | 否 | PK |
| identity_issuer | varchar(255) | 否 | OIDC issuer |
| external_subject | varchar(255) | 否 | OIDC subject |
| display_name | varchar(100) | 否 | 显示名 |
| email | citext | 是 | 仅展示，不作为主身份 |
| status | varchar(20) | 否 | ACTIVE/DISABLED/DELETED |
| created_at/updated_at | timestamptz | 否 | UTC |

- 唯一：`identity_issuer + external_subject`。

### 4.3 tenant_member

字段：`id, tenant_id, user_id, status, joined_at, disabled_at, resource_version`。

- 唯一：`tenant_id + user_id`。
- FK tenant/user 均 RESTRICT。

### 4.4 role、role_permission、role_binding

- `role`：`id, tenant_id, code, name, scope, built_in, resource_version`。
- `role_permission`：`role_id, resource_type, action, condition_schema_version, condition_json`。
- `role_binding`：`id, tenant_id, subject_type, subject_id, role_id, resource_type, resource_id, expires_at`。
- `role` 唯一：`tenant_id + code`。
- `role_binding` 索引：`tenant_id + subject_type + subject_id`、`tenant_id + resource_type + resource_id`。

## 5. 资源注册中心

Prompt、Skill、MCP、ModelConfig、KnowledgeBase、SandboxProfile 采用 Definition + Version 模式。

### 5.1 resource_definition

| 字段 | 类型 | Null | 说明 |
|---|---|---:|---|
| id | uuid | 否 | PK |
| tenant_id | uuid | 否 | 租户 |
| resource_type | varchar(32) | 否 | PROMPT/SKILL/MCP/MODEL/KNOWLEDGE/SANDBOX |
| code | citext | 否 | 租户内类型唯一 |
| name | varchar(100) | 否 | 名称 |
| description | varchar(2000) | 是 | 描述 |
| status | varchar(20) | 否 | DRAFT/ACTIVE/DISABLED/DELETING/DELETED |
| owner_user_id | uuid | 否 | Owner |
| visibility | varchar(20) | 否 | PRIVATE/TENANT |
| current_draft_json | jsonb | 否 | 对应强类型 Draft Schema |
| draft_schema_version | varchar(16) | 否 | Schema 版本 |
| resource_version | bigint | 否 | CAS 版本 |
| created/updated/deleted_* | 公共字段 | - | - |

- 唯一：`tenant_id + resource_type + code`，条件 `deleted_at is null`。
- Draft JSON 不允许存 Secret 明文。

### 5.2 resource_version

| 字段 | 类型 | Null | 说明 |
|---|---|---:|---|
| id | uuid | 否 | PK |
| tenant_id | uuid | 否 | 租户 |
| definition_id | uuid | 否 | FK resource_definition |
| version_no | bigint | 否 | 单调版本 |
| schema_version | varchar(16) | 否 | 内容 Schema |
| content_json | jsonb | 否 | 不可变版本内容 |
| content_hash | varchar(80) | 否 | Canonical JSON Hash |
| status | varchar(20) | 否 | PUBLISHED/DISABLED |
| source_uri | text | 是 | Git/上传来源 |
| source_hash | varchar(80) | 是 | 来源 Hash |
| published_at | timestamptz | 否 | 发布时间 |
| published_by | uuid | 否 | 发布人 |

- 唯一：`definition_id + version_no`。
- 唯一：`definition_id + content_hash`，避免重复版本。
- 已被 Snapshot 引用的记录不可物理删除。

## 6. Agent 与发布

### 6.1 agent_definition

| 字段 | 类型 | Null | 说明 |
|---|---|---:|---|
| id | uuid | 否 | PK |
| tenant_id | uuid | 否 | 租户 |
| code | citext | 否 | 租户内唯一 |
| name | varchar(100) | 否 | 名称 |
| description | varchar(2000) | 是 | 描述 |
| icon_uri | text | 是 | 受控 Artifact URI |
| runtime_type | varchar(20) | 否 | AGENTSCOPE/CODEX |
| visibility | varchar(20) | 否 | PRIVATE/TENANT |
| owner_user_id | uuid | 否 | Owner |
| default_language | varchar(16) | 否 | 默认 `zh-CN` |
| status | varchar(20) | 否 | DRAFT/ACTIVE/DISABLED/DELETING/DELETED |
| active_deployment_id | uuid | 是 | 当前默认 Deployment，仅作查询物化 |
| resource_version | bigint | 否 | CAS |
| created/updated/deleted_* | 公共字段 | - | - |

- 唯一：`tenant_id + code where deleted_at is null`。
- `active_deployment_id` 不作为发布事实来源，事实以 Deployment 状态为准。

### 6.2 agent_binding

字段：

```text
id uuid PK
tenant_id uuid NOT NULL
agent_id uuid NOT NULL
resource_type varchar(32) NOT NULL
resource_id uuid NOT NULL
version_policy varchar(24) NOT NULL
fixed_version_id uuid NULL
binding_role varchar(32) NULL
configuration_json jsonb NULL
configuration_schema_version varchar(16) NULL
created_at/by, updated_at/by
```

- `resource_type`：PROMPT/SKILL/MCP/MODEL/KNOWLEDGE/SANDBOX/AGENT。
- `version_policy`：FIXED/RESOLVE_ON_PUBLISH。
- FIXED 时 `fixed_version_id` 必填；RESOLVE_ON_PUBLISH 时必须为空。
- 唯一：`agent_id + resource_type + resource_id + coalesce(binding_role,'')`。
- Model binding 的 `binding_role` 仅允许 `PRIMARY/FALLBACK_1/FALLBACK_2`；单 Model binding 省略角色时应用层规范化为 `PRIMARY`，多 Model binding 必须恰好一个主路由且 fallback 连续、最多两级。
- `configuration_schema_version` 当前仅允许 `model-routing/v1`，`configuration_json` 仅允许主 Model binding 保存受控 `fallback_error_codes`；非 Model binding 和 fallback binding 不得保存模型路由配置。
- 应用层在同一事务校验角色和路由配置；数据库增加同一 Agent 单个角色的条件唯一约束，防止并发写入重复主路由或 fallback 层级。
- Agent 发布时将确定的 ModelConfig Version、路由顺序、错误码策略和对应不可变 `model_binding_snapshot` 编译进 AgentSnapshot，Draft 表不作为运行时读取源。

### 6.3 agent_version、agent_snapshot

`agent_version`：

```text
id, tenant_id, agent_id, version_no, created_from_version_id,
release_note, created_at, created_by
```

`agent_snapshot`：

```text
id, tenant_id, agent_version_id, schema_version, content_json,
content_hash, compiler_input_hash, created_at, created_by
```

- `agent_id + version_no` 唯一。
- Snapshot 的 `content_json/content_hash` 永不更新。
- Snapshot 只保存 Secret Reference，不保存 Secret 值。

### 6.4 runtime_bundle

字段：`id, tenant_id, snapshot_id, runtime_type, compiler_name, compiler_version, manifest_schema_version, manifest_json, content_hash, object_uri, size_bytes, signature_ref, scan_status, created_at`。

- 唯一：`snapshot_id + runtime_type + compiler_version + content_hash`。
- `object_uri` 指向不可变对象。

### 6.5 release、deployment

`release`：

```text
id, tenant_id, agent_id, requested_by, expected_agent_version,
status, workflow_id, snapshot_id, error_code, error_detail_json,
created_at, started_at, finished_at
```

`deployment`：

```text
id, tenant_id, agent_id, snapshot_id, bundle_id, runtime_target_id,
status, compatibility_hash, activated_at, retired_at, created_at
```

- Release 状态遵循领域状态机。
- 部分唯一索引：`agent_id + runtime_target_id where status='ACTIVE'`。
- 激活通过单事务退役旧 Deployment 并激活新 Deployment。

## 7. Session、Message 与 Run

### 7.1 chat_session

字段：

```text
id uuid PK
tenant_id uuid NOT NULL
user_id uuid NOT NULL
agent_id uuid NOT NULL
default_deployment_id uuid NOT NULL
title varchar(200) NULL
status varchar(20) NOT NULL
cursor_message_id uuid NULL
branch_root_id uuid NULL
metadata_json jsonb NOT NULL DEFAULT '{}'
metadata_schema_version varchar(16) NOT NULL
resource_version bigint NOT NULL
created_at, updated_at, archived_at, deleted_at
```

索引：`tenant_id + user_id + updated_at desc`、`tenant_id + agent_id + created_at desc`。

### 7.2 chat_message

字段：`id, tenant_id, session_id, branch_id, parent_message_id, role, content_parts_json, content_schema_version, source_run_id, created_at, created_by`。

- Message 内容不可更新。
- `role`：USER/ASSISTANT/SYSTEM/TOOL。
- `content_parts_json` 必须符合 MessageContent Schema。

### 7.3 agent_run

| 字段 | 类型 | Null | 说明 |
|---|---|---:|---|
| id | uuid | 否 | PK |
| tenant_id | uuid | 否 | 租户 |
| session_id | uuid | 否 | Session |
| branch_id | uuid | 是 | 分支 |
| user_message_id | uuid | 否 | 用户消息 |
| assistant_message_id | uuid | 否 | Assistant 占位/最终消息 |
| agent_id | uuid | 否 | Agent |
| snapshot_id | uuid | 否 | 不可变 Snapshot |
| deployment_id | uuid | 否 | 执行 Deployment |
| status | varchar(24) | 否 | RunStatus |
| result_quality | varchar(32) | 是 | NORMAL/SUCCEEDED_WITH_WARNINGS |
| current_attempt | integer | 否 | 默认 0 |
| latest_sequence_no | bigint | 否 | 默认 0 |
| idempotency_key | varchar(128) | 否 | 创建幂等 |
| client_request_id | varchar(128) | 是 | 客户端关联 |
| retry_of_run_id | uuid | 是 | 原 Run |
| timeout_seconds | integer | 否 | 1..86400 |
| token_budget | bigint | 是 | Token 上限 |
| cost_budget_amount | numeric(20,8) | 是 | 金额 |
| cost_budget_currency | char(3) | 是 | 币种 |
| workflow_id | varchar(255) | 是 | Temporal ID |
| error_code | varchar(64) | 是 | 稳定错误码 |
| error_detail_json | jsonb | 是 | 脱敏错误 |
| created/queued/started/finished_at | timestamptz | 对应阶段 | 时间 |

- 唯一：`tenant_id + created_by + idempotency_key`。
- Check：金额和币种同时为空或同时非空。
- 部分唯一索引：`session_id where status not in ('SUCCEEDED','FAILED','CANCELLED','TIMEOUT') and branch_id is null`。

### 7.4 run_attempt

字段：`id, tenant_id, run_id, attempt_no, fencing_token_hash, worker_id, runtime_handle_ref, status, started_at, heartbeat_at, finished_at, error_code`。

- 唯一：`run_id + attempt_no`。
- `fencing_token` 明文只在短期内部消息传递，数据库保存不可逆 Hash 或等价安全表示。

### 7.5 runtime_session

字段：`id, tenant_id, platform_session_id, runtime_type, runtime_target_id, remote_session_id, deployment_id, sandbox_instance_id, compatibility_hash, status, last_used_at, created_at, released_at`。

- 索引：`tenant_id + platform_session_id + runtime_type + status`。
- Compatibility Hash 变化后旧记录标记 STALE。

## 8. Event Store

### 8.1 run_event

字段：

```text
id uuid PK
tenant_id uuid NOT NULL
run_id uuid NOT NULL
session_id uuid NOT NULL
sequence_no bigint NOT NULL
source_event_id varchar(255) NOT NULL
execution_attempt integer NOT NULL
schema_version varchar(16) NOT NULL
event_type varchar(64) NOT NULL
payload_version varchar(16) NOT NULL
payload_json jsonb NOT NULL
occurred_at timestamptz NOT NULL
recorded_at timestamptz NOT NULL
trace_id varchar(64) NOT NULL
```

- 唯一：`run_id + sequence_no`。
- 唯一：`run_id + execution_attempt + source_event_id`。
- Payload 最大 256KB，由应用和数据库 Check 双重限制。
- 按 `recorded_at` 月分区；达到租户规模阈值后评估子分区。
- 索引：`tenant_id + run_id + sequence_no`、`tenant_id + event_type + recorded_at`。

### 8.2 run_event_counter

字段：`run_id PK, tenant_id, next_sequence_no bigint`。

Event Service 在事务中通过原子更新分配连续序号；实现也可使用 AgentRun.latest_sequence_no 条件更新，但只能选择一种方案并通过并发测试。

## 9. Sandbox、Workspace 与 Artifact

- `sandbox_instance`：`id, tenant_id, scope, image_digest, policy_hash, runtime_target_id, status, provider_ref, lease_expires_at, created_at, terminated_at, failure_code`。
- `sandbox_lease`：`id, tenant_id, sandbox_id, holder_run_id, fencing_token_hash, acquired_at, expires_at, released_at`。
- `workspace`：`id, tenant_id, user_id, session_id, run_id, uri, quota_bytes, used_bytes, status, created_at, expires_at`。
- `artifact`：`id, tenant_id, workspace_id, run_id, owner_user_id, name, object_uri, content_hash, size_bytes, content_type, status, required_output, scan_result_json, created_at, expires_at, deleted_at`。

约束：

- Workspace URI 唯一且必须通过 URI Parser 生成，禁止直接拼接。
- Artifact 在 AVAILABLE 前不得提供普通下载。
- Sandbox Lease 同一 Sandbox 同时最多一个未释放记录。

## 10. Approval、审计和可靠消息

- `approval_request`：`id, tenant_id, run_id, execution_attempt, requester_id, tool_name, parameter_digest, policy_version, status, expires_at, resource_version, created_at`。
- `approval_decision`：`id, tenant_id, approval_id, actor_id, decision, comment, created_at`，不可变。
- `execution_ticket`：`id, tenant_id, approval_id, nonce_hash, tool_name, parameter_digest, expires_at, consumed_at`。
- `audit_log`：`id, tenant_id, actor_type/id, action, resource_type/id, result, reason, diff_digest, metadata_json, trace_id, created_at`，不可更新删除。
- `idempotency_record`：`id, tenant_id, actor_id, operation_type, idempotency_key, request_hash, status, response_status, response_ref, expires_at`。
- `outbox_event`：`id, tenant_id, aggregate_type/id, event_type, payload_json, payload_schema_version, status, attempts, next_attempt_at, created_at, published_at`。
- `inbox_record`：`consumer, message_id, tenant_id, status, received_at, processed_at`，`consumer + message_id` 唯一。

## 11. 模型用量与预算

- `model_usage`：`id, tenant_id, run_id, provider, model, provider_request_id, input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, token_estimated, cost_amount, cost_currency, started_at, finished_at`。
- `quota_policy`：租户/用户/Agent/Runtime 范围和并发、速率、存储上限。
- `budget_policy`：周期 Token/费用硬软限制。
- `budget_reservation`：`run_id, policy_id, reserved_amount, consumed_amount, released_amount, status`。

## 12. V1 增量表

知识库、评测、Schedule 和 A2A 在对应 Epic 启动时创建迁移，MVP 初始迁移不创建无使用代码的占位表；但实施时必须使用以下冻结字段和约束，不得临时发明第二套模型。

### 12.1 knowledge_base

```text
id, tenant_id, code, name, adapter_type,
embedding_model_config_id, chunking_policy_json,
index_version, status, resource_version,
created_by, created_at, updated_at, deleted_at
```

- 唯一：`tenant_id + code where deleted_at is null`。
- V1 `adapter_type=default`，但 Repository 和 Retrieval Port 保持适配器边界。
- `embedding_model_config_id` 必须指向已发布、可用的 ModelConfig Version。

### 12.2 knowledge_document

```text
id, tenant_id, knowledge_base_id, artifact_id,
source_version, content_hash, acl_subjects_json,
index_version, status, error_code, resource_version,
created_by, created_at, updated_at, deleted_at
```

- 唯一：`knowledge_base_id + artifact_id + source_version`。
- 状态使用 KnowledgeDocument 状态机；删除进入 DELETING 后检索层立即排除，后台再清理 Chunk/向量。

### 12.3 knowledge_chunk / knowledge_index_record

```text
knowledge_chunk:
id, tenant_id, document_id, chunk_no, chunk_version,
content_hash, text_ref, token_count, acl_subjects_json,
status, created_at, invalidated_at

knowledge_index_record:
id, tenant_id, chunk_id, embedding_model_version_id,
embedding_version, index_version, vector_ref,
status, created_at, invalidated_at
```

- 唯一：`document_id + chunk_version + chunk_no`、`chunk_id + embedding_version + index_version`。
- `text_ref/vector_ref` 不得包含其他租户物理路径；检索返回前使用当前用户重新执行 ACL 过滤。

### 12.4 evaluation_set / evaluation_case

```text
evaluation_set:
id, tenant_id, code, name, description, visibility,
version_no, status, resource_version, created_by,
created_at, updated_at, deleted_at

evaluation_case:
id, tenant_id, evaluation_set_id, case_version,
input_json, checks_json, sensitivity,
content_hash, created_by, created_at
```

- 唯一：`tenant_id + code where deleted_at is null`、`evaluation_set_id + case_version + content_hash`。
- EvaluationCase 为不可变样本；修改生成新 `case_version`。
- `sensitivity=CONFIDENTIAL/RESTRICTED` 的样本禁止默认进入外部 Judge 模型。

### 12.5 evaluation_run / evaluation_case_result / user_feedback

```text
evaluation_run:
id, tenant_id, evaluation_set_id, evaluation_set_version,
snapshot_id, model_config_version_id,
judge_model_config_version_id, judge_prompt_version_id,
workflow_id, status, summary_json,
created_by, created_at, started_at, finished_at

evaluation_case_result:
id, tenant_id, evaluation_run_id, evaluation_case_id,
run_id, checks_result_json, score_decimal,
judge_trace_ref, status, error_code, created_at

user_feedback:
id, tenant_id, run_id, user_id, rating,
tags_json, comment_ciphertext_or_ref, created_at
```

- 唯一：`evaluation_run_id + evaluation_case_id`、`run_id + user_id`。
- Judge 结果必须冻结模型版本和 Prompt 版本；不可作为唯一发布门禁。
- 评测样本不直接外键引用生产 `chat_message` 正文；必须经脱敏复制或安全引用。

### 12.6 schedule_definition / schedule_trigger / schedule_run_link

```text
schedule_definition:
id, tenant_id, code, name, deployment_id,
cron, timezone, misfire_policy, concurrency_policy,
input_json, secret_refs_json, temporal_schedule_id,
status, resource_version, created_by,
created_at, updated_at, deleted_at

schedule_trigger:
id, tenant_id, schedule_id, scheduled_at, triggered_at,
trigger_type, status, idempotency_key, created_at

schedule_run_link:
id, tenant_id, schedule_trigger_id, run_id, created_at
```

- 唯一：`tenant_id + code where deleted_at is null`、`schedule_id + scheduled_at + trigger_type`、`schedule_trigger_id`。
- `timezone` 必须为 IANA 时区；并发策略只允许 SKIP/QUEUE/REPLACE。
- REPLACE 先向旧 Run 发出受控取消，不允许直接将旧 Run 标记 CANCELLED。
- `secret_refs_json` 仅存引用，不存解析值。

### 12.7 remote_agent_binding / a2a_call_record

```text
remote_agent_binding:
id, tenant_id, code, name, agent_card_url,
auth_secret_ref, timeout_seconds, allowed_data_classes_json,
card_hash, status, resource_version,
created_by, created_at, updated_at, deleted_at

a2a_call_record:
id, tenant_id, run_id, binding_id, attempt_no,
request_hash, remote_task_id, response_hash,
status, error_code, started_at, finished_at, trace_id
```

- 唯一：`tenant_id + code where deleted_at is null`、`run_id + binding_id + attempt_no`。
- Agent Card 获取必须防 SSRF、禁止私网和重定向绕过，并保存 `card_hash`。
- 所有 A2A 数据外发记录数据分类、策略结果和审计事件；V1 不建 A2A Server/Gateway 表。

## 13. Alembic 策略

1. 每个迁移只处理一个可回滚或可前向修复的逻辑变更。
2. 生产使用 expand → migrate/backfill → contract。
3. 新增非空列先允许 Null 或提供安全默认值，回填后再加约束。
4. 大表索引使用并发创建或运维窗口，迁移脚本不得长时间锁表。
5. Enum 优先使用 varchar + Check，避免 PostgreSQL Enum 难以回滚。
6. 降级脚本不得删除仍被旧版本代码读取的数据；无法安全降级时提供前向修复说明。

## 14. Repository 规则

- 每个租户 Repository 方法第一个业务参数为 `TenantContext`。
- 不提供无 tenant 条件的 `get_by_id`。
- 原生 SQL 必须通过专项审查和跨租户测试。
- 状态更新方法必须显式接收 expected status/resource_version/fencing token。
- 不可变表只暴露 append/get/list。
- 分页使用稳定排序和不透明 Cursor。

## 15. 数据设计验收

- ERD 与本文字段一致。
- 所有外键、唯一约束和部分索引有自动化测试。
- 两租户同 ID/同 code 边界测试通过。
- Run 并发创建只产生一个活动主 Run。
- 100 个并发事件写入无重复序号。
- 旧 fencing token 无法更新 Attempt、Run 或写 Event。
- Alembic 全量升级、空库部署、已有数据升级和前向修复演练通过。
