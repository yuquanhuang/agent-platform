# ADR-002：Agent 平台前端 UI 组件库采用 Element Plus

> ADR 状态：已接受  
> ADR 版本：V1.1  
> 生效基线：`agent-platform-v1-dev-baseline-2026-08-r5`  
> 决策日期：2026-08-05

## 1. 决策

V1 Vue 前端统一使用 Element Plus。组件库用于布局、表单、表格、分页、弹窗、通知和基础交互，不改变 OpenAPI、RunEvent、权限或状态所有权契约。

## 2. 约束

- 通过 `frontend/src/ui/` 统一封装主题、表单、表格和反馈组件，业务页面避免直接散落全局配置。
- 颜色、间距、圆角、字号和状态色使用项目 design token；不得直接复制 Element Plus 默认值形成第二套设计系统。
- 表单校验、权限判断和服务端状态仍由项目 Schema、权限守卫和 `@tanstack/vue-query` 管理，不写入组件私有状态。
- 按需引入组件和样式；生产构建必须检查体积、无障碍、暗色主题和国际化边界。
- 更换组件库必须新增 ADR，并提供页面、主题、测试和构建迁移方案。

## 3. 验收

Epic 0 至少完成应用壳、导航、表单、表格、空状态和错误提示的统一封装，并通过 ESLint、`vue-tsc`、Vitest、Vue Test Utils 和生产构建。
