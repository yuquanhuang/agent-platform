from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPOSITORY_ROOT / "infra" / "images" / "runtime-agentscope" / "Dockerfile"
BACKEND_DOCKERFILE = REPOSITORY_ROOT / "infra" / "images" / "backend" / "Dockerfile"
KUBERNETES_ROOT = REPOSITORY_ROOT / "infra" / "kubernetes" / "agent-platform"


def test_agentscope_runtime_image_keeps_supply_chain_inputs_controlled() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG PYTHON_IMAGE" in content
    assert "ARG UV_IMAGE" in content
    assert "FROM ${PYTHON_IMAGE}" in content
    assert "FROM ${UV_IMAGE}" in content
    assert "uv sync" in content
    assert "--frozen" in content
    assert "--no-dev" in content
    assert "--package backend" in content
    assert "pip install" not in content
    assert "curl " not in content


def test_agentscope_runtime_image_is_non_root_and_keeps_worker_fail_closed() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")

    assert "USER 65532:65532" in content
    assert 'CMD ["python", "-m", "apps.runtime_worker_agentscope.main"]' in content

    entrypoint = (
        REPOSITORY_ROOT / "backend" / "apps" / "runtime_worker_agentscope" / "main.py"
    ).read_text(encoding="utf-8")
    assert "unavailable_process(ProcessName.RUNTIME_WORKER_AGENTSCOPE)" in entrypoint


def test_backend_image_uses_locked_shared_environment_and_non_root_runtime() -> None:
    content = BACKEND_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG PYTHON_IMAGE" in content
    assert "ARG UV_IMAGE" in content
    assert "FROM ${PYTHON_IMAGE}" in content
    assert "FROM ${UV_IMAGE}" in content
    assert "uv sync" in content
    assert "--frozen" in content
    assert "--no-dev" in content
    assert "--package backend" in content
    assert "USER 65532:65532" in content
    assert "pip install" not in content
    assert "curl " not in content


def test_kubernetes_base_activates_only_composed_worker_and_redacts_downloads() -> None:
    kustomization = (KUBERNETES_ROOT / "kustomization.yaml").read_text(encoding="utf-8")
    deployment = (KUBERNETES_ROOT / "event-worker-deployment.yaml").read_text(
        encoding="utf-8"
    )
    ingress = (KUBERNETES_ROOT / "download-ingress.example.yaml").read_text(
        encoding="utf-8"
    )
    reconciliation = (
        KUBERNETES_ROOT / "reconciliation-worker-deployment.yaml"
    ).read_text(encoding="utf-8")

    assert "event-worker-deployment.yaml" in kustomization
    assert "download-ingress.example.yaml" not in kustomization
    assert "runtime-worker" not in kustomization
    assert "reconciliation-worker" not in kustomization
    assert "secretKeyRef:" in deployment
    assert "AP_SECRET_DATABASE_DSN" in deployment
    assert "apps.reconciliation_worker.main" in reconciliation
    assert "AP_SECRET_INTERNAL_SERVICE_TOKEN_SIGNING_KEY" in reconciliation
    assert "AP_SECRET_EXECUTION_TICKET_KEY" in reconciliation
    assert 'nginx.ingress.kubernetes.io/enable-access-log: "false"' in ingress
    assert "$request_uri" not in ingress
