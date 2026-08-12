# syntax=docker/dockerfile:1.7

# Existing CI supplies approved, digest-pinned OCI references.
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

LABEL org.opencontainers.image.title="agent-platform-backend" \
      org.opencontainers.image.description="Shared API and worker image for Agent Platform"

ENV HOME=/tmp/agent-platform-home \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/backend \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /tmp/agent-platform-home && chown 65532:65532 /tmp/agent-platform-home

WORKDIR /app/backend

COPY --from=builder --chown=65532:65532 /workspace/.venv /app/.venv
COPY --chown=65532:65532 backend/alembic.ini ./alembic.ini
COPY --chown=65532:65532 backend/apps ./apps
COPY --chown=65532:65532 backend/migrations ./migrations
COPY --chown=65532:65532 backend/packages ./packages

USER 65532:65532

CMD ["python", "-m", "apps.api.main"]
