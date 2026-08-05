# Vue 3 Stack Rules

仅当目标项目采用 Vue 3 时适用。若项目采用其他框架，应替换本文件并从 `frontend/AGENTS.md` 移除引用。

- 优先使用 Composition API 和项目既有 `<script setup>`/普通 setup 约定，不在同一模块混用多套风格。
- 路由模式服从项目既有部署方案；使用 history 模式时，服务端必须把未知前端路由回退到 `index.html`。
- 读取当前路由使用 Vue Router API，不直接依赖 `location.hash` 实现业务状态。
- Pinia 只保存需要跨组件或跨路由共享的状态，页面局部状态保留在组件或 composable。
- Element Plus 主题通过统一 token 或主题入口映射，不在业务组件内重复覆盖全局变量。
- Sass/CSS 变量和 TypeScript 布局常量应具有明确单一来源，避免三套 token 独立漂移。
- 页签、多实例路由和系统参数过滤必须复用项目统一策略，不在页面内自行拼装唯一键。
- Vue 组件测试关注渲染结果、用户操作和事件，不直接断言组件私有实现。
