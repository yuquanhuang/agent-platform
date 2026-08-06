"""Prompt application authorization, mapping and sensitive Diff tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    CompositeResourceReferenceReader,
    PromptManagementService,
    PromptRegistry,
    RequestMetadata,
    TenantAccessResolver,
)
from packages.contracts.generated.resource_content import (
    ResourceContentPrompt,
    ResourceContentVariable,
)
from packages.contracts.generated.resources_models import PromptCreateRequest
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    MutationOutcome,
    ResourceDefinitionRecord,
    ResourceVersionRecord,
    TenantAccess,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
RESOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_A = UUID("44444444-4444-4444-8444-444444444444")
VERSION_B = UUID("55555555-5555-4555-8555-555555555555")
NOW = datetime(2026, 8, 6, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-prompt", trace_id="trace-prompt")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="prompt-admin",
        display_name="Prompt Admin",
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


def content(secret: str, template: str = "Hello {{ token }}") -> ResourceContentPrompt:
    return ResourceContentPrompt(
        resource_type="prompt",
        template=template,
        variables=[
            ResourceContentVariable(
                name="token",
                type="string",
                required=True,
                sensitive=True,
                default=secret,
                max_length=100,
            )
        ],
        language="zh-CN",
        compiler_policy_version="1",
    )


def definition() -> ResourceDefinitionRecord:
    return ResourceDefinitionRecord(
        id=RESOURCE_ID,
        tenant_id=TENANT_ID,
        resource_type="prompt",
        code="welcome_prompt",
        name="Welcome Prompt",
        description=None,
        owner_user_id=ACTOR_ID,
        visibility="tenant",
        content_schema_version="1.0",
        content=content("secret-a"),
        status="DRAFT",
        resource_version=1,
        created_at=NOW,
        updated_at=NOW,
    )


class ResolverStub:
    def __init__(self, tenant_access: TenantAccess) -> None:
        self.tenant_access = tenant_access

    async def resolve_tenant_access(
        self, authenticated: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        return self.tenant_access


class RegistryStub:
    def __init__(self) -> None:
        self.request_hash: str | None = None

    async def create_definition(self, context: TenantContext, **kwargs: object):
        self.request_hash = cast(str, kwargs["request_hash"])
        return MutationOutcome(value=definition())

    async def get_version(
        self, context: TenantContext, **kwargs: object
    ) -> ResourceVersionRecord | None:
        version_id = cast(UUID, kwargs["version_id"])
        if version_id == VERSION_A:
            return ResourceVersionRecord(
                id=VERSION_A,
                tenant_id=TENANT_ID,
                definition_id=RESOURCE_ID,
                version_no=1,
                schema_version="1.0",
                content=content("secret-a"),
                content_hash="sha256:" + "a" * 64,
                release_note="v1",
                status="PUBLISHED",
                published_at=NOW,
                published_by=ACTOR_ID,
            )
        if version_id == VERSION_B:
            return ResourceVersionRecord(
                id=VERSION_B,
                tenant_id=TENANT_ID,
                definition_id=RESOURCE_ID,
                version_no=2,
                schema_version="1.0",
                content=content("secret-b", "Hi {{ token }}"),
                content_hash="sha256:" + "b" * 64,
                release_note="v2",
                status="PUBLISHED",
                published_at=NOW,
                published_by=ACTOR_ID,
            )
        return None


def service(
    tenant_access: TenantAccess, registry: RegistryStub
) -> PromptManagementService:
    return PromptManagementService(
        cast(TenantAccessResolver, ResolverStub(tenant_access)),
        cast(PromptRegistry, registry),
        CompositeResourceReferenceReader([]),
    )


@pytest.mark.asyncio
async def test_create_prompt_requires_permission_and_returns_strong_etag() -> None:
    registry = RegistryStub()
    prompt_service = service(access("prompt:create"), registry)
    request = PromptCreateRequest(
        code="welcome_prompt",
        name="Welcome Prompt",
        description=None,
        visibility="tenant",
        content_schema_version="1.0",
        content=content("secret-a"),
    )

    result, etag = await prompt_service.create_prompt(
        principal(),
        request=request,
        idempotency_key="prompt-create-1",
        metadata=METADATA,
    )

    assert result.resource_type == "prompt"
    assert etag == '"rv:1"'
    assert registry.request_hash is not None

    denied_service = service(access(), RegistryStub())
    with pytest.raises(PlatformError) as denied:
        await denied_service.create_prompt(
            principal(),
            request=request,
            idempotency_key="prompt-create-2",
            metadata=METADATA,
        )
    assert denied.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_prompt_diff_redacts_sensitive_defaults_but_keeps_template_diff() -> None:
    prompt_service = service(access("prompt:read"), RegistryStub())

    result = await prompt_service.diff_prompt_versions(
        principal(),
        resource_id=str(RESOURCE_ID),
        from_version_id=str(VERSION_A),
        to_version_id=str(VERSION_B),
        metadata=METADATA,
    )

    sensitive = next(change for change in result.changes if change.sensitive)
    assert sensitive.path == "/variables/token/default"
    assert sensitive.before == "[REDACTED]"
    assert sensitive.after == "[REDACTED]"
    template = next(change for change in result.changes if change.path == "/template")
    assert template.before == "Hello {{ token }}"
    assert template.after == "Hi {{ token }}"
