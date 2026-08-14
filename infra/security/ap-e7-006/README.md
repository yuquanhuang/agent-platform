# AP-E7-006 安全验收

本目录提供生产 Sandbox 和供应链证据的静态验收输入，不会创建 Sandbox Pod，也不会调用
Registry、签名服务或漏洞数据库。

```bash
cd backend
../.venv/bin/python3 -m apps.security_acceptance.main \
  ../infra/security/ap-e7-006/plan.yaml \
  --output ../artifacts/ap-e7-006/security-baseline
```

示例计划固定为 `mode: dry_run`，即使静态检查全部通过也只输出 `dry_run`，不能作为生产
上线证据。切换到 `production` 前必须使用现有 CI/制品仓库的真实 Registry digest、SBOM、
漏洞/许可证/Secret/恶意代码报告、签名和 provenance，并替换所有 `example.invalid` 与
`replace-with-*` 占位内容。生产不允许对 critical/high/known-exploitable 漏洞设置任何
限时风险例外，存在 `risk_exceptions` 时验收直接 fail-closed。

Sandbox 清单要求 gVisor RuntimeClass、独立 ServiceAccount、禁用 Token 自动挂载、禁止宿主
namespace/hostPath/PVC、只读根文件系统、drop ALL、RuntimeDefault seccomp、不可变镜像 digest
以及 CPU/内存/临时磁盘限制。默认网络模式为完全禁出；需要联网时必须切换为受控 egress
proxy，并由 NetworkPolicy 只允许 selector 定位的代理 Pod，不能直接放行任意 IP。

真实容器逃逸、fork bomb、磁盘/内存耗尽、DNS rebinding、重定向、宿主资源读取、Secret
泄漏和 Prompt Injection 越权仍必须在隔离 Kubernetes 安全环境执行；静态清单检查不能替代
这些动态阻断测试。
