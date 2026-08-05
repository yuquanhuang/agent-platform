# ADR-001：Agent 平台前端框架采用 Vue 3

> ADR 状态：已接受  
> ADR 版本：V1.3  
> 生效基线：`agent-platform-v1-dev-baseline-2026-08-r5`  
> 决策日期：2026-08-04

## 1. 背景

冻结基线 `2026-08-R1` 曾将 React + Vite 作为 V1 前端实现选择。当前项目决定统一采用 Vue 技术栈，需要在进入 AI Coding 开发前消除工程基线、架构、交互、测试和任务包之间的框架冲突。

本次变更只替换 Web 前端实现技术栈，不改变产品范围、后端 API、OpenAPI、RunSpec、RunEvent、AG-UI、OIDC、权限、Temporal、Sandbox、Workspace 或数据库契约。

## 2. 决策

V1 前端固定使用：

```text
Vue 3 Composition API
TypeScript
Vite
Vue Router 4
Pinia
@tanstack/vue-query
OpenAPI generated TypeScript Client
Vitest + Vue Test Utils
Playwright
```

约束如下：

1. 组件默认使用 `<script setup lang="ts">` 和 Composition API。
2. Vue Router 统一管理路由、权限守卫和 Feature Flag。
3. Pinia 只管理身份上下文、UI 会话和跨页面客户端状态。
4. 服务端实体、缓存、重试、失效和请求竞态由 `@tanstack/vue-query` 管理，禁止复制到 Pinia。
5. API Client 和 DTO 从 OpenAPI 生成；RunEvent Type 从 JSON Schema 生成或由同一源码导出。
6. SSE/AG-UI 使用独立 service/composable 和可测试状态归并函数，组件不是运行事实来源。
7. 前端 CI 至少执行 Prettier、ESLint、`vue-tsc --noEmit`、Vitest、Vue Test Utils 测试和生产构建；关键流程使用 Playwright。
8. UI 组件库和设计 token 通过项目统一入口管理；当前选择见 [ADR-002：前端 UI 组件库采用 Element Plus](./ADR-002-前端UI组件库采用ElementPlus.md)，不得改变本文的数据和状态边界。

## 3. 状态所有权

| 状态类型 | 所有者 | 示例 |
|---|---|---|
| 服务端资源与列表 | `@tanstack/vue-query` | Agent、Prompt、Skill、Run 列表 |
| 用户身份与当前租户 | Pinia | 当前用户、active tenant、membership version |
| 页面局部状态 | Vue component/composable | Dialog、Tab、临时输入 |
| Run 流式投影 | 独立 Run Store + 纯归并函数 | message block、tool timeline、approval card |
| 运行事实 | 服务端 RunEvent/Event Store | sequence、终态、历史回放 |

Query Key 和 Pinia Store 必须包含必要的 tenant、session、run 维度；切换租户后必须清理或隔离旧缓存。

## 4. 保持不变的契约

- URL 路由、页面范围和权限动作。
- OIDC Authorization Code + PKCE。
- `If-Match`、ETag、`Idempotency-Key` 和 Error.code。
- OpenAPI 3.1 以及生成 TypeScript Client 的单一来源。
- RunEvent `(run_id, sequence_no)` 顺序和 SSE Last-Event-ID 恢复规则。
- AG-UI 仅作为前端出口适配，内部仍以 RunEvent 为事实模型。
- 后端 Python、Temporal、AgentScope、Codex ACP 和 Sandbox 设计。

## 5. 被替代方案

React、React Router 和 React 专属状态/组件实现不再是 V1 开发基线。R1 中的相关记录只作为历史变更依据，不得作为新代码实现输入。

本 ADR 不否定 React 的技术可行性；选择 Vue 3 的目的在于建立单一、明确且可验证的工程基线，减少 AI Coding 期间的框架分歧。

## 6. 迁移和验收

- 当前尚未形成需要兼容的正式 React 生产前端，因此不建设 React/Vue 双运行和组件级迁移层。
- 不允许同时引入 React 和 Vue 微前端作为过渡方案。
- 所有前端任务必须引用本 ADR、ADR-002 和当前 R5 基线。
- 全目录不得保留未标记为历史的 React 固定选型描述。
- OpenAPI 和 Schema 内容不因框架替换而变化，但必须重新校验生成 TypeScript Client、RunEvent Type 和基线 SHA-256。
- Vue 脚手架、路由权限、Query Cache、Pinia 状态、SSE 恢复和生产构建全部通过后，框架替换验收完成。

## 7. 回滚

在大规模页面开发前若 Vue 方案验证失败，必须创建新的 ADR 和冻结基线修订；不得直接恢复 R1 文件或在单个任务中改回 React。
