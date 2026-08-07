# syntax=docker/dockerfile:1.7

# Existing CI must supply approved OCI references including manifest digests,
# for example: registry.example/python:3.12-slim@sha256:<digest>.
ARG UV_IMAGE
ARG PYTHON_IMAGE

FROM ${UV_IMAGE} AS uv-bin

FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv-bin /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/README.md backend/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
      --frozen \
      --no-dev \
      --no-install-project \
      --package backend

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="agent-platform-runtime-agentscope" \
      org.opencontainers.image.description="AgentScope runtime worker for Agent Platform"

ENV HOME=/tmp/runtime-home \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/backend \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /tmp/runtime-home && chown 65532:65532 /tmp/runtime-home

WORKDIR /app/backend

COPY --from=builder --chown=65532:65532 /workspace/.venv /app/.venv
COPY --chown=65532:65532 backend/apps ./apps
COPY --chown=65532:65532 backend/packages ./packages

USER 65532:65532

# The entrypoint remains fail-closed until the dedicated AgentRun runtime task
# implements and wires the production worker.
CMD ["python", "-m", "apps.runtime_worker_agentscope.main"]
