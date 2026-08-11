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
        "model_provider",
        "model_config",
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
        "agent:create",
        "agent:read",
        "agent:list",
        "agent:update",
        "agent:delete",
        "agent:disable",
        "agent:publish",
        "prompt:create",
        "prompt:read",
        "prompt:list",
        "prompt:update",
        "prompt:delete",
        "prompt:publish",
        "prompt:rollback",
        "prompt:disable",
        "skill:create",
        "skill:read",
        "skill:list",
        "skill:update",
        "skill:delete",
        "skill:publish",
        "skill:rollback",
        "skill:disable",
        "mcp:create",
        "mcp:read",
        "mcp:list",
        "mcp:update",
        "mcp:delete",
        "mcp:publish",
        "mcp:rollback",
        "mcp:disable",
        "mcp:execute",
        "model_provider:create",
        "model_provider:read",
        "model_provider:list",
        "model_provider:update",
        "model_provider:delete",
        "model_provider:disable",
        "model_provider:execute",
        "model_config:create",
        "model_config:read",
        "model_config:list",
        "model_config:update",
        "model_config:delete",
        "model_config:publish",
        "model_config:rollback",
        "model_config:disable",
        "session:create",
        "session:read",
        "session:list",
        "session:update",
        "session:delete",
        "message:read",
        "message:list",
        "run:create",
        "run:read",
        "run:list",
        "run:cancel",
        "run:retry",
        "approval:read",
        "approval:list",
        "approval:approve",
        "audit:list",
        "artifact:create",
        "artifact:read",
        "artifact:download",
        "artifact:delete",
    )
)
