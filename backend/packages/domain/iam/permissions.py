"""Basic RBAC permission parsing and evaluation."""

from dataclasses import dataclass

ALLOWED_ACTIONS = frozenset(
    {
        "create",
        "read",
        "list",
        "update",
        "delete",
        "publish",
        "rollback",
        "disable",
        "execute",
        "approve",
        "cancel",
        "retry",
        "download",
        "manage",
        "audit",
        "debug",
        "view_sensitive",
    }
)
ALLOWED_RESOURCES = frozenset(
    {
        "tenant",
        "member",
        "role",
        "secret",
        "agent",
        "prompt",
        "skill",
        "mcp",
        "model",
        "runtime_target",
        "sandbox_profile",
        "session",
        "message",
        "run",
        "run_event",
        "approval",
        "artifact",
        "audit",
        "knowledge",
        "schedule",
        "evaluation",
    }
)


@dataclass(frozen=True, slots=True, order=True)
class Permission:
    resource: str
    action: str

    @classmethod
    def parse(cls, value: str) -> "Permission":
        resource, separator, action = value.partition(":")
        if not separator or ":" in action:
            raise ValueError("permission must use resource:action format")
        if resource not in ALLOWED_RESOURCES:
            raise ValueError(f"unsupported permission resource: {resource}")
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"unsupported permission action: {action}")
        return cls(resource=resource, action=action)

    def __str__(self) -> str:
        return f"{self.resource}:{self.action}"


class PermissionSet:
    def __init__(self, permissions: set[Permission] | frozenset[Permission]) -> None:
        self._permissions = frozenset(permissions)

    @classmethod
    def parse(cls, values: list[str] | tuple[str, ...]) -> "PermissionSet":
        parsed = [Permission.parse(value) for value in values]
        if len(parsed) != len(set(parsed)):
            raise ValueError("permissions must be unique")
        return cls(frozenset(parsed))

    def allows(self, resource: str, action: str) -> bool:
        return Permission(resource=resource, action=action) in self._permissions

    def as_strings(self) -> tuple[str, ...]:
        return tuple(str(permission) for permission in sorted(self._permissions))


TENANT_ADMIN_PERMISSIONS = PermissionSet.parse(
    (
        "tenant:read",
        "member:create",
        "member:read",
        "member:list",
        "member:update",
        "member:delete",
        "role:create",
        "role:read",
        "role:list",
        "role:update",
        "role:delete",
        "prompt:create",
        "prompt:read",
        "prompt:list",
        "prompt:update",
        "prompt:delete",
        "prompt:publish",
        "prompt:rollback",
        "prompt:disable",
    )
)
