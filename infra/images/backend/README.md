# Backend image

现有 CI/制品仓库负责传入经过批准且带 digest 的 `UV_IMAGE`、`PYTHON_IMAGE`，构建、扫描并回填最终 Registry digest。仓库保留 Dockerfile 作为可审计构建输入，不在源码中硬编码 Registry 或凭证。

示例：

```bash
docker build \
  --build-arg UV_IMAGE=registry.example/uv@sha256:<digest> \
  --build-arg PYTHON_IMAGE=registry.example/python:3.12-slim@sha256:<digest> \
  -f infra/images/backend/Dockerfile \
  -t registry.example/agent-platform/backend:<version> .
```

同一镜像由 Kubernetes 覆盖 `command` 启动 API、Event Worker 或其他已完成生产装配的进程。尚处于 fail-closed 的进程不得仅通过覆盖命令启用。
