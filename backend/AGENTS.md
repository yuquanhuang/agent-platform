# Backend AI Coding Guide

本文件适用于 `backend/**`。开始编码前必须阅读：

- `../harness/rules/backend/architecture.md`
- `../harness/rules/backend/style.md`
- `../harness/rules/backend/testing.md`
- `../harness/rules/backend/security.md`
- `../harness/agents/python-backend.md`

要求：

- 先确认项目实际 Python 版本、依赖管理方式、应用入口和模块边界。
- API、任务入口不得直接堆积业务编排、SQL 或事务控制。
- 外部依赖在单元测试中必须通过明确边界隔离。
- API 或事件模型变化必须同步更新契约，并验证前端消费者。
- 完成后至少运行 `make backend-check` 或等价的相关测试和静态检查。
