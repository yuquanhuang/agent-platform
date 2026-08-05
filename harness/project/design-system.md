# Project Design System

本文件记录 Agent 平台的项目级设计基线。未明确的品牌视觉不得由单个页面自行扩展。

## 基础信息

- 产品/品牌名称：`agent平台`
- 前端框架：`Vue 3 + TypeScript + Vite`
- 组件库：`Element Plus`
- 设计稿或参考系统：当前以产品交互契约和 Element Plus 无障碍模式为准
- Token 源文件：`frontend/src/styles/tokens.css`
- 主题切换方式：Epic 0 仅提供浅色语义 Token；暗色主题需单独验收后启用

## 设计目标

- 面向桌面端的中高信息密度管理平台，同时保证窄屏下导航和核心状态可用。
- 所有状态必须同时使用文字/图标语义，不只依赖颜色。
- 交互元素必须键盘可达、焦点可见；避免无说明动画、纯装饰渐变和页面级硬编码颜色。
- 未确认品牌色前，项目 Token 映射 Element Plus 语义变量，不创建平行色板。

## Token 清单

| 类别 | Token 来源 | 约束 |
| --- | --- | --- |
| 品牌色与状态色 | Element Plus 语义变量，经 `tokens.css` 映射 | 业务组件不得硬编码重复色值 |
| 文字与字体 | 系统 UI 字体栈、Element Plus 字号层级 | 正文最小 14px，状态文本必须可读 |
| 间距与布局 | `--ap-space-*` | 使用 4/8/12/16/24/32px 尺度 |
| 圆角与阴影 | `--ap-radius-*`、`--ap-shadow-*` | 页面容器与浮层使用统一层级 |
| 层级与动画 | Element Plus 层级；`--ap-motion-fast` | 尊重 `prefers-reduced-motion` |

## 布局规范

- 页面框架：顶栏 + 可收起侧栏 + 主内容区。
- 侧边栏/顶栏：桌面侧栏 240px，收起 72px；顶栏 56px。
- 内容最大宽度和页面边距：内容最大 1440px，页面边距 24px；窄屏 16px。
- 响应式断点：768px 以下使用窄屏布局，不依赖 hover 才能完成操作。
- 表格、表单和详情页密度：默认 Element Plus `default`，高密度表格需页面级明确说明。

## 组件规范

- Button：主操作每个区域最多一个 primary；危险操作使用二次确认并由后端鉴权。
- Form：统一标签、错误文案和提交状态；服务端错误不得被客户端校验覆盖。
- Table/List：服务端分页和稳定排序；loading、empty、partial data 分开展示。
- Dialog/Drawer：短确认使用 Dialog，复杂编辑使用 Drawer/页面；关闭前处理未保存状态。
- Navigation：由 Vue Router 统一管理；未启用能力不展示可点击入口。
- Feedback：使用统一 Alert/Message/Result 封装，错误不得只写入控制台。
- Empty/Error State：明确原因、影响和可执行下一步；依赖不可用提供安全重试。

## 视觉验收

- 关键页面至少检查默认、loading、empty、error、disabled 和窄屏状态。
- 修改全局 token 或基础组件时记录受影响页面并执行视觉回归。
- 设计稿与现有组件冲突时先确认规范归属，不在页面中临时覆盖全局样式。
