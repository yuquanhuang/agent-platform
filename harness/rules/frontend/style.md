# Frontend Style Rules

- 默认使用 TypeScript；禁止无说明地使用 `any`、忽略类型错误或关闭严格校验。
- 格式化、lint 和导入顺序服从目标项目现有 Prettier/ESLint/Biome 配置。
- 组件、hooks/composables、store、service 和工具函数命名应表达业务含义。
- 复杂条件和重复逻辑提取为可测试的函数，不在模板中堆积难以验证的表达式。
- 优先复用已有组件、指令、hooks、设计 token 和工具，不复制相似实现。
- 代码注释解释约束和原因，不复述语法表面行为。
- 不遗留调试日志、临时 mock、测试账号或硬编码环境地址。
