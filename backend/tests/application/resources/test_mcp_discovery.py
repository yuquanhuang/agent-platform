"""MCP configuration, untrusted discovery and isolated handler tests."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from packages.application.resources.mcp_discovery import (
    MCP_CAPABILITY_DISCOVERY_EVENT,
    McpCapabilityDiscoveryHandler,
    validate_discovery_response,
    validate_mcp_content,
)
from packages.contracts.generated.resource_content import ResourceContentMcp
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.mcp import (
    McpCapabilityEvidenceRecord,
    McpDiscoveryResponse,
    McpDiscoveryResult,
    McpDiscoveryTarget,
    McpRawTool,
)
from packages.domain.outbox import OutboxEvent, OutboxStatus

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
DEFINITION_ID = UUID("33333333-3333-4333-8333-333333333333")
OPERATION_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 8, 10, tzinfo=UTC)
SECRET_REF = f"secret://tenant/{TENANT_ID}/mcp/search"


def context() -> TenantContext:
    return TenantContext(
        tenant_id=str(TENANT_ID),
        subject_type=SubjectType.USER,
        subject_id=str(USER_ID),
        membership_version=1,
        auth_time=NOW,
        request_id="request-mcp",
        trace_id="trace-mcp",
    )


def content(**overrides: object) -> ResourceContentMcp:
    payload: dict[str, object] = {
        "resource_type": "mcp",
        "transport": "streamable_http",
        "endpoint": "https://mcp.example.test/v1",
        "header_templates": {"Authorization": f"Bearer ${{{SECRET_REF}}}"},
        "secret_refs": [SECRET_REF],
        "timeout_seconds": 30,
        "allowed_tools": ["search.query"],
    }
    payload.update(overrides)
    return ResourceContentMcp.model_validate(payload)


def target() -> McpDiscoveryTarget:
    value = content()
    return McpDiscoveryTarget(
        definition_id=DEFINITION_ID,
        draft_resource_version=3,
        content_hash="sha256:" + "a" * 64,
        transport="streamable_http",
        endpoint=value.endpoint,
        header_templates=value.header_templates or {},
        secret_refs=tuple(value.secret_refs),
        timeout_seconds=value.timeout_seconds,
        allowed_tools=tuple(value.allowed_tools or ()),
    )


def response() -> McpDiscoveryResponse:
    return McpDiscoveryResponse(
        protocol_version="2025-06-18",
        server_name="search-mcp",
        server_version="1.0.0",
        tools=(
            McpRawTool(
                name="search.query",
                description="Search approved public sources.",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                annotations={"readOnlyHint": True},
            ),
        ),
    )


@pytest.mark.parametrize(
    "invalid_endpoint",
    [
        "http://mcp.example.test/v1",
        "https://127.0.0.1/v1",
        "https://localhost/v1",
        "https://mcp.internal/v1",
        "https://user:password@mcp.example.test/v1",
        "https://mcp.example.test/v1?token=secret",
    ],
)
def test_mcp_content_rejects_unsafe_endpoints(invalid_endpoint: str) -> None:
    with pytest.raises(ValueError):
        validate_mcp_content(context(), content(endpoint=invalid_endpoint))


def test_mcp_content_requires_declared_tenant_secret_placeholders() -> None:
    with pytest.raises(ValueError, match="undeclared Secret"):
        validate_mcp_content(
            context(),
            content(
                header_templates={
                    "Authorization": "Bearer ${secret://tenant/other/mcp/search}"
                }
            ),
        )


def test_discovery_normalizes_schema_hash_risk_and_capability_hash() -> None:
    result = validate_discovery_response(target(), response())

    assert result.status == "PASSED"
    assert result.capability_hash is not None
    assert result.tools[0].schema_hash.startswith("sha256:")
    assert result.tools[0].risk_level == "MEDIUM"
    assert result.findings == ()


def test_discovery_rejects_missing_allowed_tool_and_invalid_schema() -> None:
    invalid = McpDiscoveryResponse(
        protocol_version="2025-06-18",
        server_name="search-mcp",
        server_version=None,
        tools=(
            McpRawTool(
                name="other.tool",
                description=None,
                input_schema={"type": "not-a-json-schema-type"},
            ),
        ),
    )

    result = validate_discovery_response(target(), invalid)

    assert result.status == "REJECTED"
    assert {str(finding["code"]) for finding in result.findings} == {
        "INVALID_MCP_TOOL_SCHEMA",
        "MCP_ALLOWED_TOOL_NOT_DISCOVERED",
    }


class Discoverer:
    async def discover(
        self, context: TenantContext, target: McpDiscoveryTarget
    ) -> McpDiscoveryResponse:
        assert context.subject_type is SubjectType.SERVICE
        assert SECRET_REF in repr(target)
        return response()


class CompletionStore:
    def __init__(self) -> None:
        self.states: list[tuple[str, object]] = []
        self.terminal_error: dict[str, object] | None = None

    async def mark_running(self, context: TenantContext, operation_id: UUID) -> bool:
        self.states.append(("RUNNING", operation_id))
        return True

    async def record_terminal(
        self,
        context: TenantContext,
        *,
        operation_id: UUID,
        target: McpDiscoveryTarget,
        result: McpDiscoveryResult,
        discovered_by: UUID,
        error: dict[str, object] | None,
    ) -> McpCapabilityEvidenceRecord:
        self.states.append(("TERMINAL", result))
        self.terminal_error = error
        return McpCapabilityEvidenceRecord(
            id=uuid4(),
            tenant_id=TENANT_ID,
            definition_id=target.definition_id,
            operation_id=operation_id,
            draft_resource_version=target.draft_resource_version,
            content_hash=target.content_hash,
            status=result.status,
            capability_hash=result.capability_hash,
            tool_names=tuple(tool.name for tool in result.tools),
            published_version_id=None,
            discovered_at=NOW,
            discovered_by=discovered_by,
        )


def event() -> OutboxEvent:
    value = target()
    return OutboxEvent(
        id=uuid4(),
        tenant_id=TENANT_ID,
        aggregate_type="mcp",
        aggregate_id=DEFINITION_ID,
        event_type=MCP_CAPABILITY_DISCOVERY_EVENT,
        payload={
            "operation_id": str(OPERATION_ID),
            "requested_by": str(USER_ID),
            "definition_id": str(DEFINITION_ID),
            "draft_resource_version": value.draft_resource_version,
            "content_hash": value.content_hash,
            "transport": value.transport,
            "endpoint": value.endpoint,
            "header_templates": value.header_templates,
            "secret_refs": list(value.secret_refs),
            "timeout_seconds": value.timeout_seconds,
            "allowed_tools": list(value.allowed_tools),
        },
        payload_schema_version=1,
        status=OutboxStatus.PUBLISHING,
        attempts=1,
        next_attempt_at=NOW,
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_handler_terminalizes_safe_normalized_evidence() -> None:
    store = CompletionStore()
    handler = McpCapabilityDiscoveryHandler(discoverer=Discoverer(), store=store)

    started = await handler.start(event())

    assert started.workflow_id == f"mcp-capability-discovery/{OPERATION_ID}"
    assert store.states[0] == ("RUNNING", OPERATION_ID)
    terminal = store.states[-1]
    assert terminal[0] == "TERMINAL"
    assert "Bearer" not in repr(terminal)
    assert SECRET_REF not in repr(terminal)


@pytest.mark.asyncio
async def test_handler_skips_gateway_call_for_terminal_replay() -> None:
    class TerminalStore(CompletionStore):
        async def mark_running(
            self, context: TenantContext, operation_id: UUID
        ) -> bool:
            del context, operation_id
            return False

    class UnexpectedDiscoverer(Discoverer):
        async def discover(
            self, context: TenantContext, target: McpDiscoveryTarget
        ) -> McpDiscoveryResponse:
            del context, target
            raise AssertionError("terminal replay must not call the MCP Gateway")

    handler = McpCapabilityDiscoveryHandler(
        discoverer=UnexpectedDiscoverer(), store=TerminalStore()
    )

    started = await handler.start(event())

    assert started.already_exists is True
