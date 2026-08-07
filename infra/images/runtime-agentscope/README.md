# AgentScope Runtime Image

该目录只定义 AgentScope Runtime 的受控 OCI 镜像输入。镜像构建、SBOM、漏洞与许可证扫描、签名、推送和 manifest digest 回填由现有 CI/制品仓库负责；仓库不建立平行制品流水线。

CI 构建时必须：

1. 使用仓库根目录作为 build context，并指定本目录的 `Dockerfile`。
2. 通过 `PYTHON_IMAGE` 和 `UV_IMAGE` 传入企业批准的完整 OCI 引用，两个引用都必须包含 `@sha256:<64 hex>` manifest digest。
3. 在构建前执行 `make check-all`，构建后生成 SBOM并执行漏洞、许可证和 Secret 扫描。
4. 对推送后的镜像签名，从 Registry 获取可拉取的 manifest digest；不得使用本地 image ID 或可变标签回填基线。
5. 将代码 revision、基础镜像 digest、SBOM、扫描结果、签名证明和最终镜像 digest关联到同一发布记录。

参考构建命令：

```bash
docker buildx build \
  --file infra/images/runtime-agentscope/Dockerfile \
  --build-arg PYTHON_IMAGE="${APPROVED_PYTHON_IMAGE_AT_DIGEST}" \
  --build-arg UV_IMAGE="${APPROVED_UV_IMAGE_AT_DIGEST}" \
  --tag "${RUNTIME_IMAGE_REPOSITORY}:${RELEASE_VERSION}" \
  --push \
  .
```

当前 `runtime-worker-agentscope` 入口仍故障关闭。CI 可以构建和扫描候选镜像，但在后续 AgentRun Runtime 任务完成前不得部署为生产 Worker。
