"""Basic RBAC permission domain tests."""

import pytest

from packages.domain.public import TENANT_ADMIN_PERMISSIONS, Permission, PermissionSet


def test_permission_uses_frozen_resource_action_format() -> None:
    permission = Permission.parse("member:create")

    assert permission.resource == "member"
    assert permission.action == "create"
    assert str(permission) == "member:create"


@pytest.mark.parametrize(
    "value",
    ["member", "member:create:extra", "unknown:create", "member:unknown"],
)
def test_permission_rejects_unknown_or_malformed_values(value: str) -> None:
    with pytest.raises(ValueError):
        Permission.parse(value)


def test_permission_set_is_unique_and_evaluates_exact_action() -> None:
    permissions = PermissionSet.parse(("member:create", "member:read"))

    assert permissions.allows("member", "create") is True
    assert permissions.allows("member", "delete") is False
    assert permissions.as_strings() == ("member:create", "member:read")


def test_permission_set_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="unique"):
        PermissionSet.parse(("member:create", "member:create"))


def test_tenant_admin_includes_agent_draft_permissions() -> None:
    for action in ("create", "read", "list", "update", "delete", "disable", "publish"):
        assert TENANT_ADMIN_PERMISSIONS.allows("agent", action)


def test_tenant_admin_includes_session_management_permissions() -> None:
    for action in ("create", "read", "list", "update", "delete"):
        assert TENANT_ADMIN_PERMISSIONS.allows("session", action)


def test_tenant_admin_includes_message_history_permissions() -> None:
    for action in ("read", "list"):
        assert TENANT_ADMIN_PERMISSIONS.allows("message", action)


def test_tenant_admin_includes_run_creation_and_history_permissions() -> None:
    for action in ("create", "read", "list", "cancel", "retry"):
        assert TENANT_ADMIN_PERMISSIONS.allows("run", action)
