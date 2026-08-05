# Frontend AI Coding Guide

本文件适用于 `frontend/**`。开始编码前必须阅读：

- `../harness/rules/frontend/architecture.md`
- `../harness/rules/frontend/style.md`
- `../harness/rules/frontend/testing.md`
- `../harness/rules/frontend/security.md`
- `../harness/rules/frontend/design.md`
- `../harness/agents/frontend.md`

如果项目采用 Vue 3，还必须阅读：

- `../harness/rules/frontend/vue3-stack.md`

要求：

- 先确认项目实际框架、包管理器、组件库、状态管理和构建工具。
- 优先复用已有组件、设计 token、API Client 和类型定义。
- 权限控制不能只依赖隐藏界面；服务端必须执行真实鉴权。
- API 类型优先从共享契约生成，不维护含义重复的手写 DTO。
- 完成后至少运行 `make frontend-check` 或等价的 lint、typecheck、test 和 build。
