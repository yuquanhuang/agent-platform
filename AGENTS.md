# Full-stack Monorepo AI Coding Guide

## 适用范围

本文件适用于整个前后端同仓库项目。开始任何任务前必须阅读：

- `harness/project.json`
- `harness/rules/common/constraints.md`
- `harness/rules/common/security.md`
- `harness/rules/common/recommendations.md`

根据修改范围继续读取：

- 修改 `backend/**`：读取 `backend/AGENTS.md`。
- 修改 `frontend/**`：读取 `frontend/AGENTS.md`。
- 修改 API、事件或共享类型：同时读取前后端规则和 `harness/rules/common/contracts.md`。
- 跨端功能：读取 `harness/agents/fullstack.md`。

## 全仓工作流

1. 先确定需求边界、受影响端、共享契约和已有测试。
2. 只修改完成需求所必需的文件，不夹带无关重构、依赖升级或全仓格式化。
3. 契约优先：先确认接口和数据模型，再分别实现后端与前端。
4. 后端变更执行后端门禁，前端变更执行前端门禁，共享契约变更同时执行两端门禁。
5. 完成后运行 `make check`；发布前或高风险变更运行 `make check-all`。
6. 交付时说明行为变化、契约变化、迁移要求、验证结果和剩余风险。

## 规则优先级

1. 用户当前明确要求。
2. 距离目标文件最近的 `AGENTS.md`。
3. 本文件和 `harness/rules/common/`。
4. 推荐规则。

发生冲突时不得静默选择，必须说明冲突和处理依据。

## 标准命令

```bash
make check
make check-all
make backend-check
make frontend-check
make contract-check
```

## 完成定义

根据修改范围检查 `harness/evals/` 中的 common、backend、frontend 和 integration 清单。不得声称未实际执行的验证已经通过。
