# Frontend Security Rules

- 禁止把密钥、长期凭证和服务端私密配置打包进前端产物。
- 展示富文本、Markdown、HTML 和第三方内容时必须采用可信消毒策略，禁止直接渲染未校验 HTML。
- 避免在 URL、日志、埋点和本地存储中保存 Token 或敏感业务数据。
- 新窗口链接根据场景设置 `noopener`、`noreferrer`，外部 URL 必须校验允许的协议和来源。
- 请求凭证策略必须与 CORS、CSRF、Cookie SameSite 和服务端鉴权设计一致。
- 上传文件时前端校验只用于体验，服务端仍必须执行完整校验。
- 权限路由和按钮控制不得被视为最终授权。
