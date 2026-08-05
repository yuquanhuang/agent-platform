# Vue Admin Design System Example

> 本文件来自原前端规约，仅作为“Vue + Element Plus 中后台”设计系统示例。迁移到新项目时必须替换产品名称、`--tp-*` token 前缀、色值、布局尺寸和品牌约束，不能直接当作通用规则。

## 设计目标

Techplayweb 采用参考 Vben Admin Ele 的浅色后台风格：白色侧边栏、白色顶栏、浅灰蓝页面底色、清晰边界和高饱和主题蓝。界面应服务于中后台高频操作，保持轻量、克制、可扫描，不做营销式大面积装饰。

## 色彩规范

主色参考 Vben token `--primary: 212 100% 45%`，落地为 `#006fe6`。

| 语义 | Token | 值 | 用途 |
| --- | --- | --- | --- |
| 品牌主色 | `--tp-color-primary` | `#006fe6` | 主按钮、激活菜单、链接、进度 |
| 主色 hover | `--tp-color-primary-hover` | `#0f7df0` | 按钮 hover、菜单 hover 强调 |
| 主色 active | `--tp-color-primary-active` | `#0057b8` | 按下态、深强调 |
| 主色浅底 | `--tp-color-primary-bg` | `#e8f2ff` | 菜单选中底、弱强调底 |
| 页面底色 | `--tp-color-bg-page` | `#f1f5f9` | 主内容区域 |
| 容器底色 | `--tp-color-bg-container` | `#ffffff` | 顶栏、侧栏、卡片、弹窗 |
| 浮层底色 | `--tp-color-bg-overlay` | `#ffffff` | Dialog、Drawer、Dropdown |
| 主文字 | `--tp-color-text-primary` | `#1f2937` | 标题、重要正文 |
| 常规文字 | `--tp-color-text-regular` | `#4b5563` | 正文、菜单 |
| 次级文字 | `--tp-color-text-secondary` | `#6b7280` | 辅助说明 |
| 占位文字 | `--tp-color-text-placeholder` | `#9ca3af` | placeholder、弱提示 |
| 边框 | `--tp-color-border` | `#e5e7eb` | 容器、表格、分割线 |
| 浅边框 | `--tp-color-border-light` | `#edf0f5` | 顶栏、标签栏弱分割 |

状态色使用 Element Plus 语义，但必须通过 token 映射，不在组件内硬编码。

## 字体与字号

字体族统一使用 `--tp-font-family-base`，优先系统 UI 字体：

`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", "PingFang SC", "Microsoft YaHei", sans-serif`

| 语义 | Token | 值 |
| --- | --- | --- |
| 超小辅助 | `--tp-font-size-xs` | `12px` |
| 小号文本 | `--tp-font-size-sm` | `13px` |
| 正文/菜单/按钮 | `--tp-font-size-base` | `14px` |
| 次级标题 | `--tp-font-size-md` | `15px` |
| 页面标题 | `--tp-font-size-lg` | `16px` |
| 弹窗标题 | `--tp-font-size-xl` | `18px` |
| 品牌标题 | `--tp-font-size-brand` | `18px` |

字体粗细使用 token：常规 `--tp-font-weight-regular: 400`，中等 `--tp-font-weight-medium: 500`，强调 `--tp-font-weight-semibold: 600`，不在组件内直接写 `bold`。

## 布局规范

- 侧边栏：白色背景，右侧 `1px` 浅边框；普通菜单使用常规文字色，激活态使用主色文字和浅蓝底。
- 顶栏：白色背景，底部浅边框，高度使用 `--tp-size-header-height: 55px`。
- 标签栏：白色背景，激活态主色文字加底部主色线。
- 内容区：页面背景使用浅灰蓝，常用内边距使用 `--tp-space-content-x` 与 `--tp-space-content-y`。
- 卡片：白底、浅边框、轻阴影、圆角 `6px`，只用于具体内容容器，不嵌套卡片。

## 组件规范

### Button

- 默认字号 `--tp-font-size-base`。
- 高度、圆角走 Element Plus token 映射。
- 主按钮背景使用 `--tp-color-primary`，hover 使用 `--tp-color-primary-hover`，active 使用 `--tp-color-primary-active`。

### Table

- 表头字号 `--tp-font-size-md`，字重 `--tp-font-weight-semibold`。
- 行文字字号 `--tp-font-size-base`。
- 表头背景使用 `--tp-color-fill-light`，边框使用 `--tp-color-border`。

### Dialog / Drawer

- 背景使用 `--tp-color-bg-overlay`。
- 标题字号 `--tp-font-size-xl`，字重 `--tp-font-weight-semibold`。
- Header/Footer 只保留浅边框分割，不使用重阴影。

### Menu

- 字号 `--tp-font-size-base`。
- hover 背景 `--tp-color-menu-hover-bg`。
- active 背景 `--tp-color-menu-active-bg`，文字和图标使用 `--tp-color-menu-active-text`。

## Token 约束

- 新增颜色、字体、字号、圆角、阴影、布局高度、常用间距时，必须先补 token。
- 组件内只能引用 `--tp-*` 或映射后的 `--el-*` token。
- 禁止在业务组件中新增散落的 `#xxxxxx`、`rgb(...)`、固定 `font-size`、固定 `border-radius`、固定 `box-shadow`。
