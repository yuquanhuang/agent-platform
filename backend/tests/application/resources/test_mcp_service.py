"""MCP service publication and rollback capability-evidence tests."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from packages.application.public import (
    CompositeResourceReferenceReader,
    McpAccessResolver,
    McpDiscoveryEvidenceStore,
    McpManagementService,
    McpRegistry,
    RequestMetadata,
)
from packages.contracts.generated.resource_content import ResourceContentMcp
from packages.contracts.generated.resources_models import (
    ResourcePublishRequest,
    ResourceRollbackRequest,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.domain.public import (
    IdempotencyReplay,
    McpCapabilityEvidenceRecord,
    MutationOutcome,
    ResourceDefinitionRecord,
    ResourceVersionRecord,
    TenantAccess,
    canonical_content_hash,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
RESOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
NEW_VERSION_ID = UUID("55555555-5555-4555-8555-555555555555")
DISCOVERY_ID = UUID("66666666-6666-4666-8666-666666666666")
NOW = datetime(2026, 8, 10, tzinfo=UTC)
METADATA = RequestMetadata(request_id="req-mcp", trace_id="trace-mcp")


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        identity_issuer="https://issuer.test",
        external_subject="mcp-admin",
        display_name="MCP Admin",
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


def content() -> ResourceContentMcp:
    secret_ref = f"secret://tenant/{TENANT_ID}/mcp/search"
    return ResourceContentMcp(
        resource_type="mcp",
        transport="streamable_http",
        endpoint="https://mcp.example.test/v1",
        header_templates={"Authorization": f"Bearer ${{{secret_ref}}}"},
        secret_refs=[secret_ref],
        timeout_seconds=30,
        allowed_tools=["search.query"],
    )


def definition(resource_version: int = 3) -> ResourceDefinitionRecord:
    return ResourceDefinitionRecord(
        id=RESOURCE_ID,
        tenant_id=TENANT_ID,
        resource_type="mcp",
        code="search_mcp",
        name="Search MCP",
        description=None,
        owner_user_id=ACTOR_ID,
        visibility="tenant",
        content_schema_version="1.0",
        content=content(),
        status="DRAFT",
        resource_version=resource_version,
        created_at=NOW,
        updated_at=NOW,
    )


def version(version_id: UUID = VERSION_ID) -> ResourceVersionRecord:
    value = content()
    return ResourceVersionRecord(
        id=version_id,
        tenant_id=TENANT_ID,
        definition_id=RESOURCE_ID,
        version_no=1,
        schema_version="1.0",
        content=value,
        content_hash=canonical_content_hash(value),
        release_note="release",
        status="PUBLISHED",
        published_at=NOW,
        published_by=ACTOR_ID,
    )


def evidence(*, published_version_id: UUID | None) -> McpCapabilityEvidenceRecord:
    return McpCapabilityEvidenceRecord(
        id=DISCOVERY_ID,
        tenant_id=TENANT_ID,
        definition_id=RESOURCE_ID,
        operation_id=None,
        draft_resource_version=3,
        content_hash=canonical_content_hash(content()),
        status="PASSED",
        capability_hash="sha256:" + "a" * 64,
        tool_names=("search.query",),
        published_version_id=published_version_id,
        discovered_at=NOW,
        discovered_by=ACTOR_ID,
    )


class Resolver:
    def __init__(self, tenant_access: TenantAccess) -> None:
        self.tenant_access = tenant_access

    async def resolve_tenant_access(
        self, authenticated: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        del authenticated, metadata
        return self.tenant_access


class Registry:
    def __init__(self) -> None:
        self.attestation_id: UUID | None = None
        self.replay: IdempotencyReplay | None = None

    async def get_idempotency_replay(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return self.replay

    async def get_definition(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return definition()

    async def get_version(self, context: TenantContext, **kwargs: object):
        del context, kwargs
        return version()

    async def publish_version(self, context: TenantContext, **kwargs: object):
        del context
        self.attestation_id = cast(UUID, kwargs["mcp_discovery_attestation_id"])
        return MutationOutcome(value=version(NEW_VERSION_ID))

    async def rollback_version(self, context: TenantContext, **kwargs: object):
        del context
        self.attestation_id = cast(UUID, kwargs["mcp_discovery_attestation_id"])
        return MutationOutcome(value=version(NEW_VERSION_ID))


class DiscoveryStore:
    def __init__(self, record: McpCapabilityEvidenceRecord | None) -> None:
        self.record = record
        self.calls: list[tuple[str, object]] = []

    async def get_publishable_evidence(
        self, context: TenantContext, **kwargs: object
    ) -> McpCapabilityEvidenceRecord | None:
        del context
        self.calls.append(("publish", kwargs))
        return self.record

    async def get_published_evidence(
        self, context: TenantContext, **kwargs: object
    ) -> McpCapabilityEvidenceRecord | None:
        del context
        self.calls.append(("rollback", kwargs))
        return self.record


def service(
    tenant_access: TenantAccess, registry: Registry, discoveries: DiscoveryStore
) -> McpManagementService:
    return McpManagementService(
        cast(McpAccessResolver, Resolver(tenant_access)),
        cast(McpRegistry, registry),
        CompositeResourceReferenceReader([]),
        cast(McpDiscoveryEvidenceStore, discoveries),
    )


@pytest.mark.asyncio
async def test_publish_requires_exact_passed_discovery_evidence() -> None:
    registry = Registry()
    management = service(access("mcp:publish"), registry, DiscoveryStore(None))

    with pytest.raises(PlatformError, match="passed discovery"):
        await management.publish_mcp_server(
            principal(),
            resource_id=str(RESOURCE_ID),
            request=ResourcePublishRequest(
                expected_resource_version=3, release_note="publish"
            ),
            idempotency_key="mcp-publish-001",
            metadata=METADATA,
        )

    assert registry.attestation_id is None


@pytest.mark.asyncio
async def test_publish_binds_selected_discovery_attestation() -> None:
    registry = Registry()
    discoveries = DiscoveryStore(evidence(published_version_id=None))
    management = service(access("mcp:publish"), registry, discoveries)

    result = await management.publish_mcp_server(
        principal(),
        resource_id=str(RESOURCE_ID),
        request=ResourcePublishRequest(
            expected_resource_version=3, release_note="publish"
        ),
        idempotency_key="mcp-publish-002",
        metadata=METADATA,
    )

    assert result.id == str(NEW_VERSION_ID)
    assert registry.attestation_id == DISCOVERY_ID
    call = cast(dict[str, object], discoveries.calls[0][1])
    assert call["draft_resource_version"] == 3
    assert call["allowed_tools"] == ("search.query",)


@pytest.mark.asyncio
async def test_rollback_reuses_source_version_evidence_atomically_in_registry() -> None:
    registry = Registry()
    discoveries = DiscoveryStore(evidence(published_version_id=VERSION_ID))
    management = service(access("mcp:rollback"), registry, discoveries)

    result = await management.rollback_mcp_server(
        principal(),
        resource_id=str(RESOURCE_ID),
        request=ResourceRollbackRequest(
            version_id=str(VERSION_ID),
            expected_resource_version=3,
            release_note="rollback",
        ),
        idempotency_key="mcp-rollback-001",
        metadata=METADATA,
    )

    assert result.id == str(NEW_VERSION_ID)
    assert registry.attestation_id == DISCOVERY_ID
    call = cast(dict[str, object], discoveries.calls[0][1])
    assert call["source_version_id"] == VERSION_ID
    assert "operation_id" not in call
