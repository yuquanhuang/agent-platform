from pathlib import Path

import yaml

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


def test_worker_metrics_services_and_scrape_contract_are_bounded() -> None:
    kustomization = (KUBERNETES_ROOT / "kustomization.yaml").read_text(encoding="utf-8")
    metrics_services = list(
        yaml.safe_load_all(
            (KUBERNETES_ROOT / "metrics-services.yaml").read_text(encoding="utf-8")
        )
    )
    assert "metrics-services.yaml" in kustomization
    assert "prometheus-rules.yaml" in kustomization
    services = {
        document["metadata"]["name"]: document
        for document in metrics_services
        if document["kind"] == "Service"
    }
    assert set(services) == {
        "agent-platform-event-worker-metrics",
        "agent-platform-reconciliation-worker-metrics",
    }
    for service in services.values():
        assert service["spec"]["type"] == "ClusterIP"
        assert service["spec"]["ports"] == [
            {
                "name": "metrics",
                "port": 9090,
                "targetPort": "metrics",
                "protocol": "TCP",
            }
        ]
    monitor = next(
        document
        for document in metrics_services
        if document["kind"] == "ServiceMonitor"
    )
    assert monitor["spec"]["endpoints"] == [
        {
            "port": "metrics",
            "path": "/metrics",
            "interval": "15s",
            "scrapeTimeout": "10s",
        }
    ]


def test_worker_metrics_deployments_and_rules_keep_operational_boundaries() -> None:
    for filename in (
        "event-worker-deployment.yaml",
        "reconciliation-worker-deployment.yaml",
    ):
        documents = list(
            yaml.safe_load_all((KUBERNETES_ROOT / filename).read_text(encoding="utf-8"))
        )
        container = documents[0]["spec"]["template"]["spec"]["containers"][0]
        assert container["ports"] == [
            {"name": "metrics", "containerPort": 9090, "protocol": "TCP"}
        ]

    rules = list(
        yaml.safe_load_all(
            (KUBERNETES_ROOT / "prometheus-rules.yaml").read_text(encoding="utf-8")
        )
    )
    runbook = (KUBERNETES_ROOT / "runbooks" / "agent-platform-workers.md").read_text(
        encoding="utf-8"
    )
    runbook_anchors = {
        heading.removeprefix("## ")
        .strip()
        .lower()
        .replace(" / ", "--")
        .replace(" ", "-")
        for heading in runbook.splitlines()
        if heading.startswith("## ")
    }
    alerts = [
        rule
        for document in rules
        if document["kind"] == "PrometheusRule"
        for group in document["spec"]["groups"]
        for rule in group["rules"]
        if "alert" in rule
    ]
    assert alerts
    for alert in alerts:
        labels = alert["labels"]
        annotations = alert["annotations"]
        assert labels["severity"] in {"warning", "critical"}
        assert labels["owner"]
        assert alert["for"]
        runbook_path = annotations["runbook_path"]
        relative_path, _, anchor = runbook_path.partition("#")
        assert (
            relative_path
            == "infra/kubernetes/agent-platform/runbooks/agent-platform-workers.md"
        )
        assert anchor
        assert anchor in runbook_anchors
        assert "recovery" in annotations

    rules_text = (KUBERNETES_ROOT / "prometheus-rules.yaml").read_text(encoding="utf-8")
    assert "agent_platform:run_event_batch_duration_p95_seconds > 0.1" in rules_text
    assert "agent_platform:sse_visibility_delay_p95_seconds > 0.5" in rules_text
    assert 'outcome="rpc_error"' in rules_text
    assert "authorization_rejected|store_rejected|store_failure" in rules_text
    assert "AgentPlatformSseDeliveryDegraded" in rules_text
    assert rules_text.count("absent(up{") == 1
    assert "example.invalid" not in rules_text
