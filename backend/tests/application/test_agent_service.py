"""Agent Draft application authorization and binding policy tests."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    AgentManagementService,
    AgentRegistry,
    RequestMetadata,
)
from packages.contracts.generated.core_models import AgentUpdateRequest
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import AgentBindingRecord, AgentRecord, TenantAccess

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
MODEL_ID = UUID("44444444-4444-4444-8444-444444444444")
MODEL_VERSION_ID = UUID("55555555-5555-4555-8555-555555555555")
PROMPT_ID = UUID("66666666-6666-4666-8666-666666666666")
PROMPT_VERSION_ID = UUID("77777777-7777-4777-8777-777777777777")
NOW = datetime(2026, 8, 7, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-agent", trace_id="trace-agent")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="agent-admin",
        display_name="Agent Admin",
        platform_roles=frozenset(),
        auth_time=NOW,
    )


def access(*permissions: str) -> TenantAccess:
    return TenantAccess(
        context=TenantContext(
            tenant_id=str(TENANT_ID),
            subject_type=SubjectType.USER,
            subject_id=str(ACTOR_ID),
            membership_version=1,
            auth_time=NOW,
            request_id=METADATA.request_id,
            trace_id=METADATA.trace_id,
        ),
        permissions=frozenset(permissions),
    )


def record() -> AgentRecord:
    return AgentRecord(
        id=AGENT_ID,
        tenant_id=TENANT_ID,
        code="support_agent",
        name="Support Agent",
        description=None,
        runtime_type="agentscope",
        visibility="tenant",
        tags=("support",),
        bindings=(),
        status="DRAFT",
        resource_version=1,
        active_deployment_id=None,
        owner_user_id=ACTOR_ID,
        created_at=NOW,
        updated_at=NOW,
    )


class ResolverStub:
    def __init__(self, tenant_access: TenantAccess) -> None:
        self.tenant_access = tenant_access

    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        return self.tenant_access


class RegistryStub:
    def __init__(self) -> None:
        self.bindings: list[AgentBindingRecord] | None = None

    async def update_agent(self, context: TenantContext, **kwargs: object):
        self.bindings = cast(list[AgentBindingRecord] | None, kwargs["bindings"])
        return replace(
            record(), bindings=tuple(self.bindings or []), resource_version=2
        )


def service(
    tenant_access: TenantAccess, registry: RegistryStub
) -> AgentManagementService:
    return AgentManagementService(
        ResolverStub(tenant_access),
        cast(AgentRegistry, registry),
    )


def model_binding(
    suffix: str,
    *,
    role: str | None = None,
    configure: bool = False,
) -> dict[str, object]:
    binding: dict[str, object] = {
        "resource_type": "model",
        "resource_id": str(UUID(int=MODEL_ID.int + int(suffix))),
        "version_policy": "fixed",
        "version_id": str(UUID(int=MODEL_VERSION_ID.int + int(suffix))),
    }
    if role is not None:
        binding["binding_role"] = role
    if configure:
        binding["configuration_schema_version"] = "model-routing/v1"
        binding["configuration"] = {
            "fallback_error_codes": ["RATE_LIMITED", "PROVIDER_UNAVAILABLE"]
        }
    return binding


@pytest.mark.asyncio
async def test_single_legacy_model_binding_is_normalized_to_primary() -> None:
    registry = RegistryStub()
    agent_service = service(access("agent:update"), registry)

    result, etag = await agent_service.update_agent(
        principal(),
        agent_id=str(AGENT_ID),
        if_match='"rv:1"',
        request=AgentUpdateRequest.model_validate({"bindings": [model_binding("0")]}),
        metadata=METADATA,
    )

    assert result.bindings[0].binding_role == "primary"
    assert etag == '"rv:2"'


@pytest.mark.parametrize(
    "bindings",
    [
        [model_binding("0"), model_binding("1")],
        [
            model_binding("0", role="primary", configure=True),
            model_binding("1", role="primary"),
        ],
        [
            model_binding("0", role="primary", configure=True),
            model_binding("1", role="fallback_2"),
        ],
        [
            model_binding("0", role="primary"),
            model_binding("1", role="fallback_1"),
        ],
        [
            model_binding("0", role="primary", configure=True),
            model_binding("1", role="fallback_1"),
            model_binding("2", role="fallback_2"),
            model_binding("3", role="fallback_2"),
        ],
        [
            {
                "resource_type": "prompt",
                "resource_id": str(PROMPT_ID),
                "version_policy": "fixed",
                "version_id": str(PROMPT_VERSION_ID),
                "binding_role": "primary",
            }
        ],
        [
            {
                "resource_type": "prompt",
                "resource_id": str(PROMPT_ID),
                "version_policy": "fixed",
            }
        ],
        [
            {
                "resource_type": "prompt",
                "resource_id": str(PROMPT_ID),
                "version_policy": "resolve_on_publish",
                "version_id": str(PROMPT_VERSION_ID),
            }
        ],
        [
            {
                **model_binding("0", role="primary"),
                "configuration_schema_version": "model-routing/v1",
            }
        ],
        [
            {
                **model_binding("0", role="primary"),
                "configuration": {"fallback_error_codes": ["RATE_LIMITED"]},
            }
        ],
    ],
)
@pytest.mark.asyncio
async def test_unsafe_agent_bindings_fail_before_persistence(
    bindings: list[dict[str, object]],
) -> None:
    registry = RegistryStub()
    agent_service = service(access("agent:update"), registry)

    with pytest.raises(PlatformError) as raised:
        await agent_service.update_agent(
            principal(),
            agent_id=str(AGENT_ID),
            if_match='"rv:1"',
            request=AgentUpdateRequest.model_validate({"bindings": bindings}),
            metadata=METADATA,
        )

    assert raised.value.code == "VALIDATION_ERROR"
    assert registry.bindings is None


@pytest.mark.asyncio
async def test_agent_update_requires_permission_and_non_null_mutable_fields() -> None:
    denied = service(access(), RegistryStub())
    with pytest.raises(PlatformError) as denied_error:
        await denied.update_agent(
            principal(),
            agent_id=str(AGENT_ID),
            if_match='"rv:1"',
            request=AgentUpdateRequest.model_validate({"name": "Renamed"}),
            metadata=METADATA,
        )
    assert denied_error.value.code == "PERMISSION_DENIED"

    allowed = service(access("agent:update"), RegistryStub())
    with pytest.raises(PlatformError) as validation:
        await allowed.update_agent(
            principal(),
            agent_id=str(AGENT_ID),
            if_match='"rv:1"',
            request=AgentUpdateRequest.model_validate({"tags": None}),
            metadata=METADATA,
        )
    assert validation.value.code == "VALIDATION_ERROR"
