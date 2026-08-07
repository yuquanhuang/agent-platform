from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPOSITORY_ROOT / "infra" / "images" / "runtime-agentscope" / "Dockerfile"


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
