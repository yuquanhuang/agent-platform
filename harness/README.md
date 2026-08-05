# Full-stack Monorepo Harness

面向“Python 后端 + Web 前端”同仓库项目的可迁移 AI Coding Harness。默认目录为 `backend/`、`frontend/` 和 `contracts/`，命令可在 `harness/project.json` 中调整。

## 模板内容

```text
AGENTS.md                    全仓公共入口和规则路由
backend/AGENTS.md            Python 后端目录入口
frontend/AGENTS.md           前端目录入口
harness/project.json         路径、工具链和命令配置
harness/rules/common/        跨端规则与契约规则
harness/rules/backend/       Python 后端规则
harness/rules/frontend/      前端工程和设计规则
harness/agents/              后端、前端和全栈任务流程
harness/evals/               分范围完成定义
scripts/harness.py           按 Git 变更范围执行质量门禁
Makefile                     统一命令入口
install.sh                   安全安装脚本
```

## 安装

```bash
./install.sh --dry-run /path/to/monorepo
./install.sh /path/to/monorepo
```

安装程序默认拒绝覆盖已有文件。若目标项目已经存在根或子目录 `AGENTS.md`，应人工合并规则入口。

## 接入步骤

1. 确认目标项目使用 `backend/` 和 `frontend/`；目录不同则修改 `harness/project.json` 和两个子目录规则入口。
2. 根据实际 Python、前端框架和包管理器修改 `harness/project.json` 的命令。
3. 将 `harness/project/design-system.md` 填写为真实设计 token 和组件规范。
4. 确认 OpenAPI、JSON Schema 或事件 Schema 的唯一来源及生成策略。
5. 执行 `make harness-dry-run`，检查将要运行的命令。
6. 运行 `make check-all`，确认两端工具链可用。

## 变更路由

- 仅修改 `backend/**`：运行后端门禁。
- 仅修改 `frontend/**`：运行前端门禁。
- 修改 `contracts/**`：运行契约门禁以及前后端门禁。
- 修改根构建配置、Harness 或共享基础设施：运行前后端完整门禁。
- 可通过 `--scope` 显式覆盖自动识别结果。

## 默认技术假设

模板默认示例使用 Python、pytest、Black/Ruff 和 pnpm 脚本，但这些只是可编辑默认值，不代表项目必须采用该技术栈。接入时以目标项目现有工具链为准。
