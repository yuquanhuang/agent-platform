"""Canonical Snapshot diff behavior."""

from packages.domain.public import diff_agent_snapshot_content


def test_diff_is_stable_and_classifies_model_and_runtime_changes() -> None:
    before = {
        "agent": {"runtime_type": "agentscope"},
        "source": {"draft_resource_version": 2},
        "bindings": [
            {
                "resource_type": "model",
                "resource_id": "model-a",
                "version_id": "version-a",
            }
        ],
    }
    after = {
        "bindings": [
            {
                "version_id": "version-b",
                "resource_id": "model-a",
                "resource_type": "model",
            }
        ],
        "source": {"draft_resource_version": 3},
        "agent": {"runtime_type": "codex"},
    }

    changes = diff_agent_snapshot_content(before, after)

    assert [(item.path, item.category) for item in changes] == [
        ("/agent/runtime_type", "runtime"),
        ("/bindings/0/version_id", "model"),
        ("/source/draft_resource_version", "resource_version"),
    ]


def test_diff_redacts_sensitive_values_and_summarizes_large_content() -> None:
    changes = diff_agent_snapshot_content(
        {"secret_ref": "secret://tenant/old", "prompt": "a" * 300},
        {"secret_ref": "plaintext-credential", "prompt": "b" * 300},
    )

    secret_change = next(item for item in changes if item.path == "/secret_ref")
    prompt_change = next(item for item in changes if item.path == "/prompt")
    assert secret_change.category == "secret_reference"
    assert secret_change.sensitive is True
    assert secret_change.before == "secret://tenant/old"
    assert isinstance(secret_change.after, dict)
    assert str(secret_change.after["redacted_hash"]).startswith("sha256:")
    assert prompt_change.before != "a" * 300
    assert prompt_change.after != "b" * 300


def test_first_publication_reports_added_content_without_full_objects() -> None:
    changes = diff_agent_snapshot_content(
        None,
        {"agent": {"name": "Demo"}, "bindings": [{"resource_type": "sandbox"}]},
    )

    assert len(changes) == 1
    assert changes[0].change_type == "added"
    assert changes[0].before is None
    assert changes[0].after is not None
    assert changes[0].after != {
        "agent": {"name": "Demo"},
        "bindings": [{"resource_type": "sandbox"}],
    }
