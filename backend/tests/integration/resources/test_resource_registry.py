"""Real PostgreSQL resource registry, RLS, CAS and idempotency verification."""

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest
from agentscope.event import (
    AgentEvent,
    ExternalExecutionResultEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg, ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.types import ReplyFinishedReason
from alembic import command
from alembic.config import Config
from pydantic import JsonValue, SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from apps.api.app import create_app
from apps.event_worker.composition import build_temporal_outbox_dispatcher
from packages.application.event_service import (
    RUN_EVENTS_APPENDED_EVENT,
    EventWriteAccess,
    RunEventIngestionService,
    RunEventNotification,
    RunEventNotificationDispatcher,
    RunEventNotificationPublisher,
    RunEventQueryService,
    RunEventStreamService,
)
from packages.application.model_gateway import (
    ModelProviderConnectionTestHandler,
    ProviderAdapterRegistry,
)
from packages.application.outbox import OutboxDispatcher, OutboxEventRouter
from packages.application.public import (
    AgUiEventBatch,
    ApprovalCoordinator,
    ApprovalRequestInput,
    RequestMetadata,
    RunEventAgUiAdapter,
    RunManagementService,
    canonical_request_hash,
    serialize_ag_ui_event,
)
from packages.application.sandbox import (
    SANDBOX_MANAGE_PERMISSION,
    ProviderProcessObservation,
    ProviderProvisionSpec,
    ProviderSandboxObservation,
    SandboxLifecycleService,
    SandboxPolicyResolver,
    SandboxProvider,
    SandboxProvisionTokenVerifier,
    SandboxServiceAccess,
    compile_sandbox_policy,
)
from packages.application.temporal import (
    RUN_ORCHESTRATOR_TASK_QUEUE,
    AgentRunWorkflow,
    AgentRunWorkflowActivities,
    FencingTokenIssuer,
    RunExecutionRequest,
    RunRuntimeExecutor,
    RunSandboxController,
    RunSandboxProvisionRequest,
    RunSandboxReleaseRequest,
    RunSpecCompilationSource,
    RuntimeEventCandidatePublisher,
    RuntimeTargetReleaseConfig,
    agent_run_workflow_id,
)
from packages.application.tool_gateway import (
    ExecutionTicketIssue,
    ToolExecutionDenied,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolGatewayService,
    canonical_tool_parameter_digest,
)
from packages.contracts.generated.core_models import (
    AgentCreateRequest,
    AgentUpdateRequest,
    ApprovalDecisionRequest,
    CancelRunRequest,
    CopyAgentRequest,
    RetryRunRequest,
    RunCreateRequest,
    RunEventBatchRequest,
    SessionCreateRequest,
    SessionUpdateRequest,
)
from packages.contracts.generated.resource_content import (
    ResourceContentMcp,
    ResourceContentModelConfig,
    ResourceContentModelProvider,
    ResourceContentPrompt,
    ResourceContentSkill,
    ResourceContentSkillFile,
    SkillManifest,
)
from packages.contracts.generated.resources_models import (
    ActionRequest,
    ResourceCopyRequest,
    ResourceCreateRequest,
    ResourcePublishRequest,
    ResourceRollbackRequest,
    ResourceUpdateRequest,
)
from packages.contracts.generated.run_event import (
    RUNTIME_EVENT_CANDIDATE_ADAPTER,
    RuntimeEventCandidate,
)
from packages.contracts.model_gateway import ModelGatewayRequest
from packages.contracts.public import (
    AuthenticatedPrincipal,
    PlatformError,
    SubjectType,
    TenantContext,
)
from packages.contracts.sandbox_api import (
    SandboxLeaseRequest,
    SandboxProcessRequest,
    SandboxProvisionRequest,
    SandboxReleaseRequest,
)
from packages.contracts.temporal import (
    AssistantTextPart,
    FinalizeAgentRunCancellationInput,
    FinalizeAgentRunInput,
    ReleaseRunSandboxResult,
    RunSandboxHandle,
    RunSpecReference,
    RuntimeCancellationResult,
    RuntimeCompletion,
)
from packages.domain.model_gateway import (
    AdapterResponse,
    AdapterStreamEvent,
    ModelInvocationInput,
    ModelRoute,
    ModelUsageRecord,
    ProviderConnectionTarget,
    ProviderError,
)
from packages.domain.public import (
    BUNDLE_COMPILER_NAME,
    BUNDLE_COMPILER_VERSION,
    AgentBindingRecord,
    ApprovalRequestRecord,
    McpDiscoveredTool,
    McpDiscoveryResult,
    McpDiscoveryTarget,
    ReleaseRecord,
    RunRecord,
    RuntimeBundleRecord,
    SkillSupplyChainScanResult,
    TenantAccess,
    canonical_content_hash,
)
from packages.domain.skills.model import SkillScanStatus
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.config import AppSettings
from packages.infrastructure.database.public import (
    AgentSnapshotModel,
    AgentVersionModel,
    ApprovalDecisionModel,
    ApprovalRequestModel,
    DeploymentModel,
    ExecutionTicketModel,
    McpCapabilityDiscoveryModel,
    SkillSupplyChainScanModel,
    SqlAlchemyAgentRegistry,
    SqlAlchemyAgentResourceReferenceProvider,
    SqlAlchemyApprovalStore,
    SqlAlchemyBundleInputReader,
    SqlAlchemyDeploymentStore,
    SqlAlchemyExecutionTicketStore,
    SqlAlchemyMcpDiscoveryStore,
    SqlAlchemyMessageHistoryStore,
    SqlAlchemyOutboxStore,
    SqlAlchemyReleaseStore,
    SqlAlchemyResourceRegistry,
    SqlAlchemyRunEventQueryStore,
    SqlAlchemyRunEventStore,
    SqlAlchemyRunStore,
    SqlAlchemyRuntimeEventCandidatePublisher,
    SqlAlchemySandboxLifecycleStore,
    SqlAlchemySessionStore,
    SqlAlchemySkillScanStore,
    SqlAlchemySnapshotCompilationStore,
    SqlAlchemyToolAuthorizationResolver,
    SqlAlchemyWorkspaceStore,
    TenantUnitOfWork,
    create_session_factory,
)
from packages.infrastructure.model_gateway import (
    MappingSecretReferenceResolver,
    SqlAlchemyModelBindingReader,
    SqlAlchemyModelGatewayStore,
)
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.temporal import HmacFencingTokenIssuer
from packages.infrastructure.tool_gateway import HmacExecutionTicketIssuer
from packages.runtimes.agentscope import (
    AgentScopeApprovalBridge,
    AgentScopeRuntimeBridge,
    AgentScopeSessionStart,
    RuntimeToolBinding,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
APP_ROLE = "agent_platform_resource_test"
APP_PASSWORD = "resource-test-only"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
ACTOR = UUID("33333333-3333-4333-8333-333333333333")
OTHER_ACTOR = UUID("33333333-3333-4333-8333-333333333334")
PERMISSION_TENANT = UUID("44444444-4444-4444-8444-444444444444")
METADATA = RequestMetadata(
    request_id="req-resource-integration", trace_id="trace-resource-integration"
)
SKILL_METADATA = RequestMetadata(
    request_id="req-skill-integration", trace_id="trace-skill-integration"
)


def admitted_manifest(
    *, sandbox_policy_hash: str = "sha256:" + "c" * 64
) -> dict[str, JsonValue]:
    return {
        "schema_version": "1.0",
        "compiler": {
            "name": BUNDLE_COMPILER_NAME,
            "version": BUNDLE_COMPILER_VERSION,
        },
        "security": {
            "permission_policy_hash": "sha256:" + "d" * 64,
            "sandbox_policy_hash": sandbox_policy_hash,
            "secret_refs": [],
        },
    }


class Epic3AccessResolver:
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess:
        assert principal.identity_issuer == "https://issuer.test/"
        assert principal.external_subject == "resource-owner"
        return TenantAccess(
            context=TenantContext(
                tenant_id=TENANT_A,
                subject_type=SubjectType.USER,
                subject_id=str(ACTOR),
                membership_version=1,
                auth_time=principal.auth_time,
                request_id=metadata.request_id,
                trace_id=metadata.trace_id,
            ),
            permissions=frozenset(
                {
                    "session:read",
                    "run:create",
                    "run:read",
                    "run:list",
                    "run:cancel",
                    "run:retry",
                }
            ),
        )


class Epic3RunSpecCompiler:
    def __init__(self) -> None:
        self.sources: list[RunSpecCompilationSource] = []
        self.fencing_tokens: list[str] = []

    async def compile(
        self,
        context: TenantContext,
        *,
        source: RunSpecCompilationSource,
        execution_attempt: int,
        fencing_token: SecretStr,
    ) -> RunSpecReference:
        assert context.tenant_id == TENANT_A
        self.sources.append(source)
        self.fencing_tokens.append(fencing_token.get_secret_value())
        return RunSpecReference(
            uri=f"memory://run-spec/{source.run_id}/{execution_attempt}",
            content_hash="sha256:" + "9" * 64,
            size_bytes=1024,
        )


class Epic3CandidatePublisher:
    def __init__(self) -> None:
        self.publish_calls = 0
        self.candidates: dict[
            tuple[UUID, int, str], tuple[RunExecutionRequest, RuntimeEventCandidate]
        ] = {}

    async def publish(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        candidate: RuntimeEventCandidate,
    ) -> None:
        self.publish_calls += 1
        assert context.tenant_id == str(request.tenant_id) == TENANT_A
        assert request.execution_attempt == 1
        assert len(request.fencing_token.get_secret_value()) >= 16
        serialized = candidate.model_dump(mode="json")
        assert not {"event_id", "sequence_no", "recorded_at"} & serialized.keys()
        key = (request.run_id, request.execution_attempt, candidate.source_event_id)
        previous = self.candidates.get(key)
        if previous is not None:
            assert previous[1] == candidate
            return
        self.candidates[key] = (request, candidate)


class Epic3RuntimeExecutor:
    async def execute(
        self,
        context: TenantContext,
        *,
        request: RunExecutionRequest,
        event_publisher: RuntimeEventCandidatePublisher,
    ) -> RuntimeCompletion:
        assert request.sandbox is not None
        assert request.sandbox.sandbox_instance_id == "sandbox_epic3_vertical"
        occurred_at = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        raw_candidates = (
            {
                "source_event_id": "fake:run-started:1",
                "event_type": "run_started",
                "occurred_at": occurred_at,
                "payload_version": "1.0",
                "payload": {
                    "runtime_type": "agentscope",
                    "runtime_target_id": "rt_agentscope_a",
                },
            },
            {
                "source_event_id": "fake:text-start:1",
                "event_type": "text_message_start",
                "occurred_at": occurred_at,
                "payload_version": "1.0",
                "payload": {"message_id": "runtime-reply-1", "role": "assistant"},
            },
            {
                "source_event_id": "fake:text-delta:1",
                "event_type": "text_delta",
                "occurred_at": occurred_at,
                "payload_version": "1.0",
                "payload": {
                    "message_id": "runtime-reply-1",
                    "delta": "candidate-only-fragment",
                },
            },
            {
                "source_event_id": "fake:text-end:1",
                "event_type": "text_message_end",
                "occurred_at": occurred_at,
                "payload_version": "1.0",
                "payload": {"message_id": "runtime-reply-1", "finish_reason": "stop"},
            },
        )
        candidates = tuple(
            RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(candidate)
            for candidate in raw_candidates
        )
        for candidate in (*candidates, candidates[2]):
            await event_publisher.publish(context, request=request, candidate=candidate)
        return RuntimeCompletion(
            status="SUCCEEDED",
            assistant_content_parts=(
                AssistantTextPart(text="Epic 3 vertical acceptance complete."),
            ),
            result_quality="NORMAL",
            runtime_handle_ref=f"fake-runtime://{request.run_id}/1",
        )


class Epic3SandboxController:
    def __init__(self) -> None:
        self.provisioned: list[RunSandboxProvisionRequest] = []
        self.released: list[RunSandboxReleaseRequest] = []

    async def provision(
        self, context: TenantContext, *, request: RunSandboxProvisionRequest
    ) -> RunSandboxHandle:
        assert context.tenant_id == TENANT_A
        self.provisioned.append(request)
        return RunSandboxHandle(
            sandbox_instance_id="sandbox_epic3_vertical",
            lease_id="lease_epic3_vertical",
            workspace_uri=f"workspace://tenant/{TENANT_A}/runs/{request.run_id}/",
        )

    async def release(
        self, context: TenantContext, *, request: RunSandboxReleaseRequest
    ) -> ReleaseRunSandboxResult:
        assert context.tenant_id == TENANT_A
        self.released.append(request)
        return ReleaseRunSandboxResult(
            sandbox_instance_id=request.sandbox.sandbox_instance_id,
            status="TERMINATED",
        )


class IntegrationConnectionAdapter:
    async def generate(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AdapterResponse:
        del route, request, invocation, credential
        raise AssertionError("generate is outside this integration scenario")

    def stream(
        self,
        route: ModelRoute,
        request: ModelGatewayRequest,
        invocation: ModelInvocationInput,
        credential: SecretStr,
    ) -> AsyncIterator[AdapterStreamEvent]:
        del route, request, invocation, credential
        raise AssertionError("stream is outside this integration scenario")

    async def test_connection(
        self, target: ProviderConnectionTarget, credential: SecretStr
    ) -> None:
        assert target.provider == "openai"
        assert credential.get_secret_value() == "integration-provider-secret"


def require_database_url() -> str:
    database_url = os.getenv(DATABASE_URL_ENV)
    if database_url is None:
        pytest.skip(f"{DATABASE_URL_ENV} is required for PostgreSQL integration tests")
    database_name = make_url(database_url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail(f"{DATABASE_URL_ENV} must target a dedicated *_test database")
    return database_url


def migrate(database_url: str, revision: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    if revision == "base":
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


def context(tenant_id: str, actor_id: UUID = ACTOR) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        subject_type=SubjectType.USER,
        subject_id=str(actor_id),
        membership_version=1,
        auth_time=datetime(2026, 8, 6, tzinfo=UTC),
        request_id=METADATA.request_id,
        trace_id=METADATA.trace_id,
    )


def event_access(tenant_id: str) -> EventWriteAccess:
    return EventWriteAccess(
        context=TenantContext(
            tenant_id=tenant_id,
            subject_type=SubjectType.SERVICE,
            subject_id=str(ACTOR),
            auth_time=datetime(2026, 8, 6, tzinfo=UTC),
            request_id=METADATA.request_id,
            trace_id=METADATA.trace_id,
        ),
        permissions=frozenset({"internal:event_write"}),
    )


def prompt_request(
    *, code: str = "welcome_prompt", template: str = "Hello {{ name }}"
) -> ResourceCreateRequest:
    return ResourceCreateRequest(
        code=code,
        name="Welcome Prompt",
        description=None,
        content_schema_version="1.0",
        content=ResourceContentPrompt(
            resource_type="prompt",
            template=template,
            variables=[],
            language="en",
            compiler_policy_version="1",
        ),
    )


def skill_request() -> ResourceCreateRequest:
    manifest: dict[str, object] = {
        "apiVersion": "agent-platform/v1",
        "kind": "Skill",
        "metadata": {
            "name": "integration-skill",
            "version": "1.0.0",
            "displayName": "Integration Skill",
            "description": "PostgreSQL scan evidence verification.",
        },
        "runtime": {
            "compatible": ["agentscope"],
            "minimumPlatformVersion": "1.0.0",
        },
        "entry": {"instructions": "SKILL.md", "command": None},
        "permissions": {
            "filesystem": {"read": [], "write": []},
            "network": {"allowDomains": [], "allowPorts": []},
            "tools": [],
            "secrets": [],
        },
        "dependencies": {"python": [], "system": [], "lockFile": None},
        "inputs": {"type": "object"},
        "outputs": {"type": "object"},
        "sandbox": {
            "imageDigest": "registry.example.test/skill@sha256:" + "a" * 64,
            "timeoutSeconds": 60,
            "riskLevel": "LOW",
            "requiresRunSandbox": False,
        },
        "tests": [],
    }
    return ResourceCreateRequest(
        code="integration_skill",
        name="Integration Skill",
        description=None,
        content_schema_version="1.0",
        content=ResourceContentSkill(
            resource_type="skill",
            manifest=SkillManifest.model_validate(manifest),
            files=[
                ResourceContentSkillFile(
                    path="SKILL.md",
                    artifact_id=str(uuid5(NAMESPACE_URL, "skill-instructions")),
                    content_hash="sha256:" + "1" * 64,
                ),
                ResourceContentSkillFile(
                    path="manifest.yaml",
                    artifact_id=str(uuid5(NAMESPACE_URL, "skill-manifest")),
                    content_hash="sha256:" + "2" * 64,
                ),
            ],
        ),
    )


def skill_scan_result(
    status: SkillScanStatus = "PASSED",
) -> SkillSupplyChainScanResult:
    findings: tuple[dict[str, JsonValue], ...] = (
        ()
        if status == "PASSED"
        else (
            {
                "code": "TEST_REJECTION",
                "severity": "HIGH",
                "path": "/SKILL.md",
                "blocking": True,
            },
        )
    )
    return SkillSupplyChainScanResult(
        status=status,
        scanner_name="integration-scanner",
        scanner_version="1.0.0",
        policy_version="integration-v1",
        findings=findings,
        report_hash="sha256:" + ("3" if status == "PASSED" else "4") * 64,
        sbom={"format": "integration-sbom-v1"},
        sbom_hash="sha256:" + "5" * 64,
        signature_status="NOT_PROVIDED",
        provenance_status="NOT_PROVIDED",
    )


async def drop_test_role(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role_name"),
                {"role_name": APP_ROLE},
            )
            if exists is not None:
                await connection.execute(text(f"DROP OWNED BY {APP_ROLE}"))
                await connection.execute(text(f"DROP ROLE {APP_ROLE}"))
    finally:
        await engine.dispose()


async def seed_tenant_admin_role(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenant (id, code, name) VALUES "
                    "(:tenant_id, 'permission-tenant', 'Permission Tenant')"
                ),
                {"tenant_id": PERMISSION_TENANT},
            )
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            await connection.execute(
                text(
                    "INSERT INTO role "
                    "(tenant_id, code, name, built_in) VALUES "
                    "(:tenant_id, 'tenant_admin', 'Tenant Admin', true)"
                ),
                {"tenant_id": PERMISSION_TENANT},
            )
    finally:
        await engine.dispose()


async def verify_prompt_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'prompt'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {
                "create",
                "read",
                "list",
                "update",
                "delete",
                "publish",
                "rollback",
                "disable",
            }
    finally:
        await engine.dispose()


async def verify_skill_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'skill'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {
                "create",
                "read",
                "list",
                "update",
                "delete",
                "publish",
                "rollback",
                "disable",
            }
    finally:
        await engine.dispose()


async def verify_mcp_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'mcp'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {
                "create",
                "read",
                "list",
                "update",
                "delete",
                "publish",
                "rollback",
                "disable",
                "execute",
            }
    finally:
        await engine.dispose()


async def verify_model_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            rows = (
                await connection.execute(
                    text(
                        "SELECT resource_type, action FROM role_permission "
                        "WHERE tenant_id = :tenant_id AND resource_type IN "
                        "('model_provider', 'model_config')"
                    ),
                    {"tenant_id": PERMISSION_TENANT},
                )
            ).all()
            permissions = {(row.resource_type, row.action) for row in rows}
            assert permissions == {
                ("model_provider", action)
                for action in (
                    "create",
                    "read",
                    "list",
                    "update",
                    "delete",
                    "disable",
                    "execute",
                )
            } | {
                ("model_config", action)
                for action in (
                    "create",
                    "read",
                    "list",
                    "update",
                    "delete",
                    "publish",
                    "rollback",
                    "disable",
                )
            }
    finally:
        await engine.dispose()


async def verify_agent_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'agent'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {
                "create",
                "read",
                "list",
                "update",
                "delete",
                "disable",
                "publish",
            }
    finally:
        await engine.dispose()


async def verify_session_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'session'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {"create", "read", "list", "update", "delete"}
    finally:
        await engine.dispose()


async def verify_message_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'message'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {"read", "list"}
    finally:
        await engine.dispose()


async def verify_run_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id AND resource_type = 'run'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {"cancel", "create", "read", "list", "retry"}
    finally:
        await engine.dispose()


async def verify_approval_permission_backfill(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(PERMISSION_TENANT)},
            )
            actions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM role_permission "
                            "WHERE tenant_id = :tenant_id "
                            "AND resource_type = 'approval'"
                        ),
                        {"tenant_id": PERMISSION_TENANT},
                    )
                ).scalars()
            )
            assert actions == {"approve", "list", "read"}
    finally:
        await engine.dispose()


async def verify_model_resources(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    try:
        registry = SqlAlchemyResourceRegistry(create_session_factory(app_engine))
        provider_request = ResourceCreateRequest(
            code="primary_openai",
            name="Primary OpenAI",
            description=None,
            content_schema_version="1.0",
            content=ResourceContentModelProvider(
                resource_type="model_provider",
                provider_type="openai",
                base_url="https://api.openai.com/v1",
                secret_ref=f"secret://tenant/{TENANT_A}/model/openai",
                timeout_seconds=30,
                data_retention_policy=None,
            ),
        )
        provider = await registry.create_definition(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="model_provider",
            request=provider_request,
            idempotency_key="model-provider-create",
            request_hash=canonical_request_hash(
                "model_provider.create", provider_request
            ),
            metadata=METADATA,
        )
        assert provider.value is not None

        connection_test = await registry.request_model_provider_connection_test(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_id=provider.value.id,
            idempotency_key="model-provider-test",
            request_hash=canonical_request_hash(
                "model_provider.connection_test",
                extra={"resource_id": str(provider.value.id)},
            ),
            metadata=METADATA,
        )
        assert connection_test is not None
        assert connection_test.value is not None
        assert connection_test.value.status == "ACCEPTED"
        replay = await registry.request_model_provider_connection_test(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_id=provider.value.id,
            idempotency_key="model-provider-test",
            request_hash=canonical_request_hash(
                "model_provider.connection_test",
                extra={"resource_id": str(provider.value.id)},
            ),
            metadata=METADATA,
        )
        assert replay is not None
        assert replay.replay is not None
        assert replay.replay.response_body["operation_id"] == str(
            connection_test.value.id
        )

        session_factory = create_session_factory(app_engine)
        gateway_store = SqlAlchemyModelGatewayStore(session_factory)
        connection_handler = ModelProviderConnectionTestHandler(
            secrets=MappingSecretReferenceResolver(
                {
                    f"secret://tenant/{TENANT_A}/model/openai": SecretStr(
                        "integration-provider-secret"
                    )
                }
            ),
            adapters=ProviderAdapterRegistry(
                {"openai": IntegrationConnectionAdapter()}
            ),
            operations=gateway_store,
        )
        dispatcher = OutboxDispatcher(
            SqlAlchemyOutboxStore(session_factory),
            OutboxEventRouter(
                {"model_provider.connection_test_requested": connection_handler}
            ),
        )
        dispatch_summary = await dispatcher.dispatch_tenant_once(
            context(TENANT_A), now=datetime.now(UTC)
        )
        assert dispatch_summary.published == 1

        usage_started_at = datetime.now(UTC)
        await gateway_store.record(
            context(TENANT_A),
            ModelUsageRecord(
                id=UUID("55555555-5555-4555-8555-555555555555"),
                tenant_id=UUID(TENANT_A),
                run_id="run-model-gateway-integration",
                provider="openai",
                model="gpt-5-mini",
                provider_request_id="provider-request-integration",
                input_tokens=10,
                output_tokens=4,
                reasoning_tokens=1,
                cache_read_tokens=2,
                cache_write_tokens=0,
                token_estimated=False,
                cost_amount=None,
                cost_currency=None,
                started_at=usage_started_at,
                finished_at=datetime.now(UTC),
            ),
        )

        config_request = ResourceCreateRequest(
            code="gpt_default",
            name="GPT Default",
            description=None,
            content_schema_version="1.0",
            content=ResourceContentModelConfig(
                resource_type="model_config",
                provider_id=str(provider.value.id),
                model_id="gpt-5-mini",
                capabilities=["stream", "tools"],
                default_parameters={"temperature": 0.2},
                max_context_tokens=128000,
                rate_limit_rpm=60,
            ),
        )
        config = await registry.create_definition(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="model_config",
            request=config_request,
            idempotency_key="model-config-create",
            request_hash=canonical_request_hash("model_config.create", config_request),
            metadata=METADATA,
        )
        assert config.value is not None

        with pytest.raises(PlatformError) as cross_tenant:
            await registry.create_definition(
                context(TENANT_B),
                actor_id=ACTOR,
                resource_type="model_config",
                request=config_request.model_copy(
                    update={"code": "cross_tenant_model"}
                ),
                idempotency_key="model-config-cross-tenant",
                request_hash=canonical_request_hash(
                    "model_config.create",
                    config_request.model_copy(update={"code": "cross_tenant_model"}),
                ),
                metadata=METADATA,
            )
        assert cross_tenant.value.code == "RESOURCE_STATE_CONFLICT"

        disabled = await registry.set_definition_status(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="model_provider",
            resource_id=provider.value.id,
            expected_version=1,
            enabled=False,
            request=None,
            idempotency_key="model-provider-disable",
            request_hash=canonical_request_hash(
                "model_provider.disable", extra={"resource_id": str(provider.value.id)}
            ),
            metadata=METADATA,
        )
        assert disabled is not None
        assert disabled.value is not None
        assert disabled.value.status == "DISABLED"

        publish_request = ResourcePublishRequest(
            expected_resource_version=1, release_note="Initial model config"
        )
        with pytest.raises(PlatformError) as disabled_provider:
            await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="model_config",
                resource_id=config.value.id,
                request=publish_request,
                idempotency_key="model-config-publish-disabled",
                request_hash=canonical_request_hash(
                    "model_config.publish", publish_request
                ),
                metadata=METADATA,
            )
        assert disabled_provider.value.code == "RESOURCE_STATE_CONFLICT"

        enabled = await registry.set_definition_status(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="model_provider",
            resource_id=provider.value.id,
            expected_version=2,
            enabled=True,
            request=None,
            idempotency_key="model-provider-enable",
            request_hash=canonical_request_hash(
                "model_provider.enable", extra={"resource_id": str(provider.value.id)}
            ),
            metadata=METADATA,
        )
        assert enabled is not None
        assert enabled.value is not None
        assert enabled.value.status == "DRAFT"

        published = await registry.publish_version(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="model_config",
            resource_id=config.value.id,
            request=publish_request,
            idempotency_key="model-config-publish",
            request_hash=canonical_request_hash(
                "model_config.publish", publish_request
            ),
            metadata=METADATA,
        )
        assert published.value is not None
        assert published.value.version_no == 1

        binding_reader = SqlAlchemyModelBindingReader(session_factory)
        frozen_binding = await binding_reader.get_binding(
            context(TENANT_A), str(published.value.id)
        )
        frozen_route = frozen_binding.routes[0]
        assert frozen_route.provider == "openai"
        assert frozen_route.base_url == "https://api.openai.com/v1"
        assert frozen_route.secret_ref == (f"secret://tenant/{TENANT_A}/model/openai")
        assert frozen_route.default_parameters == {"temperature": 0.2}
        assert frozen_route.rate_limit_rpm == 60

        provider_update = ResourceUpdateRequest(
            name=None,
            description=None,
            visibility=None,
            content_schema_version=None,
            content=ResourceContentModelProvider(
                resource_type="model_provider",
                provider_type="openai",
                base_url="https://api.openai.com/v2",
                secret_ref=f"secret://tenant/{TENANT_A}/model/openai-v2",
                timeout_seconds=45,
                data_retention_policy=None,
            ),
        )
        updated_provider = await registry.update_definition(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="model_provider",
            resource_id=provider.value.id,
            expected_version=3,
            request=provider_update,
            metadata=METADATA,
        )
        assert updated_provider is not None
        assert updated_provider.resource_version == 4

        still_frozen = await binding_reader.get_binding(
            context(TENANT_A), str(published.value.id)
        )
        assert still_frozen.routes[0].base_url == "https://api.openai.com/v1"
        assert still_frozen.routes[0].timeout_seconds == 30

        rollback_request = ResourceRollbackRequest(
            version_id=str(published.value.id),
            expected_resource_version=2,
            release_note="Restore original frozen binding",
        )
        rolled_back = await registry.rollback_version(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="model_config",
            resource_id=config.value.id,
            source_version_id=published.value.id,
            request=rollback_request,
            idempotency_key="model-config-rollback",
            request_hash=canonical_request_hash(
                "model_config.rollback", rollback_request
            ),
            metadata=METADATA,
        )
        assert rolled_back is not None
        assert rolled_back.value is not None
        rollback_binding = await binding_reader.get_binding(
            context(TENANT_A), str(rolled_back.value.id)
        )
        assert rollback_binding.routes[0].base_url == "https://api.openai.com/v1"
        assert rollback_binding.routes[0].secret_ref.endswith("/model/openai")

        with pytest.raises(ProviderError) as cross_tenant_binding:
            await binding_reader.get_binding(context(TENANT_B), str(published.value.id))
        assert cross_tenant_binding.value.code == "MODEL_BINDING_NOT_FOUND"

        with pytest.raises(PlatformError) as referenced:
            await registry.delete_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="model_provider",
                resource_id=provider.value.id,
                expected_version=4,
                idempotency_key="model-provider-delete-referenced",
                request_hash=canonical_request_hash(
                    "model_provider.delete",
                    extra={"resource_id": str(provider.value.id)},
                ),
                metadata=METADATA,
            )
        assert referenced.value.code == "RESOURCE_STATE_CONFLICT"
    finally:
        await app_engine.dispose()

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            operation_status = await connection.scalar(
                text(
                    "SELECT status FROM operation_record "
                    "WHERE operation_type = 'model_provider.connection_test'"
                )
            )
            assert operation_status == "SUCCEEDED"
            payload = (
                await connection.execute(
                    text(
                        "SELECT payload_json FROM outbox_event "
                        "WHERE event_type = 'model_provider.connection_test_requested'"
                    )
                )
            ).scalar_one()
            assert payload["secret_ref"] == f"secret://tenant/{TENANT_A}/model/openai"
            assert "secret_value" not in payload
            audit = str(
                (
                    await connection.execute(
                        text(
                            "SELECT jsonb_agg(metadata_json) FROM audit_log "
                            "WHERE action = 'model_provider.connection_test.requested'"
                        )
                    )
                ).scalar_one()
            )
            assert "secret://" not in audit
            completed_audit = str(
                (
                    await connection.execute(
                        text(
                            "SELECT jsonb_agg(metadata_json) FROM audit_log "
                            "WHERE action = 'model_provider.connection_test.completed'"
                        )
                    )
                ).scalar_one()
            )
            assert "integration-provider-secret" not in completed_audit
            usage = (
                await connection.execute(
                    text(
                        "SELECT input_tokens, output_tokens, reasoning_tokens, "
                        "cache_read_tokens, cache_write_tokens, token_estimated "
                        "FROM model_usage WHERE run_id = "
                        "'run-model-gateway-integration'"
                    )
                )
            ).one()
            assert tuple(usage) == (10, 4, 1, 2, 0, False)
    finally:
        await engine.dispose()


async def verify_agent_draft(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    try:
        session_factory = create_session_factory(app_engine)
        resources = SqlAlchemyResourceRegistry(session_factory)
        agents = SqlAlchemyAgentRegistry(session_factory)
        snapshots = SqlAlchemySnapshotCompilationStore(session_factory)
        bundle_inputs = SqlAlchemyBundleInputReader(session_factory)
        references = SqlAlchemyAgentResourceReferenceProvider(session_factory)
        prompts, _ = await resources.list_definitions(
            context(TENANT_A),
            resource_type="prompt",
            limit=20,
            cursor=None,
            keyword="welcome_prompt",
        )
        model_configs, _ = await resources.list_definitions(
            context(TENANT_A),
            resource_type="model_config",
            limit=20,
            cursor=None,
            keyword="gpt_default",
        )
        prompt = next(item for item in prompts if item.code == "welcome_prompt")
        model_config = next(
            item for item in model_configs if item.code == "gpt_default"
        )
        prompt_versions, _ = await resources.list_versions(
            context(TENANT_A),
            resource_type="prompt",
            resource_id=prompt.id,
            limit=20,
            cursor=None,
        )
        model_versions, _ = await resources.list_versions(
            context(TENANT_A),
            resource_type="model_config",
            resource_id=model_config.id,
            limit=20,
            cursor=None,
        )
        assert prompt_versions
        assert model_versions

        child_request = AgentCreateRequest.model_validate(
            {
                "code": "child_agent",
                "name": "Child Agent",
                "runtime_type": "agentscope",
            }
        )
        child = await agents.create_agent(
            context(TENANT_A),
            actor_id=ACTOR,
            request=child_request,
            idempotency_key="agent-child-create",
            request_hash=canonical_request_hash("agent.create", child_request),
            metadata=METADATA,
        )
        assert child.value is not None

        create_request = AgentCreateRequest.model_validate(
            {
                "code": "support_agent",
                "name": "Support Agent",
                "runtime_type": "agentscope",
                "visibility": "tenant",
                "tags": ["support"],
            }
        )
        created = await agents.create_agent(
            context(TENANT_A),
            actor_id=ACTOR,
            request=create_request,
            idempotency_key="agent-create",
            request_hash=canonical_request_hash("agent.create", create_request),
            metadata=METADATA,
        )
        assert created.value is not None
        replay = await agents.create_agent(
            context(TENANT_A),
            actor_id=ACTOR,
            request=create_request,
            idempotency_key="agent-create",
            request_hash=canonical_request_hash("agent.create", create_request),
            metadata=METADATA,
        )
        assert replay.replay is not None

        binding_records = [
            AgentBindingRecord(
                resource_type="model",
                resource_id=model_config.id,
                version_policy="fixed",
                version_id=model_versions[0].id,
                binding_role="primary",
                configuration_schema_version="model-routing/v1",
                configuration={
                    "fallback_error_codes": [
                        "RATE_LIMITED",
                        "PROVIDER_UNAVAILABLE",
                    ]
                },
            ),
            AgentBindingRecord(
                resource_type="prompt",
                resource_id=prompt.id,
                version_policy="resolve_on_publish",
                version_id=None,
                binding_role=None,
                configuration_schema_version=None,
                configuration=None,
            ),
            AgentBindingRecord(
                resource_type="agent",
                resource_id=child.value.id,
                version_policy="resolve_on_publish",
                version_id=None,
                binding_role=None,
                configuration_schema_version=None,
                configuration=None,
            ),
        ]
        update_request = AgentUpdateRequest.model_validate(
            {
                "description": "Routes support requests",
                "bindings": [
                    {
                        "resource_type": binding.resource_type,
                        "resource_id": str(binding.resource_id),
                        "version_policy": binding.version_policy,
                        "version_id": (
                            str(binding.version_id)
                            if binding.version_id is not None
                            else None
                        ),
                        "binding_role": binding.binding_role,
                        "configuration_schema_version": (
                            binding.configuration_schema_version
                        ),
                        "configuration": binding.configuration,
                    }
                    for binding in binding_records
                ],
            }
        )
        updated = await agents.update_agent(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=created.value.id,
            expected_version=1,
            request=update_request,
            bindings=binding_records,
            metadata=METADATA,
        )
        assert updated is not None
        assert updated.resource_version == 2
        assert [binding.binding_role for binding in updated.bindings[:1]] == ["primary"]
        assert (
            await agents.get_agent(context(TENANT_B), agent_id=created.value.id) is None
        )
        child_references = await agents.list_agent_references(
            context(TENANT_A), agent_id=child.value.id
        )
        assert child_references is not None
        assert [reference.resource_id for reference in child_references] == [
            created.value.id
        ]
        assert child_references[0].reference_type == "child_agent"
        assert (
            await agents.list_agent_references(
                context(TENANT_B), agent_id=child.value.id
            )
            is None
        )

        with pytest.raises(PlatformError) as unpublished_child:
            await snapshots.compile_snapshot(
                context(TENANT_A),
                actor_id=ACTOR,
                agent_id=created.value.id,
                expected_draft_resource_version=2,
                release_note="Child is not published yet",
                idempotency_key="agent-snapshot-before-child",
                request_hash=canonical_request_hash(
                    "agent.snapshot.compile",
                    extra={"agent_id": str(created.value.id), "draft_version": 2},
                ),
                metadata=METADATA,
            )
        assert unpublished_child.value.code == "RESOURCE_STATE_CONFLICT"

        child_publication = await snapshots.compile_snapshot(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=child.value.id,
            expected_draft_resource_version=1,
            release_note="Publish child",
            idempotency_key="agent-child-snapshot-v1",
            request_hash=canonical_request_hash(
                "agent.snapshot.compile",
                extra={"agent_id": str(child.value.id), "draft_version": 1},
            ),
            metadata=METADATA,
        )
        assert child_publication is not None
        assert child_publication.version.version_no == 1
        assert child_publication.snapshot.content["bindings"] == []

        publication_hash = canonical_request_hash(
            "agent.snapshot.compile",
            extra={"agent_id": str(created.value.id), "draft_version": 2},
        )
        concurrent_publications = await asyncio.gather(
            *(
                snapshots.compile_snapshot(
                    context(TENANT_A),
                    actor_id=ACTOR,
                    agent_id=created.value.id,
                    expected_draft_resource_version=2,
                    release_note="Publish support Agent",
                    idempotency_key="agent-snapshot-v1",
                    request_hash=publication_hash,
                    metadata=METADATA,
                )
                for _ in range(2)
            )
        )
        publication = concurrent_publications[0]
        assert publication is not None
        assert concurrent_publications[1] is not None
        assert concurrent_publications[1].snapshot.id == publication.snapshot.id
        assert {
            item.replayed for item in concurrent_publications if item is not None
        } == {
            False,
            True,
        }
        assert publication.version.version_no == 1
        assert publication.snapshot.schema_version == "agent-snapshot/v1"
        assert publication.snapshot.content_hash.startswith("sha256:")
        assert (
            await snapshots.get_snapshot(
                context(TENANT_A), snapshot_id=publication.snapshot.id
            )
            == publication.snapshot
        )
        assert (
            await snapshots.get_version(
                context(TENANT_A), agent_version_id=publication.version.id
            )
            == publication.version
        )
        assert (
            await snapshots.get_snapshot(
                context(TENANT_B), snapshot_id=publication.snapshot.id
            )
            is None
        )
        compiled_bindings = publication.snapshot.content["bindings"]
        assert isinstance(compiled_bindings, list)
        typed_bindings = cast(list[dict[str, object]], compiled_bindings)
        assert {item["resource_type"] for item in typed_bindings} == {
            "agent",
            "model",
            "prompt",
        }
        model_routing = publication.snapshot.content["model_routing"]
        assert isinstance(model_routing, dict)
        assert model_routing["fallback_error_codes"] == [
            "RATE_LIMITED",
            "PROVIDER_UNAVAILABLE",
        ]
        prompt_binding = next(
            item for item in typed_bindings if item["resource_type"] == "prompt"
        )
        assert prompt_binding["version_id"] == str(prompt_versions[0].id)
        immutable_prompt = await bundle_inputs.get_resource_version(
            context(TENANT_A),
            resource_id=prompt.id,
            version_id=prompt_versions[0].id,
        )
        assert immutable_prompt is not None
        assert immutable_prompt.content_hash == prompt_binding["content_hash"]
        assert (
            await bundle_inputs.get_resource_version(
                context(TENANT_B),
                resource_id=prompt.id,
                version_id=prompt_versions[0].id,
            )
            is None
        )
        model_binding = next(
            item for item in typed_bindings if item["resource_type"] == "model"
        )
        frozen_model_value = model_binding["model_binding_snapshot"]
        assert isinstance(frozen_model_value, dict)
        frozen_model = cast(dict[str, object], frozen_model_value)
        immutable_model = await bundle_inputs.get_model_binding_snapshot(
            context(TENANT_A), snapshot_id=UUID(str(frozen_model["id"]))
        )
        assert immutable_model is not None
        assert immutable_model.model_config_version_id == model_versions[0].id
        assert immutable_model.snapshot_hash == frozen_model["content_hash"]
        assert (
            await bundle_inputs.get_model_binding_snapshot(
                context(TENANT_B), snapshot_id=immutable_model.id
            )
            is None
        )

        prompt_references = await references.list_references(
            context(TENANT_A),
            target_type="prompt",
            target_id=prompt.id,
            limit=20,
            after=None,
        )
        assert {reference.reference_type for reference in prompt_references} == {
            "draft_binding",
            "snapshot",
        }
        first_reference_page = await references.list_references(
            context(TENANT_A),
            target_type="prompt",
            target_id=prompt.id,
            limit=1,
            after=None,
        )
        assert len(first_reference_page) == 1
        second_reference_page = await references.list_references(
            context(TENANT_A),
            target_type="prompt",
            target_id=prompt.id,
            limit=1,
            after=first_reference_page[0],
        )
        assert len(second_reference_page) == 1
        assert {
            first_reference_page[0].reference_type,
            second_reference_page[0].reference_type,
        } == {"draft_binding", "snapshot"}
        assert not await references.list_references(
            context(TENANT_B),
            target_type="prompt",
            target_id=prompt.id,
            limit=20,
            after=None,
        )

        cycle_binding = AgentBindingRecord(
            resource_type="agent",
            resource_id=created.value.id,
            version_policy="resolve_on_publish",
            version_id=None,
            binding_role=None,
            configuration_schema_version=None,
            configuration=None,
        )
        child_with_cycle = await agents.update_agent(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=child.value.id,
            expected_version=2,
            request=AgentUpdateRequest.model_validate(
                {
                    "bindings": [
                        {
                            "resource_type": "agent",
                            "resource_id": str(created.value.id),
                            "version_policy": "resolve_on_publish",
                        }
                    ]
                }
            ),
            bindings=[cycle_binding],
            metadata=METADATA,
        )
        assert child_with_cycle is not None
        with pytest.raises(PlatformError) as recursive:
            await snapshots.compile_snapshot(
                context(TENANT_A),
                actor_id=ACTOR,
                agent_id=child.value.id,
                expected_draft_resource_version=3,
                release_note="Recursive release",
                idempotency_key="agent-child-snapshot-recursive",
                request_hash=canonical_request_hash(
                    "agent.snapshot.compile",
                    extra={"agent_id": str(child.value.id), "draft_version": 3},
                ),
                metadata=METADATA,
            )
        assert recursive.value.code == "RESOURCE_STATE_CONFLICT"

        with pytest.raises(DBAPIError):
            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_A},
                )
                await connection.execute(
                    text(
                        "UPDATE agent_snapshot SET schema_version = 'changed' "
                        "WHERE id = :snapshot_id"
                    ),
                    {"snapshot_id": publication.snapshot.id},
                )

        with pytest.raises(PlatformError) as stale:
            await agents.update_agent(
                context(TENANT_A),
                actor_id=ACTOR,
                agent_id=created.value.id,
                expected_version=1,
                request=AgentUpdateRequest.model_validate({"name": "Stale"}),
                bindings=None,
                metadata=METADATA,
            )
        assert stale.value.code == "RESOURCE_VERSION_CONFLICT"

        copy_request = CopyAgentRequest(code="support_agent_copy", name="Support Copy")
        copied = await agents.copy_agent(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=created.value.id,
            request=copy_request,
            idempotency_key="agent-copy",
            request_hash=canonical_request_hash("agent.copy", copy_request),
            metadata=METADATA,
        )
        assert copied is not None
        assert copied.value is not None
        assert len(copied.value.bindings) == 3

        disabled = await agents.set_agent_disabled(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=created.value.id,
            expected_version=3,
            request=None,
            idempotency_key="agent-disable",
            request_hash=canonical_request_hash("agent.disable"),
            metadata=METADATA,
        )
        assert disabled is not None
        assert disabled.value is not None
        assert disabled.value.status == "DISABLED"

        with pytest.raises(PlatformError) as referenced:
            await agents.delete_agent(
                context(TENANT_A),
                actor_id=ACTOR,
                agent_id=child.value.id,
                expected_version=3,
                idempotency_key="agent-child-delete",
                request_hash=canonical_request_hash("agent.delete"),
                metadata=METADATA,
            )
        assert referenced.value.code == "RESOURCE_STATE_CONFLICT"

        deleted = await agents.delete_agent(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=copied.value.id,
            expected_version=1,
            idempotency_key="agent-copy-delete",
            request_hash=canonical_request_hash("agent.delete"),
            metadata=METADATA,
        )
        assert deleted is not None
        assert deleted.value is not None
        assert deleted.value.status == "SUCCEEDED"
        assert (
            await agents.get_agent(context(TENANT_A), agent_id=copied.value.id) is None
        )
    finally:
        await app_engine.dispose()


async def verify_release_request_and_failure_protection(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    try:
        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        releases = SqlAlchemyReleaseStore(session_factory)
        records, _ = await agents.list_agents(
            context(TENANT_A),
            limit=20,
            cursor=None,
            status=None,
            keyword=None,
        )
        agent = next(item for item in records if item.status == "ACTIVE")
        request_hash = canonical_request_hash(
            "agent.release.request",
            extra={
                "agent_id": str(agent.id),
                "expected_agent_version": agent.resource_version,
                "runtime_targets": ["rt_agentscope_default"],
            },
        )
        requested = await releases.request_release(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=agent.id,
            expected_agent_version=agent.resource_version,
            runtime_targets=("rt_agentscope_default",),
            release_note="Release workflow integration",
            run_smoke_test=True,
            activate_on_success=True,
            idempotency_key="release-request-integration",
            request_hash=request_hash,
            metadata=METADATA,
        )
        assert requested is not None
        assert requested.value is not None
        release = requested.value
        replay = await releases.request_release(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=agent.id,
            expected_agent_version=agent.resource_version,
            runtime_targets=("rt_agentscope_default",),
            release_note="Release workflow integration",
            run_smoke_test=True,
            activate_on_success=True,
            idempotency_key="release-request-integration",
            request_hash=request_hash,
            metadata=METADATA,
        )
        assert replay is not None
        assert replay.replay is not None
        assert replay.replay.response_body["release_id"] == str(release.id)
        validating = await releases.transition_release(
            context(TENANT_A),
            release_id=release.id,
            expected_status="REQUESTED",
            target_status="VALIDATING",
        )
        assert validating.status == "VALIDATING"
        failed = await releases.fail_release(
            context(TENANT_A),
            release_id=release.id,
            error_code="RUNTIME_IMAGE_DIGEST_UNAVAILABLE",
            error_detail={"message": "Registry digest is unavailable."},
        )
        assert failed.status == "FAILED"
        current_agent = await agents.get_agent(context(TENANT_A), agent_id=agent.id)
        assert current_agent is not None
        assert current_agent.active_deployment_id is None
    finally:
        await app_engine.dispose()

    admin_engine = create_async_engine(database_url)
    try:
        async with admin_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT r.status, o.status, count(e.id) "
                        "FROM release r "
                        "JOIN operation_record o ON o.id = r.operation_id "
                        "JOIN outbox_event e ON e.aggregate_id = r.id "
                        "WHERE r.id = :release_id GROUP BY r.status, o.status"
                    ),
                    {"release_id": release.id},
                )
            ).one()
            assert tuple(row) == ("FAILED", "FAILED", 1)
    finally:
        await admin_engine.dispose()


async def verify_deployment_activation_history_and_fencing(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    try:
        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        releases = SqlAlchemyReleaseStore(session_factory)
        target_configs = {
            target: RuntimeTargetReleaseConfig(
                runtime_type="agentscope",
                image_digest="registry.example/agentscope@sha256:" + "d" * 64,
            )
            for target in (
                "rt_agentscope_a",
                "rt_agentscope_b",
                "rt_agentscope_missing",
            )
        }
        deployments = SqlAlchemyDeploymentStore(session_factory, target_configs)
        records, _ = await agents.list_agents(
            context(TENANT_A),
            limit=20,
            cursor=None,
            status=None,
            keyword=None,
        )
        agent = next(item for item in records if item.status == "ACTIVE")
        async with TenantUnitOfWork(session_factory, context(TENANT_A)) as unit_of_work:
            snapshot_id = await unit_of_work.session.scalar(
                select(AgentSnapshotModel.id)
                .join(
                    AgentVersionModel,
                    (AgentVersionModel.tenant_id == AgentSnapshotModel.tenant_id)
                    & (AgentVersionModel.id == AgentSnapshotModel.agent_version_id),
                )
                .where(
                    AgentSnapshotModel.tenant_id == UUID(TENANT_A),
                    AgentVersionModel.agent_id == agent.id,
                )
                .order_by(AgentVersionModel.version_no.desc())
                .limit(1)
            )
        assert snapshot_id is not None
        bundle = RuntimeBundleRecord(
            id=uuid5(NAMESPACE_URL, f"integration-bundle/{snapshot_id}"),
            tenant_id=UUID(TENANT_A),
            snapshot_id=snapshot_id,
            runtime_type="agentscope",
            compiler_name=BUNDLE_COMPILER_NAME,
            compiler_version=BUNDLE_COMPILER_VERSION,
            manifest_schema_version="1.0",
            manifest=admitted_manifest(),
            content_hash="sha256:" + "e" * 64,
            object_uri="s3://integration/bundles/agentscope.tar",
            size_bytes=1024,
            signature_ref="sigstore://integration/agentscope",
            sbom_ref="s3://integration/bundles/agentscope.spdx.json",
            scan_status="PASSED",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        )
        await releases.store_runtime_bundle(context(TENANT_A), record=bundle)

        async def ready_release(suffix: str, targets: tuple[str, ...]):
            request_hash = canonical_request_hash(
                "agent.release.request",
                extra={
                    "agent_id": str(agent.id),
                    "expected_agent_version": agent.resource_version,
                    "runtime_targets": list(targets),
                    "suffix": suffix,
                },
            )
            outcome = await releases.request_release(
                context(TENANT_A),
                actor_id=ACTOR,
                agent_id=agent.id,
                expected_agent_version=agent.resource_version,
                runtime_targets=targets,
                release_note=f"Deployment activation {suffix}",
                run_smoke_test=False,
                activate_on_success=True,
                idempotency_key=f"deployment-activation-{suffix}",
                request_hash=request_hash,
                metadata=METADATA,
            )
            assert outcome is not None and outcome.value is not None
            current = await releases.transition_release(
                context(TENANT_A),
                release_id=outcome.value.id,
                expected_status="REQUESTED",
                target_status="VALIDATING",
            )
            current = await releases.transition_release(
                context(TENANT_A),
                release_id=current.id,
                expected_status="VALIDATING",
                target_status="COMPILING",
                snapshot_id=snapshot_id,
            )
            current = await releases.transition_release(
                context(TENANT_A),
                release_id=current.id,
                expected_status="COMPILING",
                target_status="SCANNING",
            )
            return await releases.transition_release(
                context(TENANT_A),
                release_id=current.id,
                expected_status="SCANNING",
                target_status="ACTIVATING",
            )

        targets = ("rt_agentscope_a", "rt_agentscope_b")
        first = await ready_release("first", targets)
        first_ids = await deployments.activate(
            context(TENANT_A), release=first, bundles=(bundle,)
        )
        assert len(first_ids) == 2
        assert first_ids == await deployments.activate(
            context(TENANT_A), release=first, bundles=(bundle,)
        )

        second = await ready_release("second", targets)
        second_ids = await deployments.activate(
            context(TENANT_A), release=second, bundles=(bundle,)
        )
        assert len(second_ids) == 2
        assert set(first_ids).isdisjoint(second_ids)
        with pytest.raises(PlatformError) as fenced:
            await deployments.activate(
                context(TENANT_A), release=first, bundles=(bundle,)
            )
        assert fenced.value.code == "RESOURCE_STATE_CONFLICT"

        failed = await ready_release(
            "prevalidation-failure",
            ("rt_agentscope_a", "rt_not_configured"),
        )
        with pytest.raises(PlatformError):
            await deployments.activate(
                context(TENANT_A), release=failed, bundles=(bundle,)
            )

        older = await ready_release("concurrent-older", targets)
        newer = await ready_release("concurrent-newer", targets)
        await asyncio.gather(
            deployments.activate(context(TENANT_A), release=older, bundles=(bundle,)),
            deployments.activate(context(TENANT_A), release=newer, bundles=(bundle,)),
            return_exceptions=True,
        )
        current_agent = await agents.get_agent(context(TENANT_A), agent_id=agent.id)
        assert current_agent is not None
        assert current_agent.active_deployment_id is not None
        default_deployment = await deployments.get_deployment(
            context(TENANT_A),
            deployment_id=current_agent.active_deployment_id,
        )
        assert default_deployment is not None
        assert default_deployment.release_id == newer.id
        assert default_deployment.status == "ACTIVE"
        assert (
            await deployments.get_deployment(
                context(TENANT_B),
                deployment_id=current_agent.active_deployment_id,
            )
            is None
        )

        admin_engine = create_async_engine(database_url)
        try:
            async with admin_engine.connect() as connection:
                active_rows = (
                    await connection.execute(
                        text(
                            "SELECT runtime_target_id, count(*) FROM deployment "
                            "WHERE tenant_id = :tenant_id AND agent_id = :agent_id "
                            "AND status = 'ACTIVE' GROUP BY runtime_target_id"
                        ),
                        {"tenant_id": TENANT_A, "agent_id": agent.id},
                    )
                ).all()
                assert sorted(tuple(row) for row in active_rows) == [
                    ("rt_agentscope_a", 1),
                    ("rt_agentscope_b", 1),
                ]
                history = (
                    await connection.execute(
                        text(
                            "SELECT status, count(*) FROM deployment "
                            "WHERE tenant_id = :tenant_id AND agent_id = :agent_id "
                            "GROUP BY status"
                        ),
                        {"tenant_id": TENANT_A, "agent_id": agent.id},
                    )
                ).all()
                counts = {str(row[0]): int(row[1]) for row in history}
                assert counts["ACTIVE"] == 2
                assert counts["RETIRED"] >= 4
        finally:
            await admin_engine.dispose()
    finally:
        await app_engine.dispose()


async def verify_session_lifecycle_and_deployment_pinning(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        sessions = SqlAlchemySessionStore(session_factory)
        agent_records, _ = await agents.list_agents(
            context(TENANT_A),
            limit=20,
            cursor=None,
            status=None,
            keyword=None,
        )
        agent = next(item for item in agent_records if item.active_deployment_id)
        create_request = SessionCreateRequest.model_validate(
            {
                "agent_id": str(agent.id),
                "title": "Pinned before rollback",
                "metadata": {"locale": "zh-CN", "tags": ["integration"]},
            }
        )
        create_hash = canonical_request_hash("session.create", create_request)
        created = await sessions.create_session(
            context(TENANT_A),
            user_id=ACTOR,
            request=create_request,
            metadata_value={"locale": "zh-CN", "tags": ["integration"]},
            idempotency_key="session-create-pinned",
            request_hash=create_hash,
            metadata=METADATA,
        )
        assert created.value is not None
        pinned = created.value
        assert pinned.default_deployment_id == agent.active_deployment_id
        replay = await sessions.create_session(
            context(TENANT_A),
            user_id=ACTOR,
            request=create_request,
            metadata_value={"locale": "zh-CN", "tags": ["integration"]},
            idempotency_key="session-create-pinned",
            request_hash=create_hash,
            metadata=METADATA,
        )
        assert replay.replay is not None
        assert replay.replay.response_body["id"] == str(pinned.id)
        assert (
            await sessions.get_session(
                context(TENANT_A), user_id=OTHER_ACTOR, session_id=pinned.id
            )
            is None
        )
        assert (
            await sessions.get_session(
                context(TENANT_B), user_id=ACTOR, session_id=pinned.id
            )
            is None
        )

        delete_request = SessionCreateRequest.model_validate(
            {"agent_id": str(agent.id), "title": "Delete lifecycle"}
        )
        deleting = await sessions.create_session(
            context(TENANT_A),
            user_id=ACTOR,
            request=delete_request,
            metadata_value={},
            idempotency_key="session-create-delete-lifecycle",
            request_hash=canonical_request_hash("session.create", delete_request),
            metadata=METADATA,
        )
        assert deleting.value is not None
        updated = await sessions.update_session(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=deleting.value.id,
            expected_version=1,
            request=SessionUpdateRequest.model_validate({"title": "Renamed"}),
            metadata_value=None,
            metadata=METADATA,
        )
        assert updated is not None and updated.resource_version == 2
        with pytest.raises(PlatformError) as active_delete:
            await sessions.delete_session(
                context(TENANT_A),
                user_id=ACTOR,
                session_id=updated.id,
                expected_version=2,
                idempotency_key="session-delete-active",
                request_hash=canonical_request_hash(
                    "session.delete",
                    extra={"session_id": str(updated.id), "if_match": '"rv:2"'},
                ),
                metadata=METADATA,
            )
        assert active_delete.value.code == "RESOURCE_STATE_CONFLICT"
        archived = await sessions.archive_session(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=updated.id,
            expected_version=2,
            idempotency_key="session-archive-lifecycle",
            request_hash=canonical_request_hash(
                "session.archive",
                extra={"session_id": str(updated.id), "if_match": '"rv:2"'},
            ),
            metadata=METADATA,
        )
        assert archived is not None and archived.value is not None
        assert archived.value.status == "ARCHIVED"
        deleted = await sessions.delete_session(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=updated.id,
            expected_version=3,
            idempotency_key="session-delete-lifecycle",
            request_hash=canonical_request_hash(
                "session.delete",
                extra={"session_id": str(updated.id), "if_match": '"rv:3"'},
            ),
            metadata=METADATA,
        )
        assert deleted is not None and deleted.value is not None
        assert deleted.value.status == "SUCCEEDED"

        visible, next_cursor = await sessions.list_sessions(
            context(TENANT_A),
            user_id=ACTOR,
            limit=1,
            cursor=None,
            agent_id=agent.id,
            status=None,
        )
        assert len(visible) == 1
        assert visible[0].id == pinned.id
        assert next_cursor is None
        tombstones, _ = await sessions.list_sessions(
            context(TENANT_A),
            user_id=ACTOR,
            limit=20,
            cursor=None,
            agent_id=agent.id,
            status="DELETED",
        )
        assert [item.id for item in tombstones] == [updated.id]

        async with admin_engine.connect() as connection:
            operation_status = await connection.scalar(
                text(
                    "SELECT status FROM operation_record "
                    "WHERE tenant_id = :tenant_id AND resource_type = 'session' "
                    "AND resource_id = :session_id"
                ),
                {"tenant_id": TENANT_A, "session_id": updated.id},
            )
            audit_payload = str(
                await connection.scalar(
                    text(
                        "SELECT jsonb_agg(metadata_json) FROM audit_log "
                        "WHERE tenant_id = :tenant_id AND resource_type = 'session'"
                    ),
                    {"tenant_id": TENANT_A},
                )
            )
        assert operation_status == "SUCCEEDED"
        assert "Pinned before rollback" not in audit_payload
        assert "Renamed" not in audit_payload
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_message_history_branching_and_immutability(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    message_ids = [uuid5(NAMESPACE_URL, f"message-{index}") for index in range(1, 6)]
    branch_id = uuid5(NAMESPACE_URL, "message-branch-b")
    try:
        async with admin_engine.begin() as connection:
            session_id = await connection.scalar(
                text(
                    "SELECT id FROM chat_session WHERE tenant_id = :tenant_id "
                    "AND title = 'Pinned before rollback'"
                ),
                {"tenant_id": TENANT_A},
            )
            deleted_session_id = await connection.scalar(
                text(
                    "SELECT id FROM chat_session WHERE tenant_id = :tenant_id "
                    "AND status = 'DELETED'"
                ),
                {"tenant_id": TENANT_A},
            )
            assert session_id is not None and deleted_session_id is not None
            await connection.execute(
                text(
                    "INSERT INTO chat_message "
                    "(id, tenant_id, session_id, branch_id, parent_message_id, "
                    "role, content_parts_json, created_by) VALUES "
                    "(:m1, :tenant_id, :session_id, NULL, NULL, 'USER', "
                    '\'[ {"type": "text", "text": "m1"} ]\'::jsonb, :actor), '
                    "(:m2, :tenant_id, :session_id, NULL, :m1, 'ASSISTANT', "
                    '\'[ {"type": "text", "text": "m2"} ]\'::jsonb, :actor), '
                    "(:b1, :tenant_id, :session_id, :branch_id, :m2, 'USER', "
                    '\'[ {"type": "text", "text": "b1"} ]\'::jsonb, :actor), '
                    "(:b2, :tenant_id, :session_id, :branch_id, :b1, 'ASSISTANT', "
                    '\'[ {"type": "text", "text": "b2"} ]\'::jsonb, :actor)'
                ),
                {
                    "m1": message_ids[0],
                    "m2": message_ids[1],
                    "b1": message_ids[2],
                    "b2": message_ids[3],
                    "tenant_id": TENANT_A,
                    "session_id": session_id,
                    "branch_id": branch_id,
                    "actor": ACTOR,
                },
            )
            await connection.execute(
                text(
                    "UPDATE chat_session SET cursor_message_id = :cursor "
                    "WHERE tenant_id = :tenant_id AND id = :session_id"
                ),
                {
                    "cursor": message_ids[3],
                    "tenant_id": TENANT_A,
                    "session_id": session_id,
                },
            )

        messages = SqlAlchemyMessageHistoryStore(create_session_factory(app_engine))
        first_page = await messages.list_session_messages(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=session_id,
            limit=2,
            cursor=None,
            branch_id=None,
        )
        assert first_page is not None
        first_records, frozen_cursor = first_page
        assert [record.id for record in first_records] == message_ids[:2]
        assert frozen_cursor is not None

        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO chat_message "
                    "(id, tenant_id, session_id, branch_id, parent_message_id, "
                    "role, content_parts_json, created_by) VALUES "
                    "(:id, :tenant_id, :session_id, :branch_id, :parent_id, "
                    "'ASSISTANT', "
                    '\'[ {"type": "text", "text": "b3"} ]\'::jsonb, :actor)'
                ),
                {
                    "id": message_ids[4],
                    "tenant_id": TENANT_A,
                    "session_id": session_id,
                    "branch_id": branch_id,
                    "parent_id": message_ids[3],
                    "actor": ACTOR,
                },
            )
            await connection.execute(
                text(
                    "UPDATE chat_session SET cursor_message_id = :cursor "
                    "WHERE tenant_id = :tenant_id AND id = :session_id"
                ),
                {
                    "cursor": message_ids[4],
                    "tenant_id": TENANT_A,
                    "session_id": session_id,
                },
            )

        second_page = await messages.list_session_messages(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=session_id,
            limit=2,
            cursor=frozen_cursor,
            branch_id=None,
        )
        assert second_page is not None
        second_records, next_cursor = second_page
        assert [record.id for record in second_records] == message_ids[2:4]
        assert next_cursor is None

        branch_history = await messages.list_session_messages(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=session_id,
            limit=20,
            cursor=None,
            branch_id=branch_id,
        )
        assert branch_history is not None
        assert [record.id for record in branch_history[0]] == message_ids
        assert (
            await messages.list_session_messages(
                context(TENANT_A),
                user_id=OTHER_ACTOR,
                session_id=session_id,
                limit=20,
                cursor=None,
                branch_id=None,
            )
            is None
        )
        assert (
            await messages.list_session_messages(
                context(TENANT_B),
                user_id=ACTOR,
                session_id=session_id,
                limit=20,
                cursor=None,
                branch_id=None,
            )
            is None
        )
        assert (
            await messages.list_session_messages(
                context(TENANT_A),
                user_id=ACTOR,
                session_id=deleted_session_id,
                limit=20,
                cursor=None,
                branch_id=None,
            )
            is None
        )

        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE chat_message SET role = 'SYSTEM' WHERE id = :id"),
                    {"id": message_ids[0]},
                )
        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM chat_message WHERE id = :id"),
                    {"id": message_ids[0]},
                )
        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO chat_message "
                        "(tenant_id, session_id, parent_message_id, role, "
                        "content_parts_json, created_by) VALUES "
                        "(:tenant_id, :session_id, :parent_id, 'USER', "
                        '\'[ {"type": "text", "text": "x"} ]\'::jsonb, :actor)'
                    ),
                    {
                        "tenant_id": TENANT_A,
                        "session_id": deleted_session_id,
                        "parent_id": message_ids[0],
                        "actor": ACTOR,
                    },
                )
        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO chat_message "
                        "(tenant_id, session_id, role, content_parts_json, created_by) "
                        "VALUES (:tenant_id, :session_id, 'USER', '[]'::jsonb, :actor)"
                    ),
                    {
                        "tenant_id": TENANT_A,
                        "session_id": session_id,
                        "actor": ACTOR,
                    },
                )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_session_remains_pinned_after_rollback(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT s.default_deployment_id, a.active_deployment_id, "
                        "d.status FROM chat_session AS s "
                        "JOIN agent_definition AS a ON a.tenant_id = s.tenant_id "
                        "AND a.id = s.agent_id "
                        "JOIN deployment AS d ON d.tenant_id = s.tenant_id "
                        "AND d.id = s.default_deployment_id "
                        "WHERE s.tenant_id = :tenant_id "
                        "AND s.title = 'Pinned before rollback'"
                    ),
                    {"tenant_id": TENANT_A},
                )
            ).one()
        assert row.default_deployment_id != row.active_deployment_id
        assert row.status == "RETIRED"
    finally:
        await engine.dispose()


async def verify_run_uses_retired_session_deployment(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        async with admin_engine.connect() as connection:
            session_row = (
                await connection.execute(
                    text(
                        "SELECT id, default_deployment_id FROM chat_session "
                        "WHERE tenant_id = :tenant_id "
                        "AND title = 'Pinned before rollback'"
                    ),
                    {"tenant_id": TENANT_A},
                )
            ).one()
        request = RunCreateRequest.model_validate(
            {"session_id": str(session_row.id), "input": {"text": "after rollback"}}
        )
        created = await SqlAlchemyRunStore(
            create_session_factory(app_engine)
        ).create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=request,
            idempotency_key="run-create-retired-default",
            request_hash=canonical_request_hash("run.create", request),
            metadata=METADATA,
        )
        assert created.value is not None
        assert created.value.deployment_id == session_row.default_deployment_id
        async with admin_engine.connect() as connection:
            status = await connection.scalar(
                text(
                    "SELECT status FROM deployment WHERE tenant_id = :tenant_id "
                    "AND id = :deployment_id"
                ),
                {
                    "tenant_id": TENANT_A,
                    "deployment_id": created.value.deployment_id,
                },
            )
        assert status == "RETIRED"
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_epic3_vertical_acceptance(database_url: str) -> None:
    """Drive the real Run API through Outbox and Temporal with a Candidate Fake."""

    if os.getenv("AP_TEST_TEMPORAL") != "1":
        return
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        now = datetime.now(UTC)
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE outbox_event SET status = 'PUBLISHED', "
                    "published_at = COALESCE(published_at, :now), "
                    "next_attempt_at = :now WHERE status <> 'PUBLISHED'"
                ),
                {"now": now},
            )

        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        sessions = SqlAlchemySessionStore(session_factory)
        runs = SqlAlchemyRunStore(session_factory)
        agent_records, _ = await agents.list_agents(
            context(TENANT_A), limit=20, cursor=None, status=None, keyword=None
        )
        agent = next(item for item in agent_records if item.active_deployment_id)
        session_request = SessionCreateRequest.model_validate(
            {"agent_id": str(agent.id), "title": "AP-E3-007 vertical acceptance"}
        )
        session_result = await sessions.create_session(
            context(TENANT_A),
            user_id=ACTOR,
            request=session_request,
            metadata_value={},
            idempotency_key="session-create-ap-e3-007",
            request_hash=canonical_request_hash("session.create", session_request),
            metadata=METADATA,
        )
        assert session_result.value is not None
        session_id = session_result.value.id

        settings = AppSettings.model_validate(
            {
                "env": "test",
                "mock_identity_issuer": "https://issuer.test",
                "mock_external_subject": "resource-owner",
                "mock_active_tenant_id": TENANT_A,
                "mock_membership_version": 1,
            }
        )
        application = create_app(
            settings,
            identity_provider=MockIdentityProvider(settings),
            run_service=RunManagementService(Epic3AccessResolver(), runs),
        )
        compiler = Epic3RunSpecCompiler()
        publisher = Epic3CandidatePublisher()
        sandbox_controller = Epic3SandboxController()
        run_activities = AgentRunWorkflowActivities(
            runs,
            compiler,
            cast(FencingTokenIssuer, HmacFencingTokenIssuer(SecretStr("a" * 32))),
            cast(RunRuntimeExecutor, Epic3RuntimeExecutor()),
            cast(RuntimeEventCandidatePublisher, publisher),
            sandbox_controller=cast(RunSandboxController, sandbox_controller),
        )
        download_dir = Path("/private/tmp/agent-platform-temporal-test")
        download_dir.mkdir(parents=True, exist_ok=True)
        history_json = ""
        async with (
            await WorkflowEnvironment.start_time_skipping(
                data_converter=pydantic_data_converter,
                download_dest_dir=str(download_dir),
            ) as environment,
            Worker(
                environment.client,
                task_queue=RUN_ORCHESTRATOR_TASK_QUEUE,
                workflows=[AgentRunWorkflow],
                activities=[
                    run_activities.prepare_agent_run,
                    run_activities.provision_run_sandbox,
                    run_activities.execute_agent_run,
                    run_activities.inspect_agent_runtime,
                    run_activities.cancel_agent_runtime,
                    run_activities.recover_agent_run,
                    run_activities.finalize_agent_run,
                    run_activities.finalize_agent_run_cancellation,
                    run_activities.release_run_sandbox,
                ],
            ),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client,
        ):
            request = RunCreateRequest.model_validate(
                {
                    "session_id": str(session_id),
                    "input": {"text": "vertical prompt"},
                }
            )
            response = await client.post(
                "/api/v1/runs",
                headers={
                    "Authorization": "Bearer mock",
                    "Idempotency-Key": "run-create-ap-e3-007",
                    "X-Request-ID": "req-ap-e3-007",
                },
                json=request.model_dump(mode="json"),
            )
            assert response.status_code == 202
            accepted = response.json()
            run_id = UUID(accepted["run_id"])

            dispatcher = build_temporal_outbox_dispatcher(
                settings,
                session_factory=session_factory,
                temporal_client=environment.client,
                metrics=PlatformMetrics(),
            )
            summary = await dispatcher.dispatch_tenant_once(
                context(TENANT_A), now=datetime.now(UTC) + timedelta(seconds=5)
            )
            assert summary.claimed == summary.published == 1

            terminal = None
            for _ in range(200):
                polled = await client.get(
                    f"/api/v1/runs/{run_id}",
                    headers={
                        "Authorization": "Bearer mock",
                        "X-Request-ID": "req-ap-e3-007-poll",
                    },
                )
                assert polled.status_code == 200
                terminal = polled.json()
                if terminal["status"] in {
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELLED",
                    "TIMEOUT",
                }:
                    break
                await asyncio.sleep(0.02)
            assert terminal is not None
            assert terminal["status"] == "SUCCEEDED"
            assert terminal["latest_sequence_no"] == 0
            handle = environment.client.get_workflow_handle(
                agent_run_workflow_id(UUID(TENANT_A), run_id)
            )
            history_json = (await handle.fetch_history()).to_json()

        assert publisher.publish_calls == 5
        assert len(sandbox_controller.provisioned) == 1
        assert len(sandbox_controller.released) == 1
        assert [
            candidate.event_type for _, candidate in publisher.candidates.values()
        ] == ["run_started", "text_message_start", "text_delta", "text_message_end"]
        assert "candidate-only-fragment" not in history_json
        assert "fake:text-delta:1" not in history_json

        async with admin_engine.connect() as connection:
            run_row = (
                await connection.execute(
                    text(
                        "SELECT status, current_attempt, latest_sequence_no, "
                        "assistant_message_id, workflow_id, temporal_run_id, "
                        "workflow_start_outcome FROM agent_run WHERE id = :run_id"
                    ),
                    {"run_id": run_id},
                )
            ).one()
            outbox_status = await connection.scalar(
                text(
                    "SELECT status FROM outbox_event WHERE aggregate_id = :run_id "
                    "AND event_type = 'agent.run_requested.v1'"
                ),
                {"run_id": run_id},
            )
            attempt_status = await connection.scalar(
                text(
                    "SELECT status FROM run_attempt WHERE run_id = :run_id "
                    "AND attempt_no = 1"
                ),
                {"run_id": run_id},
            )
            messages = (
                await connection.execute(
                    text(
                        "SELECT id, role, parent_message_id, source_run_id, "
                        "content_parts_json FROM chat_message "
                        "WHERE session_id = :session_id ORDER BY created_at, id"
                    ),
                    {"session_id": session_id},
                )
            ).all()
            cursor = await connection.scalar(
                text(
                    "SELECT cursor_message_id FROM chat_session WHERE id = :session_id"
                ),
                {"session_id": session_id},
            )
            run_event_table = await connection.scalar(
                text("SELECT to_regclass('public.run_event')")
            )
            run_event_count = await connection.scalar(
                text("SELECT count(*) FROM run_event WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
        assert run_row.status == "SUCCEEDED"
        assert run_row.current_attempt == 1
        assert run_row.latest_sequence_no == 0
        assert run_row.assistant_message_id is not None
        assert run_row.workflow_id == agent_run_workflow_id(UUID(TENANT_A), run_id)
        assert run_row.temporal_run_id
        assert run_row.workflow_start_outcome == "STARTED"
        assert outbox_status == "PUBLISHED"
        assert attempt_status == "COMPLETED"
        assert [row.role for row in messages] == ["USER", "ASSISTANT"]
        assert messages[0].parent_message_id is None
        assert messages[0].source_run_id is None
        assert messages[0].content_parts_json == [
            {"type": "text", "text": "vertical prompt"}
        ]
        assert messages[1].parent_message_id == messages[0].id
        assert messages[1].source_run_id == run_id
        assert messages[1].content_parts_json == [
            {"type": "text", "text": "Epic 3 vertical acceptance complete."}
        ]
        assert cursor == messages[1].id
        assert run_event_table == "run_event"
        assert run_event_count == 0

        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE chat_message SET role = 'SYSTEM' WHERE id = :id"),
                    {"id": messages[0].id},
                )
        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM chat_message WHERE id = :id"),
                    {"id": messages[1].id},
                )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_run_creation_atomicity_and_guards(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        runs = SqlAlchemyRunStore(session_factory)
        sessions = SqlAlchemySessionStore(session_factory)
        async with admin_engine.connect() as connection:
            session_row = (
                await connection.execute(
                    text(
                        "SELECT id, agent_id, default_deployment_id "
                        "FROM chat_session WHERE tenant_id = :tenant_id "
                        "AND title = 'Pinned before rollback'"
                    ),
                    {"tenant_id": TENANT_A},
                )
            ).one()

        attachment_request = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "input": {
                    "text": "attachment",
                    "attachments": [{"artifact_id": str(uuid5(NAMESPACE_URL, "a"))}],
                },
            }
        )
        with pytest.raises(PlatformError) as attachment_error:
            await runs.create_run(
                context(TENANT_A),
                user_id=ACTOR,
                request=attachment_request,
                idempotency_key="run-attachment-rejected",
                request_hash=canonical_request_hash("run.create", attachment_request),
                metadata=METADATA,
            )
        assert attachment_error.value.code == "RESOURCE_STATE_CONFLICT"

        invalid_deployment = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "input": {"text": "invalid deployment"},
                "execution": {"deployment_id": str(uuid5(NAMESPACE_URL, "missing"))},
            }
        )
        with pytest.raises(PlatformError) as deployment_error:
            await runs.create_run(
                context(TENANT_A),
                user_id=ACTOR,
                request=invalid_deployment,
                idempotency_key="run-invalid-deployment",
                request_hash=canonical_request_hash("run.create", invalid_deployment),
                metadata=METADATA,
            )
        assert deployment_error.value.code == "RESOURCE_STATE_CONFLICT"

        request = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "client_request_id": "client-request-001",
                "input": {"text": "secret user prompt"},
                "execution": {
                    "timeout_seconds": 120,
                    "token_budget": 2048,
                    "cost_budget": {"amount": "1.25000000", "currency": "USD"},
                },
            }
        )
        request_hash = canonical_request_hash("run.create", request)
        created = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=request,
            idempotency_key="run-create-main-001",
            request_hash=request_hash,
            metadata=METADATA,
        )
        assert created.value is not None
        run = created.value
        assert run.assistant_message_id is None
        assert run.deployment_id == session_row.default_deployment_id
        replay = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=request,
            idempotency_key="run-create-main-001",
            request_hash=request_hash,
            metadata=METADATA,
        )
        assert replay.replay is not None
        assert replay.replay.response_body["run_id"] == str(run.id)
        with pytest.raises(PlatformError) as duplicate:
            await runs.create_run(
                context(TENANT_A),
                user_id=ACTOR,
                request=request,
                idempotency_key="run-create-main-002",
                request_hash=request_hash,
                metadata=METADATA,
            )
        assert duplicate.value.code == "RUN_ALREADY_ACTIVE"
        assert (
            await runs.get_run(context(TENANT_A), user_id=OTHER_ACTOR, run_id=run.id)
            is None
        )
        assert (
            await runs.get_run(context(TENANT_B), user_id=ACTOR, run_id=run.id) is None
        )
        listed = await runs.list_session_runs(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=session_row.id,
            limit=20,
            cursor=None,
        )
        assert listed is not None and [item.id for item in listed[0]] == [run.id]

        async with admin_engine.connect() as connection:
            persisted = (
                await connection.execute(
                    text(
                        "SELECT r.assistant_message_id, r.snapshot_id, "
                        "m.role, m.source_run_id, s.cursor_message_id, "
                        "o.event_type, o.payload_json, a.metadata_json "
                        "FROM agent_run AS r "
                        "JOIN chat_message AS m ON m.id = r.user_message_id "
                        "JOIN chat_session AS s ON s.id = r.session_id "
                        "JOIN outbox_event AS o ON o.aggregate_id = r.id "
                        "JOIN audit_log AS a ON a.resource_id = r.id "
                        "WHERE r.id = :run_id"
                    ),
                    {"run_id": run.id},
                )
            ).one()
        assert persisted.assistant_message_id is None
        assert persisted.snapshot_id == run.snapshot_id
        assert persisted.role == "USER" and persisted.source_run_id is None
        assert persisted.cursor_message_id == run.user_message_id
        assert persisted.event_type == "agent.run_requested.v1"
        assert persisted.payload_json["initial_execution_attempt"] == 1
        assert "secret user prompt" not in str(persisted.metadata_json)

        candidates = await runs.list_stalled_runs(
            context(TENANT_A),
            created_before=datetime.now(UTC),
            cancelling_before=datetime.now(UTC),
            limit=20,
        )
        assert run.id in {candidate.run_id for candidate in candidates}
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE outbox_event SET status = 'DEAD', attempts = 10 "
                    "WHERE aggregate_id = :run_id"
                ),
                {"run_id": run.id},
            )
        assert await runs.requeue_run_request(
            context(TENANT_A), run_id=run.id, now=datetime.now(UTC)
        )
        async with admin_engine.connect() as connection:
            outbox_state = (
                await connection.execute(
                    text(
                        "SELECT status, attempts, published_at FROM outbox_event "
                        "WHERE aggregate_id = :run_id"
                    ),
                    {"run_id": run.id},
                )
            ).one()
        assert outbox_state.status == "PENDING"
        assert outbox_state.attempts == 0
        assert outbox_state.published_at is None

        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE agent_run SET snapshot_id = :snapshot_id "
                        "WHERE id = :run_id"
                    ),
                    {"snapshot_id": uuid5(NAMESPACE_URL, "other"), "run_id": run.id},
                )
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE agent_run SET status = 'FAILED', finished_at = now() "
                    "WHERE id = :run_id"
                ),
                {"run_id": run.id},
            )

        guard_request = SessionCreateRequest.model_validate(
            {"agent_id": str(session_row.agent_id), "title": "Run delete guard"}
        )
        guard_session = await sessions.create_session(
            context(TENANT_A),
            user_id=ACTOR,
            request=guard_request,
            metadata_value={},
            idempotency_key="session-create-run-guard",
            request_hash=canonical_request_hash("session.create", guard_request),
            metadata=METADATA,
        )
        assert guard_session.value is not None
        concurrent_requests = [
            RunCreateRequest.model_validate(
                {
                    "session_id": str(guard_session.value.id),
                    "input": {"text": f"concurrent {index}"},
                }
            )
            for index in (1, 2)
        ]

        async def create_concurrent(index: int):
            concurrent = concurrent_requests[index]
            return await runs.create_run(
                context(TENANT_A),
                user_id=ACTOR,
                request=concurrent,
                idempotency_key=f"run-concurrent-00{index + 1}",
                request_hash=canonical_request_hash("run.create", concurrent),
                metadata=METADATA,
            )

        results = await asyncio.gather(
            create_concurrent(0), create_concurrent(1), return_exceptions=True
        )
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        conflicts = [result for result in results if isinstance(result, PlatformError)]
        assert len(conflicts) == 1 and conflicts[0].code == "RUN_ALREADY_ACTIVE"
        archived = await sessions.archive_session(
            context(TENANT_A),
            user_id=ACTOR,
            session_id=guard_session.value.id,
            expected_version=2,
            idempotency_key="session-archive-run-guard",
            request_hash=canonical_request_hash(
                "session.archive",
                extra={"session_id": str(guard_session.value.id), "if_match": '"rv:2"'},
            ),
            metadata=METADATA,
        )
        assert archived is not None and archived.value is not None
        with pytest.raises(PlatformError) as delete_guard:
            await sessions.delete_session(
                context(TENANT_A),
                user_id=ACTOR,
                session_id=guard_session.value.id,
                expected_version=3,
                idempotency_key="session-delete-run-guard",
                request_hash=canonical_request_hash(
                    "session.delete",
                    extra={
                        "session_id": str(guard_session.value.id),
                        "if_match": '"rv:3"',
                    },
                ),
                metadata=METADATA,
            )
        assert delete_guard.value.code == "RESOURCE_STATE_CONFLICT"
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_run_event_store_constraints(database_url: str) -> None:
    admin_engine = create_async_engine(database_url)
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    event_id = uuid5(NAMESPACE_URL, "run-event-store-event-1")
    second_event_id = uuid5(NAMESPACE_URL, "run-event-store-event-2")
    occurred_at = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
    recorded_at = occurred_at + timedelta(milliseconds=10)
    try:
        async with admin_engine.connect() as connection:
            run_row = (
                await connection.execute(
                    text(
                        "SELECT id, session_id FROM agent_run "
                        "WHERE tenant_id = :tenant_id ORDER BY created_at, id LIMIT 1"
                    ),
                    {"tenant_id": TENANT_A},
                )
            ).one()
            relkind = await connection.scalar(
                text(
                    "SELECT relkind::text FROM pg_class "
                    "WHERE oid = 'run_event'::regclass"
                )
            )
        assert relkind == "r"

        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO run_event_counter (run_id, tenant_id) "
                    "VALUES (:run_id, :tenant_id)"
                ),
                {"run_id": run_row.id, "tenant_id": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO run_event "
                    "(id, tenant_id, run_id, session_id, sequence_no, "
                    "source_event_id, execution_attempt, schema_version, event_type, "
                    "payload_version, payload_json, occurred_at, recorded_at, trace_id) "
                    "VALUES (:id, :tenant_id, :run_id, :session_id, 1, "
                    "'runtime-event-1', 1, '1.0', 'text_delta', '1.0', "
                    '\'{"message_id":"msg-test","delta":"hello"}\'::jsonb, '
                    ":occurred_at, :recorded_at, 'trace-run-event-store')"
                ),
                {
                    "id": event_id,
                    "tenant_id": TENANT_A,
                    "run_id": run_row.id,
                    "session_id": run_row.session_id,
                    "occurred_at": occurred_at,
                    "recorded_at": recorded_at,
                },
            )
            counter = await connection.scalar(
                text(
                    "UPDATE run_event_counter SET next_sequence_no = 2 "
                    "WHERE run_id = :run_id RETURNING next_sequence_no"
                ),
                {"run_id": run_row.id},
            )
        assert counter == 2

        with pytest.raises(DBAPIError):
            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config"
                        "('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_A},
                )
                await connection.execute(
                    text(
                        "INSERT INTO run_event "
                        "(id, tenant_id, run_id, session_id, sequence_no, "
                        "source_event_id, execution_attempt, schema_version, event_type, "
                        "payload_version, payload_json, occurred_at, recorded_at, trace_id) "
                        "VALUES (:id, :tenant_id, :run_id, :session_id, 1, "
                        "'runtime-event-2', 1, '1.0', 'warning', '1.0', '{}'::jsonb, "
                        ":occurred_at, :recorded_at, 'trace-run-event-store')"
                    ),
                    {
                        "id": second_event_id,
                        "tenant_id": TENANT_A,
                        "run_id": run_row.id,
                        "session_id": run_row.session_id,
                        "occurred_at": occurred_at,
                        "recorded_at": recorded_at,
                    },
                )

        with pytest.raises(DBAPIError):
            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config"
                        "('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_A},
                )
                await connection.execute(
                    text(
                        "INSERT INTO run_event "
                        "(id, tenant_id, run_id, session_id, sequence_no, "
                        "source_event_id, execution_attempt, schema_version, event_type, "
                        "payload_version, payload_json, occurred_at, recorded_at, trace_id) "
                        "VALUES (:id, :tenant_id, :run_id, :session_id, 2, "
                        "'runtime-event-1', 1, '1.0', 'warning', '1.0', '{}'::jsonb, "
                        ":occurred_at, :recorded_at, 'trace-run-event-store')"
                    ),
                    {
                        "id": second_event_id,
                        "tenant_id": TENANT_A,
                        "run_id": run_row.id,
                        "session_id": run_row.session_id,
                        "occurred_at": occurred_at,
                        "recorded_at": recorded_at,
                    },
                )

        with pytest.raises(DBAPIError):
            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config"
                        "('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_A},
                )
                await connection.execute(
                    text(
                        "INSERT INTO run_event "
                        "(id, tenant_id, run_id, session_id, sequence_no, "
                        "source_event_id, execution_attempt, schema_version, event_type, "
                        "payload_version, payload_json, occurred_at, recorded_at, trace_id) "
                        "VALUES (:id, :tenant_id, :run_id, :session_id, 2, "
                        "'runtime-event-large', 1, '1.0', 'text_delta', '1.0', "
                        "CAST(:payload AS jsonb), :occurred_at, :recorded_at, "
                        "'trace-run-event-store')"
                    ),
                    {
                        "id": second_event_id,
                        "tenant_id": TENANT_A,
                        "run_id": run_row.id,
                        "session_id": run_row.session_id,
                        "payload": json.dumps({"delta": "x" * 262144}),
                        "occurred_at": occurred_at,
                        "recorded_at": recorded_at,
                    },
                )

        for statement in (
            "UPDATE run_event SET payload_json = '{}'::jsonb WHERE id = :event_id",
            "DELETE FROM run_event WHERE id = :event_id",
        ):
            with pytest.raises(DBAPIError):
                async with app_engine.begin() as connection:
                    await connection.execute(
                        text(
                            "SELECT set_config"
                            "('app.current_tenant_id', :tenant_id, true)"
                        ),
                        {"tenant_id": TENANT_A},
                    )
                    await connection.execute(text(statement), {"event_id": event_id})

        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": TENANT_B},
            )
            assert await connection.scalar(text("SELECT count(*) FROM run_event")) == 0
            assert (
                await connection.scalar(text("SELECT count(*) FROM run_event_counter"))
                == 0
            )

        with pytest.raises(DBAPIError):
            async with app_engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config"
                        "('app.current_tenant_id', :tenant_id, true)"
                    ),
                    {"tenant_id": TENANT_B},
                )
                await connection.execute(
                    text(
                        "UPDATE run_event_counter SET next_sequence_no = 3 "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": run_row.id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO run_event_counter (run_id, tenant_id) "
                        "VALUES (:run_id, :tenant_id)"
                    ),
                    {
                        "run_id": uuid5(NAMESPACE_URL, "cross-tenant-run"),
                        "tenant_id": TENANT_A,
                    },
                )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_run_workflow_attempt_and_message_finalization(
    database_url: str,
) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        runs = SqlAlchemyRunStore(session_factory)
        events = RunEventIngestionService(SqlAlchemyRunEventStore(session_factory))
        event_queries = RunEventQueryService(
            Epic3AccessResolver(), SqlAlchemyRunEventQueryStore(session_factory)
        )
        async with admin_engine.connect() as connection:
            session_row = (
                await connection.execute(
                    text(
                        "SELECT id, default_deployment_id FROM chat_session "
                        "WHERE tenant_id = :tenant_id "
                        "AND title = 'Pinned before rollback'"
                    ),
                    {"tenant_id": TENANT_A},
                )
            ).one()

        success_request = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "input": {"text": "workflow success input"},
                "execution": {"timeout_seconds": 90, "token_budget": 1024},
            }
        )
        success = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=success_request,
            idempotency_key="run-workflow-success-001",
            request_hash=canonical_request_hash("run.create", success_request),
            metadata=METADATA,
        )
        assert success.value is not None
        run = success.value
        source = await runs.load_run_spec_source(context(TENANT_A), run_id=run.id)
        assert source is not None
        assert source.user_text == "workflow success input"
        assert source.snapshot_id == run.snapshot_id
        assert source.deployment_id == run.deployment_id
        assert source.bundle_uri and source.bundle_hash.startswith("sha256:")

        workflow_id = f"run/{TENANT_A}/{run.id}"
        workflow_started_at = datetime.now(UTC)
        assert await runs.record_workflow_start(
            context(TENANT_A),
            run_id=run.id,
            workflow_id=workflow_id,
            temporal_run_id="temporal-run-integration-1",
            outcome="STARTED",
            started_at=workflow_started_at,
        )
        assert not await runs.record_workflow_start(
            context(TENANT_A),
            run_id=run.id,
            workflow_id=workflow_id,
            temporal_run_id="temporal-run-integration-1",
            outcome="ALREADY_EXISTS",
            started_at=workflow_started_at,
        )
        fencing_token = "integration-fencing-token-success-0001"
        fencing_hash = "sha256:" + hashlib.sha256(fencing_token.encode()).hexdigest()
        await runs.prepare_run(
            context(TENANT_A),
            run_id=run.id,
            workflow_id=workflow_id,
            execution_attempt=1,
            fencing_token_hash=fencing_hash,
        )
        await runs.prepare_run(
            context(TENANT_A),
            run_id=run.id,
            workflow_id=workflow_id,
            execution_attempt=1,
            fencing_token_hash=fencing_hash,
        )
        await runs.mark_run_running(
            context(TENANT_A),
            run_id=run.id,
            execution_attempt=1,
            worker_id="integration-run-worker",
        )
        await runs.mark_run_running(
            context(TENANT_A),
            run_id=run.id,
            execution_attempt=1,
            worker_id="integration-run-worker",
        )

        occurred_at = datetime.now(UTC)

        def delta(index: int) -> RuntimeEventCandidate:
            return RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                {
                    "source_event_id": f"integration-delta-{index:03d}",
                    "event_type": "text_delta",
                    "occurred_at": occurred_at,
                    "payload_version": "1.0",
                    "payload": {
                        "message_id": f"runtime-message-{run.id}",
                        "delta": f"chunk-{index}",
                    },
                }
            )

        concurrent_batches = await asyncio.gather(
            *(
                events.append_batch(
                    event_access(TENANT_A),
                    run_id=str(run.id),
                    request=RunEventBatchRequest(
                        execution_attempt=1,
                        execution_fencing_token=fencing_token,
                        events=[delta(index) for index in range(start, start + 10)]
                        + ([delta(0)] if start == 0 else []),
                    ),
                )
                for start in range(0, 100, 10)
            )
        )
        created_sequences = sorted(
            item.sequence_no
            for batch in concurrent_batches
            for item in batch.items
            if item.status == "created" and item.sequence_no is not None
        )
        assert created_sequences == list(range(1, 101))
        created_by_source = {
            item.source_event_id: item.sequence_no
            for batch in concurrent_batches
            for item in batch.items
        }
        assert (
            sum(
                item.status == "duplicate"
                for batch in concurrent_batches
                for item in batch.items
            )
            == 1
        )

        duplicate = await events.append_batch(
            event_access(TENANT_A),
            run_id=str(run.id),
            request=RunEventBatchRequest(
                execution_attempt=1,
                execution_fencing_token=fencing_token,
                events=[delta(0), delta(0)],
            ),
        )
        assert [item.status for item in duplicate.items] == [
            "duplicate",
            "duplicate",
        ]
        assert duplicate.items[0].event_id == duplicate.items[1].event_id
        assert (
            duplicate.items[0].sequence_no == created_by_source["integration-delta-000"]
        )
        reused_source = await events.append_batch(
            event_access(TENANT_A),
            run_id=str(run.id),
            request=RunEventBatchRequest(
                execution_attempt=1,
                execution_fencing_token=fencing_token,
                events=[
                    RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                        {
                            "source_event_id": "integration-delta-000",
                            "event_type": "text_delta",
                            "occurred_at": occurred_at,
                            "payload_version": "1.0",
                            "payload": {
                                "message_id": f"runtime-message-{run.id}",
                                "delta": "different-content",
                            },
                        }
                    )
                ],
            ),
        )
        assert reused_source.items[0].status == "rejected"
        assert reused_source.items[0].error is not None
        assert reused_source.items[0].error.code == "IDEMPOTENCY_KEY_REUSED"

        with pytest.raises(PlatformError) as stale_fencing:
            await events.append_batch(
                event_access(TENANT_A),
                run_id=str(run.id),
                request=RunEventBatchRequest(
                    execution_attempt=1,
                    execution_fencing_token="stale-fencing-token-0001",
                    events=[delta(101)],
                ),
            )
        assert stale_fencing.value.code == "EXECUTION_FENCING_REJECTED"

        premature_terminal = await events.append_batch(
            event_access(TENANT_A),
            run_id=str(run.id),
            request=RunEventBatchRequest(
                execution_attempt=1,
                execution_fencing_token=fencing_token,
                events=[
                    RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                        {
                            "source_event_id": "integration-terminal-premature",
                            "event_type": "run_succeeded",
                            "occurred_at": occurred_at,
                            "payload_version": "1.0",
                            "payload": {
                                "result_message_id": str(run.user_message_id),
                                "usage": {
                                    "input_tokens": 1,
                                    "output_tokens": 1,
                                    "reasoning_tokens": 0,
                                    "estimated": True,
                                },
                                "warnings": [],
                                "result_quality": "NORMAL",
                            },
                        }
                    ),
                    RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                        {
                            "source_event_id": "integration-partial-warning",
                            "event_type": "warning",
                            "occurred_at": occurred_at,
                            "payload_version": "1.0",
                            "payload": {
                                "code": "PARTIAL_BATCH",
                                "message": "ordinary events still commit",
                            },
                        }
                    ),
                ],
            ),
        )
        assert premature_terminal.items[0].status == "rejected"
        assert premature_terminal.items[0].error is not None
        assert premature_terminal.items[0].error.code == "RUN_TERMINAL_NOT_FINALIZED"
        assert premature_terminal.items[1].status == "created"
        assert premature_terminal.items[1].sequence_no == 101

        success_finalization = FinalizeAgentRunInput(
            tenant_id=UUID(TENANT_A),
            run_id=run.id,
            execution_attempt=1,
            completion=RuntimeCompletion(
                status="SUCCEEDED",
                assistant_content_parts=(
                    AssistantTextPart(text="workflow assistant result"),
                ),
                result_quality="NORMAL",
                runtime_handle_ref="runtime://integration/success/1",
            ),
            request_id=METADATA.request_id,
            trace_id=METADATA.trace_id,
        )
        finalized = await runs.finalize_run(
            context(TENANT_A), input=success_finalization
        )
        replay = await runs.finalize_run(context(TENANT_A), input=success_finalization)
        assert finalized.status == "SUCCEEDED"
        assert finalized.assistant_message_id is not None
        assert replay == finalized

        def succeeded_terminal(source_event_id: str) -> RuntimeEventCandidate:
            return RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                {
                    "source_event_id": source_event_id,
                    "event_type": "run_succeeded",
                    "occurred_at": datetime.now(UTC),
                    "payload_version": "1.0",
                    "payload": {
                        "result_message_id": str(finalized.assistant_message_id),
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "reasoning_tokens": 0,
                            "estimated": True,
                        },
                        "warnings": [],
                        "result_quality": "NORMAL",
                    },
                }
            )

        terminal = await events.append_batch(
            event_access(TENANT_A),
            run_id=str(run.id),
            request=RunEventBatchRequest(
                execution_attempt=1,
                execution_fencing_token=fencing_token,
                events=[succeeded_terminal("integration-terminal-success")],
            ),
        )
        assert terminal.items[0].status == "created"
        assert terminal.items[0].sequence_no == 102
        terminal_replay = await events.append_batch(
            event_access(TENANT_A),
            run_id=str(run.id),
            request=RunEventBatchRequest(
                execution_attempt=1,
                execution_fencing_token=fencing_token,
                events=[succeeded_terminal("integration-terminal-replay")],
            ),
        )
        assert terminal_replay.items[0].status == "duplicate"
        assert terminal_replay.items[0].event_id == terminal.items[0].event_id
        conflict = await events.append_batch(
            event_access(TENANT_A),
            run_id=str(run.id),
            request=RunEventBatchRequest(
                execution_attempt=1,
                execution_fencing_token=fencing_token,
                events=[
                    RUNTIME_EVENT_CANDIDATE_ADAPTER.validate_python(
                        {
                            "source_event_id": "integration-terminal-conflict",
                            "event_type": "run_failed",
                            "occurred_at": datetime.now(UTC),
                            "payload_version": "1.0",
                            "payload": {
                                "error_code": "CONFLICT",
                                "message": "must be rejected",
                                "retryable": False,
                            },
                        }
                    )
                ],
            ),
        )
        assert conflict.items[0].status == "rejected"
        assert conflict.items[0].error is not None
        assert conflict.items[0].error.code == "RUN_TERMINAL_EVENT_CONFLICT"

        query_principal = AuthenticatedPrincipal(
            identity_issuer="https://issuer.test/",
            external_subject="resource-owner",
            display_name="Resource Owner",
            platform_roles=frozenset(),
            auth_time=occurred_at,
        )
        replayed_sequence_numbers: list[int] = []
        replayed_ag_ui_batches: list[AgUiEventBatch] = []
        ag_ui_adapter = RunEventAgUiAdapter()
        after = 0
        while True:
            page = await event_queries.list_events(
                query_principal,
                run_id=str(run.id),
                after=after,
                limit=37,
                metadata=METADATA,
            )
            replayed_sequence_numbers.extend(item.sequence_no for item in page.items)
            replayed_ag_ui_batches.extend(
                ag_ui_adapter.map_event(item) for item in page.items
            )
            assert page.latest_sequence_no == 102
            if not page.has_more:
                break
            after = page.items[-1].sequence_no
        assert replayed_sequence_numbers == list(range(1, 103))
        assert [batch.source_sequence_no for batch in replayed_ag_ui_batches] == list(
            range(1, 103)
        )
        serialized_ag_ui_events = [
            serialize_ag_ui_event(mapped_event)
            for batch in replayed_ag_ui_batches
            for mapped_event in batch.events
        ]
        terminal_ag_ui_events = [
            mapped_event
            for mapped_event in serialized_ag_ui_events
            if mapped_event["type"] in {"RUN_FINISHED", "RUN_ERROR"}
        ]
        assert len(terminal_ag_ui_events) == 1
        assert terminal_ag_ui_events[0]["type"] == "RUN_FINISHED"
        assert serialize_ag_ui_event(replayed_ag_ui_batches[-1].events[-1])["type"] == (
            "RUN_FINISHED"
        )
        queried_run = await runs.get_run(
            context(TENANT_A), user_id=ACTOR, run_id=run.id
        )
        assert queried_run is not None
        assert queried_run.latest_sequence_no == page.latest_sequence_no == 102
        exhausted = await event_queries.list_events(
            query_principal,
            run_id=str(run.id),
            after=102,
            limit=37,
            metadata=METADATA,
        )
        assert exhausted.items == []
        assert exhausted.latest_sequence_no == 102
        assert exhausted.has_more is False
        terminal_page = await SqlAlchemyRunEventQueryStore(session_factory).list_events(
            context(TENANT_A),
            user_id=ACTOR,
            run_id=run.id,
            after=102,
            limit=37,
        )
        assert terminal_page is not None
        assert terminal_page.is_terminal is True
        event_stream = await RunEventStreamService(
            event_queries, page_size=37
        ).open_stream(
            query_principal,
            run_id=str(run.id),
            after=0,
            metadata=METADATA,
        )
        stream_payload = b"".join([frame async for frame in event_stream])
        assert stream_payload.count(b"event: run_event") == 102
        assert b"id: 102\n" in stream_payload
        assert (
            await SqlAlchemyRunEventQueryStore(session_factory).list_events(
                context(TENANT_A),
                user_id=OTHER_ACTOR,
                run_id=run.id,
                after=0,
                limit=20,
            )
            is None
        )

        async with admin_engine.connect() as connection:
            persisted = (
                await connection.execute(
                    text(
                        "SELECT r.status, r.workflow_id, r.temporal_run_id, "
                        "r.workflow_start_outcome, r.workflow_started_at, "
                        "r.current_attempt, r.latest_sequence_no, "
                        "r.assistant_message_id, a.status AS attempt_status, "
                        "a.fencing_token_hash, a.runtime_handle_ref, "
                        "m.role, m.source_run_id, m.parent_message_id, "
                        "m.content_parts_json, s.cursor_message_id, "
                        "(SELECT count(*) FROM run_attempt ra WHERE ra.run_id = r.id) "
                        "AS attempt_count, "
                        "(SELECT count(*) FROM chat_message cm "
                        "WHERE cm.source_run_id = r.id) AS assistant_count "
                        "FROM agent_run r JOIN run_attempt a ON a.run_id = r.id "
                        "JOIN chat_message m ON m.id = r.assistant_message_id "
                        "JOIN chat_session s ON s.id = r.session_id "
                        "WHERE r.id = :run_id"
                    ),
                    {"run_id": run.id},
                )
            ).one()
        assert persisted.status == "SUCCEEDED"
        assert persisted.workflow_id == workflow_id
        assert persisted.temporal_run_id == "temporal-run-integration-1"
        assert persisted.workflow_start_outcome == "STARTED"
        assert persisted.workflow_started_at == workflow_started_at
        assert persisted.current_attempt == 1
        assert persisted.latest_sequence_no == 102
        assert persisted.attempt_status == "COMPLETED"
        assert persisted.fencing_token_hash == fencing_hash
        assert persisted.runtime_handle_ref == "runtime://integration/success/1"
        assert persisted.role == "ASSISTANT"
        assert persisted.source_run_id == run.id
        assert persisted.parent_message_id == run.user_message_id
        assert persisted.content_parts_json == [
            {"type": "text", "text": "workflow assistant result"}
        ]
        assert persisted.cursor_message_id == persisted.assistant_message_id
        assert persisted.attempt_count == 1
        assert persisted.assistant_count == 1
        async with admin_engine.connect() as connection:
            event_facts = (
                await connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM run_event e "
                        "WHERE e.run_id = :run_id) AS event_count, "
                        "(SELECT next_sequence_no FROM run_event_counter c "
                        "WHERE c.run_id = :run_id) AS next_sequence_no, "
                        "(SELECT count(*) FROM outbox_event o "
                        "WHERE o.aggregate_id = :run_id "
                        "AND o.event_type = 'run.events_appended.v1') AS outbox_count, "
                        "(SELECT count(*) FROM audit_log a "
                        "WHERE a.resource_id = :run_id "
                        "AND a.action = 'execution_fencing_rejected') "
                        "AS fencing_audit_count, "
                        "(SELECT count(*) FROM audit_log a "
                        "WHERE a.resource_id = :run_id "
                        "AND a.action = 'terminal_conflict') "
                        "AS terminal_audit_count"
                    ),
                    {"run_id": run.id},
                )
            ).one()
        assert event_facts.event_count == 102
        assert event_facts.next_sequence_no == 103
        assert event_facts.outbox_count == 12
        assert event_facts.fencing_audit_count == 1
        assert event_facts.terminal_audit_count == 2

        class NotificationRecorder:
            def __init__(self) -> None:
                self.notifications: list[RunEventNotification] = []

            async def publish(self, notification: RunEventNotification) -> None:
                self.notifications.append(notification)

        notification_recorder = NotificationRecorder()
        notification_dispatcher = RunEventNotificationDispatcher(
            SqlAlchemyOutboxStore(
                session_factory,
                event_types=frozenset({RUN_EVENTS_APPENDED_EVENT}),
            ),
            cast(RunEventNotificationPublisher, notification_recorder),
        )
        dispatch_now = datetime.now(UTC) + timedelta(seconds=1)
        while True:
            summary = await notification_dispatcher.dispatch_tenant_once(
                event_access(TENANT_A).context,
                now=dispatch_now,
            )
            if summary.claimed == 0:
                break
        run_notifications = sorted(
            (
                notification
                for notification in notification_recorder.notifications
                if notification.run_id == run.id
            ),
            key=lambda notification: notification.first_sequence_no,
        )
        assert len(run_notifications) == 12
        notified_sequences = [
            sequence_no
            for notification in run_notifications
            for sequence_no in range(
                notification.first_sequence_no,
                notification.last_sequence_no + 1,
            )
        ]
        assert notified_sequences == list(range(1, 103))

        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE chat_message SET content_parts_json = "
                        '\'[{"type":"text","text":"changed"}]\'::jsonb '
                        "WHERE id = :message_id"
                    ),
                    {"message_id": finalized.assistant_message_id},
                )
        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM chat_message WHERE id = :message_id"),
                    {"message_id": finalized.assistant_message_id},
                )

        failure_request = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "input": {"text": "workflow failure input"},
            }
        )
        failure = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=failure_request,
            idempotency_key="run-workflow-failure-001",
            request_hash=canonical_request_hash("run.create", failure_request),
            metadata=METADATA,
        )
        assert failure.value is not None
        failed_run = failure.value
        await runs.prepare_run(
            context(TENANT_A),
            run_id=failed_run.id,
            workflow_id=f"run/{TENANT_A}/{failed_run.id}",
            execution_attempt=1,
            fencing_token_hash="sha256:" + "b" * 64,
        )
        await runs.mark_run_running(
            context(TENANT_A),
            run_id=failed_run.id,
            execution_attempt=1,
            worker_id="integration-run-worker",
        )
        failed = await runs.finalize_run(
            context(TENANT_A),
            input=FinalizeAgentRunInput(
                tenant_id=UUID(TENANT_A),
                run_id=failed_run.id,
                execution_attempt=1,
                completion=RuntimeCompletion(
                    status="FAILED",
                    error_code="MODEL_TIMEOUT",
                    error_message="The model timed out.",
                    retryable=False,
                ),
                request_id=METADATA.request_id,
                trace_id=METADATA.trace_id,
            ),
        )
        assert failed.status == "FAILED"
        assert failed.assistant_message_id is None
        async with admin_engine.connect() as connection:
            failed_persisted = (
                await connection.execute(
                    text(
                        "SELECT r.status, r.assistant_message_id, r.error_code, "
                        "r.error_detail_json, a.status AS attempt_status, "
                        "(SELECT count(*) FROM chat_message m "
                        "WHERE m.source_run_id = r.id) AS assistant_count "
                        "FROM agent_run r JOIN run_attempt a ON a.run_id = r.id "
                        "WHERE r.id = :run_id"
                    ),
                    {"run_id": failed_run.id},
                )
            ).one()
        assert failed_persisted.status == "FAILED"
        assert failed_persisted.assistant_message_id is None
        assert failed_persisted.error_code == "MODEL_TIMEOUT"
        assert failed_persisted.error_detail_json["message"] == "The model timed out."
        assert failed_persisted.attempt_status == "LOST"
        assert failed_persisted.assistant_count == 0

        preparation_failure_request = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "input": {"text": "workflow preparation failure input"},
            }
        )
        preparation_failure = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=preparation_failure_request,
            idempotency_key="run-workflow-prepare-failure-001",
            request_hash=canonical_request_hash(
                "run.create", preparation_failure_request
            ),
            metadata=METADATA,
        )
        assert preparation_failure.value is not None
        preparation_failed_run = preparation_failure.value
        preparation_workflow_id = f"run/{TENANT_A}/{preparation_failed_run.id}"
        await runs.prepare_run(
            context(TENANT_A),
            run_id=preparation_failed_run.id,
            workflow_id=preparation_workflow_id,
            execution_attempt=1,
            fencing_token_hash="sha256:" + "c" * 64,
        )
        preparation_failed = await runs.finalize_run(
            context(TENANT_A),
            input=FinalizeAgentRunInput(
                tenant_id=UUID(TENANT_A),
                run_id=preparation_failed_run.id,
                execution_attempt=1,
                completion=RuntimeCompletion(
                    status="FAILED",
                    error_code="RUN_SPEC_UNAVAILABLE",
                    error_message="The immutable RunSpec could not be prepared.",
                    retryable=False,
                ),
                request_id=METADATA.request_id,
                trace_id=METADATA.trace_id,
            ),
        )
        assert preparation_failed.status == "FAILED"
        async with admin_engine.connect() as connection:
            preparation_counts = (
                await connection.execute(
                    text(
                        "SELECT r.workflow_id, r.assistant_message_id, "
                        "(SELECT count(*) FROM run_attempt a WHERE a.run_id = r.id) "
                        "AS attempt_count, "
                        "(SELECT max(a.status) FROM run_attempt a "
                        "WHERE a.run_id = r.id) AS attempt_status, "
                        "(SELECT count(*) FROM chat_message m "
                        "WHERE m.source_run_id = r.id) AS assistant_count "
                        "FROM agent_run r WHERE r.id = :run_id"
                    ),
                    {"run_id": preparation_failed_run.id},
                )
            ).one()
        assert preparation_counts.workflow_id == preparation_workflow_id
        assert preparation_counts.assistant_message_id is None
        assert preparation_counts.attempt_count == 1
        assert preparation_counts.attempt_status == "LOST"
        assert preparation_counts.assistant_count == 0
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_run_cancel_retry_and_recovery_fencing(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        runs = SqlAlchemyRunStore(create_session_factory(app_engine))
        async with admin_engine.connect() as connection:
            session_row = (
                await connection.execute(
                    text(
                        "SELECT id FROM chat_session WHERE tenant_id = :tenant_id "
                        "AND title = 'Pinned before rollback'"
                    ),
                    {"tenant_id": TENANT_A},
                )
            ).one()

        cancel_create = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "input": {"text": "cancel and retry immutable input"},
            }
        )
        created = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=cancel_create,
            idempotency_key="run-cancel-source-001",
            request_hash=canonical_request_hash("run.create", cancel_create),
            metadata=METADATA,
        )
        assert created.value is not None
        source = created.value
        cancel_request = CancelRunRequest(reason="integration stop")
        cancelled_request = await runs.request_cancel(
            context(TENANT_A),
            user_id=ACTOR,
            run_id=source.id,
            request=cancel_request,
            idempotency_key="run-cancel-request-001",
            request_hash=canonical_request_hash(
                "run.cancel", cancel_request, extra={"run_id": str(source.id)}
            ),
            metadata=METADATA,
        )
        assert cancelled_request is not None and cancelled_request.value is not None
        assert cancelled_request.value.status == "CANCELLING"
        cancelled = await runs.finalize_cancellation(
            context(TENANT_A),
            input=FinalizeAgentRunCancellationInput(
                tenant_id=UUID(TENANT_A),
                run_id=source.id,
                execution_attempt=0,
                cancellation=RuntimeCancellationResult(
                    run_id=source.id,
                    execution_attempt=0,
                    status="ALREADY_STOPPED",
                ),
                request_id=METADATA.request_id,
                trace_id=METADATA.trace_id,
            ),
            fencing_token_hash=None,
        )
        assert cancelled.status == "CANCELLED"
        async with admin_engine.connect() as connection:
            cancelling_at = await connection.scalar(
                text("SELECT cancelling_at FROM agent_run WHERE id = :run_id"),
                {"run_id": source.id},
            )
        assert cancelling_at is not None

        retry_request = RetryRunRequest(deployment_policy="original_snapshot")
        retried = await runs.retry_run(
            context(TENANT_A),
            user_id=ACTOR,
            run_id=source.id,
            request=retry_request,
            idempotency_key="run-retry-source-001",
            request_hash=canonical_request_hash(
                "run.retry", retry_request, extra={"run_id": str(source.id)}
            ),
            metadata=METADATA,
        )
        assert retried is not None and retried.value is not None
        retry_run = retried.value
        assert retry_run.retry_of_run_id == source.id
        assert retry_run.user_message_id == source.user_message_id
        assert retry_run.snapshot_id == source.snapshot_id
        assert retry_run.deployment_id == source.deployment_id
        async with admin_engine.connect() as connection:
            immutable_counts = (
                await connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM chat_message m "
                        "WHERE m.id = :message_id) AS source_message_count, "
                        "(SELECT count(*) FROM chat_message m "
                        "WHERE m.source_run_id = :retry_run_id) AS assistant_count"
                    ),
                    {
                        "message_id": source.user_message_id,
                        "retry_run_id": retry_run.id,
                    },
                )
            ).one()
        assert immutable_counts.source_message_count == 1
        assert immutable_counts.assistant_count == 0

        old_hash = "sha256:" + "d" * 64
        new_hash = "sha256:" + "e" * 64
        workflow_id = f"run/{TENANT_A}/{retry_run.id}"
        await runs.prepare_run(
            context(TENANT_A),
            run_id=retry_run.id,
            workflow_id=workflow_id,
            execution_attempt=1,
            fencing_token_hash=old_hash,
        )
        await runs.mark_run_running(
            context(TENANT_A),
            run_id=retry_run.id,
            execution_attempt=1,
            worker_id="integration-recovery-worker-1",
            fencing_token_hash=old_hash,
        )
        await runs.prepare_recovery_attempt(
            context(TENANT_A),
            run_id=retry_run.id,
            lost_execution_attempt=1,
            execution_attempt=2,
            fencing_token_hash=new_hash,
        )
        with pytest.raises(PlatformError) as stale_attempt:
            await runs.finalize_run(
                context(TENANT_A),
                input=FinalizeAgentRunInput(
                    tenant_id=UUID(TENANT_A),
                    run_id=retry_run.id,
                    execution_attempt=1,
                    completion=RuntimeCompletion(
                        status="FAILED",
                        error_code="STALE_ATTEMPT",
                        error_message="A stale attempt cannot finalize the Run.",
                        retryable=False,
                    ),
                    request_id=METADATA.request_id,
                    trace_id=METADATA.trace_id,
                ),
                fencing_token_hash=old_hash,
            )
        assert stale_attempt.value.code == "RESOURCE_STATE_CONFLICT"
        with pytest.raises(PlatformError) as stale_token:
            await runs.mark_run_running(
                context(TENANT_A),
                run_id=retry_run.id,
                execution_attempt=2,
                worker_id="integration-recovery-worker-2",
                fencing_token_hash=old_hash,
            )
        assert stale_token.value.code == "RESOURCE_STATE_CONFLICT"
        await runs.mark_run_running(
            context(TENANT_A),
            run_id=retry_run.id,
            execution_attempt=2,
            worker_id="integration-recovery-worker-2",
            fencing_token_hash=new_hash,
        )
        recovered = await runs.finalize_run(
            context(TENANT_A),
            input=FinalizeAgentRunInput(
                tenant_id=UUID(TENANT_A),
                run_id=retry_run.id,
                execution_attempt=2,
                completion=RuntimeCompletion(
                    status="SUCCEEDED",
                    assistant_content_parts=(
                        AssistantTextPart(text="recovered exactly once"),
                    ),
                    runtime_handle_ref="runtime://integration/recovered/2",
                ),
                request_id=METADATA.request_id,
                trace_id=METADATA.trace_id,
            ),
            fencing_token_hash=new_hash,
        )
        assert recovered.status == "SUCCEEDED"
        async with admin_engine.connect() as connection:
            attempts = list(
                (
                    await connection.execute(
                        text(
                            "SELECT attempt_no, status, fencing_token_hash "
                            "FROM run_attempt WHERE run_id = :run_id "
                            "ORDER BY attempt_no"
                        ),
                        {"run_id": retry_run.id},
                    )
                ).all()
            )
        assert [(row.attempt_no, row.status) for row in attempts] == [
            (1, "LOST"),
            (2, "COMPLETED"),
        ]
        assert attempts[0].fencing_token_hash == old_hash
        assert attempts[1].fencing_token_hash == new_hash

        running_cancel_request = RunCreateRequest.model_validate(
            {
                "session_id": str(session_row.id),
                "input": {"text": "cancel a running fenced attempt"},
            }
        )
        running_cancel = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=running_cancel_request,
            idempotency_key="run-running-cancel-001",
            request_hash=canonical_request_hash("run.create", running_cancel_request),
            metadata=METADATA,
        )
        assert running_cancel.value is not None
        running_run = running_cancel.value
        running_hash = "sha256:" + "f" * 64
        await runs.prepare_run(
            context(TENANT_A),
            run_id=running_run.id,
            workflow_id=f"run/{TENANT_A}/{running_run.id}",
            execution_attempt=1,
            fencing_token_hash=running_hash,
        )
        await runs.mark_run_running(
            context(TENANT_A),
            run_id=running_run.id,
            execution_attempt=1,
            worker_id="integration-cancel-worker",
            fencing_token_hash=running_hash,
        )
        running_cancel_body = CancelRunRequest(reason="stop running attempt")
        requested = await runs.request_cancel(
            context(TENANT_A),
            user_id=ACTOR,
            run_id=running_run.id,
            request=running_cancel_body,
            idempotency_key="run-running-cancel-request-001",
            request_hash=canonical_request_hash(
                "run.cancel",
                running_cancel_body,
                extra={"run_id": str(running_run.id)},
            ),
            metadata=METADATA,
        )
        assert requested is not None and requested.value is not None
        running_cancelled = await runs.finalize_cancellation(
            context(TENANT_A),
            input=FinalizeAgentRunCancellationInput(
                tenant_id=UUID(TENANT_A),
                run_id=running_run.id,
                execution_attempt=1,
                cancellation=RuntimeCancellationResult(
                    run_id=running_run.id,
                    execution_attempt=1,
                    status="CANCELLED",
                    runtime_handle_ref="runtime://integration/cancelled/1",
                ),
                request_id=METADATA.request_id,
                trace_id=METADATA.trace_id,
            ),
            fencing_token_hash=running_hash,
        )
        assert running_cancelled.status == "CANCELLED"
        terminal_cancel = await runs.request_cancel(
            context(TENANT_A),
            user_id=ACTOR,
            run_id=running_run.id,
            request=CancelRunRequest(reason="duplicate terminal cancel"),
            idempotency_key="run-terminal-cancel-001",
            request_hash=canonical_request_hash(
                "run.cancel",
                CancelRunRequest(reason="duplicate terminal cancel"),
                extra={"run_id": str(running_run.id)},
            ),
            metadata=METADATA,
        )
        assert terminal_cancel is not None and terminal_cancel.value is not None
        assert terminal_cancel.value.status == "CANCELLED"
        async with admin_engine.connect() as connection:
            cancelled_facts = (
                await connection.execute(
                    text(
                        "SELECT r.assistant_message_id, a.status AS attempt_status, "
                        "(SELECT count(*) FROM chat_message m "
                        "WHERE m.source_run_id = r.id) AS assistant_count "
                        "FROM agent_run r JOIN run_attempt a ON a.run_id = r.id "
                        "WHERE r.id = :run_id"
                    ),
                    {"run_id": running_run.id},
                )
            ).one()
        assert cancelled_facts.assistant_message_id is None
        assert cancelled_facts.attempt_status == "CANCELLED"
        assert cancelled_facts.assistant_count == 0
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_sandbox_lifecycle_and_fencing(database_url: str) -> None:
    """Exercise Sandbox persistence against the same immutable Run facts."""

    policy = compile_sandbox_policy(
        {
            "schema_version": "1.0",
            "scope": "run",
            "image_digest": "registry.example/runtime@sha256:" + "a" * 64,
            "cpu_limit": 1,
            "memory_mb": 512,
            "disk_mb": 1024,
            "pids_limit": 64,
            "timeout_seconds": 600,
            "network": {
                "mode": "none",
                "allow_domains": [],
                "allow_ports": [],
                "deny_private_networks": True,
            },
            "filesystem": {
                "read_patterns": ["work/**"],
                "write_patterns": ["work/**"],
                "max_files": 100,
                "max_file_bytes": 1024,
            },
            "process": {
                "allowed_executables": ["python"],
                "shell_allowed": False,
                "max_processes": 16,
            },
            "artifacts": {
                "allow_export": False,
                "max_artifacts": 0,
                "max_total_bytes": 0,
                "allowed_content_types": [],
            },
        }
    )

    class ProviderFake:
        def __init__(self) -> None:
            self.destroy_calls = 0

        async def provision(self, spec: ProviderProvisionSpec):
            assert spec.policy.policy_hash == policy.policy_hash
            assert spec.bundle_hash.startswith("sha256:")
            return ProviderSandboxObservation(
                provider_ref=f"provider://sandbox/{spec.sandbox_id}",
                state="READY",
                observed_at=datetime.now(UTC),
            )

        async def inspect(self, provider_ref: str):
            return ProviderSandboxObservation(
                provider_ref=provider_ref,
                state="RUNNING",
                observed_at=datetime.now(UTC),
            )

        async def recover(self, sandbox_id: str) -> None:
            del sandbox_id

        async def start_process(self, provider_ref: str, **kwargs: object):
            del provider_ref, kwargs
            return ProviderProcessObservation(
                process_id="proc_sandbox_integration", state="STARTING"
            )

        async def cancel_process(self, provider_ref: str, **kwargs: object):
            del provider_ref, kwargs
            return ProviderProcessObservation(
                process_id="proc_sandbox_integration", state="CANCELLED"
            )

        async def terminate(self, provider_ref: str, **kwargs: object):
            del kwargs
            return ProviderSandboxObservation(
                provider_ref=provider_ref,
                state="TERMINATED",
                observed_at=datetime.now(UTC),
            )

        async def destroy(self, provider_ref: str):
            self.destroy_calls += 1
            return ProviderSandboxObservation(
                provider_ref=provider_ref,
                state="TERMINATED",
                observed_at=datetime.now(UTC),
            )

    class PolicyResolverFake:
        async def resolve(self, *args: object, **kwargs: object):
            del args, kwargs
            return policy

    class TokenVerifierFake:
        async def verify(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        runs = SqlAlchemyRunStore(session_factory)
        async with admin_engine.connect() as connection:
            agent_id = await connection.scalar(
                text(
                    "SELECT id FROM agent_definition "
                    "WHERE tenant_id = :tenant_id "
                    "AND active_deployment_id IS NOT NULL "
                    "ORDER BY created_at, id LIMIT 1"
                ),
                {"tenant_id": TENANT_A},
            )
        assert agent_id is not None
        session_request = SessionCreateRequest.model_validate(
            {"agent_id": str(agent_id), "title": "Sandbox lifecycle integration"}
        )
        created_session = await SqlAlchemySessionStore(session_factory).create_session(
            context(TENANT_A),
            user_id=ACTOR,
            request=session_request,
            metadata_value={},
            idempotency_key="session-sandbox-lifecycle-integration",
            request_hash=canonical_request_hash("session.create", session_request),
            metadata=METADATA,
        )
        assert created_session.value is not None
        async with admin_engine.begin() as connection:
            source = (
                await connection.execute(
                    text(
                        "SELECT s.id AS session_id, s.default_deployment_id, "
                        "d.bundle_id, d.snapshot_id, b.runtime_type, b.content_hash "
                        "FROM chat_session s JOIN deployment d "
                        "ON d.id = s.default_deployment_id "
                        "JOIN runtime_bundle b ON b.id = d.bundle_id "
                        "WHERE s.tenant_id = :tenant_id AND s.id = :session_id"
                    ),
                    {
                        "tenant_id": TENANT_A,
                        "session_id": created_session.value.id,
                    },
                )
            ).one()
            await connection.execute(
                text(
                    "UPDATE runtime_bundle SET compiler_name = :column_compiler_name, "
                    "compiler_version = :column_compiler_version, manifest_json = "
                    "jsonb_set(jsonb_set(manifest_json, '{compiler}', "
                    "jsonb_build_object('name', CAST(:manifest_compiler_name AS text), "
                    "'version', CAST(:manifest_compiler_version AS text)), true), "
                    "'{security}', jsonb_build_object("
                    "'permission_policy_hash', CAST(:permission_hash AS text), "
                    "'sandbox_policy_hash', CAST(:policy_hash AS text), "
                    "'secret_refs', '[]'::jsonb), true) "
                    "WHERE id = :bundle_id"
                ),
                {
                    "column_compiler_name": BUNDLE_COMPILER_NAME,
                    "column_compiler_version": BUNDLE_COMPILER_VERSION,
                    "manifest_compiler_name": BUNDLE_COMPILER_NAME,
                    "manifest_compiler_version": BUNDLE_COMPILER_VERSION,
                    "permission_hash": "sha256:" + "d" * 64,
                    "policy_hash": policy.policy_hash,
                    "bundle_id": source.bundle_id,
                },
            )

        create_request = RunCreateRequest.model_validate(
            {
                "session_id": str(source.session_id),
                "input": {"text": "sandbox lifecycle integration"},
            }
        )
        created = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=create_request,
            idempotency_key="run-sandbox-lifecycle-integration",
            request_hash=canonical_request_hash("run.create", create_request),
            metadata=METADATA,
        )
        assert created.value is not None
        run = created.value
        fencing_token = "sandbox-execution-fencing-token-001"
        fencing_hash = "sha256:" + hashlib.sha256(fencing_token.encode()).hexdigest()
        await runs.prepare_run(
            context(TENANT_A),
            run_id=run.id,
            workflow_id=f"run/{TENANT_A}/{run.id}",
            execution_attempt=1,
            fencing_token_hash=fencing_hash,
        )

        provider = ProviderFake()
        lifecycle = SandboxLifecycleService(
            SqlAlchemySandboxLifecycleStore(session_factory),
            cast(SandboxProvider, provider),
            cast(SandboxPolicyResolver, PolicyResolverFake()),
            cast(SandboxProvisionTokenVerifier, TokenVerifierFake()),
            now=lambda: datetime.now(UTC),
        )
        service_access = SandboxServiceAccess(
            context=TenantContext(
                tenant_id=TENANT_A,
                subject_type=SubjectType.SERVICE,
                subject_id=str(ACTOR),
                auth_time=datetime.now(UTC),
                request_id="req-sandbox-integration",
                trace_id="trace-sandbox-integration",
            ),
            permissions=frozenset({SANDBOX_MANAGE_PERMISSION}),
        )
        bundle_ref = (
            f"bundle://tenant/{TENANT_A}/snapshot/{source.snapshot_id}/"
            f"runtime/{source.runtime_type}/{source.content_hash}"
        )
        workspace_uri = (
            f"workspace://tenant/{TENANT_A}/user/{ACTOR}/"
            f"session/{source.session_id}/runs/{run.id}/"
        )
        provision_request = SandboxProvisionRequest(
            tenant_id=TENANT_A,
            user_id=str(ACTOR),
            session_id=str(source.session_id),
            run_id=str(run.id),
            execution_attempt=1,
            scope="run",
            policy_ref="immutable://sandbox-policy/integration-policy",
            policy_hash=policy.policy_hash,
            bundle_ref=bundle_ref,
            bundle_hash=source.content_hash,
            workspace_uri=workspace_uri,
            provision_token=SecretStr("sandbox-provision-token-integration"),
            trace_id="trace-sandbox-integration",
        )
        idempotency_key = f"sandbox/{run.id}/1/{policy.policy_hash}"
        with pytest.raises(PlatformError) as wrong_bundle:
            await lifecycle.provision(
                service_access,
                request=provision_request.model_copy(
                    update={
                        "bundle_ref": (
                            f"bundle://tenant/{TENANT_A}/snapshot/{source.snapshot_id}/"
                            f"runtime/{source.runtime_type}/{'sha256:' + 'f' * 64}"
                        )
                    }
                ),
                idempotency_key=idempotency_key,
            )
        assert wrong_bundle.value.code == "SANDBOX_POLICY_DENIED"
        accepted = await lifecycle.provision(
            service_access,
            request=provision_request,
            idempotency_key=idempotency_key,
        )
        replay = await lifecycle.provision(
            service_access,
            request=provision_request,
            idempotency_key=idempotency_key,
        )
        assert replay == accepted
        detail = await lifecycle.get(service_access, sandbox_id=accepted.sandbox_id)
        assert detail.status == "READY"
        workspace_store = SqlAlchemyWorkspaceStore(session_factory)
        workspace = await workspace_store.get(
            service_access.context, workspace_uri=workspace_uri
        )
        assert workspace is not None
        assert workspace.status == "ACTIVE"
        updated_workspace = await workspace_store.record_usage(
            service_access.context,
            workspace_uri=workspace_uri,
            used_bytes=512,
            file_count=1,
            now=datetime.now(UTC),
        )
        assert updated_workspace is not None
        assert updated_workspace.used_bytes == 512
        with pytest.raises(PlatformError) as quota_exceeded:
            await workspace_store.record_usage(
                service_access.context,
                workspace_uri=workspace_uri,
                used_bytes=1024 * 1024 * 1024 + 1,
                file_count=1,
                now=datetime.now(UTC),
            )
        assert quota_exceeded.value.code == "WORKSPACE_QUOTA_EXCEEDED"

        stale_lease = SandboxLeaseRequest(
            run_id=str(run.id),
            execution_attempt=1,
            execution_fencing_token=SecretStr("stale-execution-fencing-token"),
            ttl_seconds=300,
            trace_id="trace-sandbox-integration",
        )
        with pytest.raises(PlatformError) as fenced:
            await lifecycle.acquire_lease(
                service_access,
                sandbox_id=accepted.sandbox_id,
                request=stale_lease,
            )
        assert fenced.value.code == "SANDBOX_FENCING_REJECTED"

        active_lease = await lifecycle.acquire_lease(
            service_access,
            sandbox_id=accepted.sandbox_id,
            request=stale_lease.model_copy(
                update={"execution_fencing_token": SecretStr(fencing_token)}
            ),
        )
        stale_process = SandboxProcessRequest(
            run_id=str(run.id),
            execution_attempt=1,
            execution_fencing_token=SecretStr("stale-execution-fencing-token"),
            process_id="proc_stale",
            argv=["python", "-m", "runtime_entry"],
            working_directory=workspace_uri + "work/",
            timeout_seconds=30,
            trace_id="trace-sandbox-integration",
        )
        with pytest.raises(PlatformError) as stale_control:
            await lifecycle.start_process(
                service_access,
                sandbox_id=accepted.sandbox_id,
                request=stale_process,
            )
        assert stale_control.value.code == "SANDBOX_FENCING_REJECTED"
        with pytest.raises(PlatformError) as stale_release:
            await lifecycle.release(
                service_access,
                sandbox_id=accepted.sandbox_id,
                request=SandboxReleaseRequest(
                    run_id=str(run.id),
                    execution_attempt=1,
                    execution_fencing_token=SecretStr("stale-execution-fencing-token"),
                    trace_id="trace-sandbox-integration",
                ),
            )
        assert stale_release.value.code == "SANDBOX_FENCING_REJECTED"
        process = await lifecycle.start_process(
            service_access,
            sandbox_id=accepted.sandbox_id,
            request=SandboxProcessRequest(
                run_id=str(run.id),
                execution_attempt=1,
                execution_fencing_token=SecretStr(fencing_token),
                process_id="proc_sandbox_integration",
                argv=["python", "-m", "runtime_entry"],
                working_directory=workspace_uri + "work/",
                timeout_seconds=30,
                trace_id="trace-sandbox-integration",
            ),
        )
        assert process.status == "STARTING"
        released = await lifecycle.release(
            service_access,
            sandbox_id=accepted.sandbox_id,
            request=SandboxReleaseRequest(
                run_id=str(run.id),
                execution_attempt=1,
                execution_fencing_token=SecretStr(fencing_token),
                trace_id="trace-sandbox-integration",
            ),
        )
        assert released.status == "TERMINATING"
        destroyed = await lifecycle.destroy(
            service_access, sandbox_id=accepted.sandbox_id
        )
        assert destroyed.status == "TERMINATED"
        assert provider.destroy_calls == 1

        cross_tenant_access = SandboxServiceAccess(
            context=service_access.context.model_copy(update={"tenant_id": TENANT_B}),
            permissions=service_access.permissions,
        )
        with pytest.raises(PlatformError) as hidden:
            await lifecycle.get(cross_tenant_access, sandbox_id=accepted.sandbox_id)
        assert hidden.value.code == "RESOURCE_NOT_FOUND"

        async with admin_engine.connect() as connection:
            persisted = (
                await connection.execute(
                    text(
                        "SELECT s.status, o.status AS operation_status, "
                        "l.fencing_token_hash, l.released_at, "
                        "w.status AS workspace_status, w.quota_bytes, "
                        "w.max_files, w.max_file_bytes, "
                        "(SELECT count(*) FROM audit_log a "
                        "WHERE a.resource_id = s.id) AS audit_count "
                        "FROM sandbox_instance s JOIN operation_record o "
                        "ON o.id = s.provision_operation_id "
                        "JOIN sandbox_lease l ON l.sandbox_id = s.id "
                        "JOIN workspace w ON w.tenant_id = s.tenant_id "
                        "AND w.uri = s.workspace_uri "
                        "WHERE s.id = :sandbox_id"
                    ),
                    {"sandbox_id": UUID(accepted.sandbox_id)},
                )
            ).one()
        assert persisted.status == "TERMINATED"
        assert persisted.operation_status == "SUCCEEDED"
        assert persisted.fencing_token_hash == active_lease.execution_fencing_token_hash
        assert persisted.released_at is not None
        assert persisted.workspace_status == "SEALED"
        assert persisted.quota_bytes == 1024 * 1024 * 1024
        assert persisted.max_files == 100
        assert persisted.max_file_bytes == 1024
        assert persisted.audit_count >= 5

        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE sandbox_instance SET policy_hash = :policy_hash "
                        "WHERE id = :sandbox_id"
                    ),
                    {
                        "policy_hash": "sha256:" + "f" * 64,
                        "sandbox_id": UUID(accepted.sandbox_id),
                    },
                )
        with pytest.raises(DBAPIError):
            async with admin_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM sandbox_lease WHERE id = :lease_id"),
                    {"lease_id": UUID(active_lease.lease_id)},
                )
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_publication_queries_are_read_only(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        publications = SqlAlchemySnapshotCompilationStore(session_factory)
        records, _ = await agents.list_agents(
            context(TENANT_A),
            limit=20,
            cursor=None,
            status=None,
            keyword=None,
        )
        agent = next(item for item in records if item.status == "ACTIVE")

        if agent.bindings:
            with pytest.raises(PlatformError) as invalid_preview:
                await publications.preview_agent_publish(
                    context(TENANT_A),
                    agent_id=agent.id,
                    expected_agent_version=agent.resource_version,
                    runtime_targets=("rt_agentscope_a",),
                )
            assert invalid_preview.value.code == "RESOURCE_STATE_CONFLICT"
            repaired = await agents.update_agent(
                context(TENANT_A),
                actor_id=ACTOR,
                agent_id=agent.id,
                expected_version=agent.resource_version,
                request=AgentUpdateRequest.model_validate({"bindings": []}),
                bindings=[],
                metadata=METADATA,
            )
            assert repaired is not None
            agent = repaired

        async with admin_engine.connect() as connection:
            counts_before = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT (SELECT count(*) FROM agent_version), "
                            "(SELECT count(*) FROM agent_snapshot), "
                            "(SELECT count(*) FROM release), "
                            "(SELECT count(*) FROM outbox_event), "
                            "(SELECT count(*) FROM audit_log)"
                        )
                    )
                ).one()
            )

        listed = await publications.list_agent_versions(
            context(TENANT_A),
            agent_id=agent.id,
            limit=20,
            cursor=None,
        )
        assert listed is not None
        versions, next_cursor = listed
        assert versions
        assert next_cursor is None
        latest = versions[0]
        loaded = await publications.get_agent_version(
            context(TENANT_A),
            agent_id=agent.id,
            version_id=latest.version.id,
        )
        assert loaded == latest
        assert (
            await publications.get_agent_version(
                context(TENANT_B),
                agent_id=agent.id,
                version_id=latest.version.id,
            )
            is None
        )
        unchanged = await publications.diff_agent_snapshots(
            context(TENANT_A),
            agent_id=agent.id,
            from_snapshot_id=latest.snapshot.id,
            to_snapshot_id=latest.snapshot.id,
        )
        assert unchanged == ()
        preview = await publications.preview_agent_publish(
            context(TENANT_A),
            agent_id=agent.id,
            expected_agent_version=agent.resource_version,
            runtime_targets=("rt_agentscope_a", "rt_agentscope_new"),
        )
        assert preview is not None
        assert preview.preview_snapshot_hash.startswith("sha256:")
        assert preview.resolved_bindings == ()
        assert preview.ready_to_publish is False
        by_target = {target.runtime_target_id: target for target in preview.targets}
        assert by_target["rt_agentscope_a"].current_deployment_id is not None
        assert by_target["rt_agentscope_a"].current_snapshot_id is not None
        assert by_target["rt_agentscope_a"].changes
        assert by_target["rt_agentscope_new"].current_deployment_id is None
        assert by_target["rt_agentscope_new"].current_snapshot_id is None
        assert by_target["rt_agentscope_new"].changes

        async with admin_engine.connect() as connection:
            counts_after = tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT (SELECT count(*) FROM agent_version), "
                            "(SELECT count(*) FROM agent_snapshot), "
                            "(SELECT count(*) FROM release), "
                            "(SELECT count(*) FROM outbox_event), "
                            "(SELECT count(*) FROM audit_log)"
                        )
                    )
                ).one()
            )
        assert counts_after == counts_before
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_agent_rollback_creates_new_release_and_deployment(
    database_url: str,
) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        snapshots = SqlAlchemySnapshotCompilationStore(session_factory)
        releases = SqlAlchemyReleaseStore(session_factory)
        target_configs = {
            target: RuntimeTargetReleaseConfig(
                runtime_type="agentscope",
                image_digest="registry.example/agentscope@sha256:" + "d" * 64,
            )
            for target in ("rt_agentscope_a", "rt_agentscope_b")
        }
        deployments = SqlAlchemyDeploymentStore(session_factory, target_configs)
        async with TenantUnitOfWork(
            session_factory, context(TENANT_A), read_only=True
        ) as unit_of_work:
            active = await unit_of_work.session.scalar(
                select(DeploymentModel)
                .where(
                    DeploymentModel.tenant_id == UUID(TENANT_A),
                    DeploymentModel.status == "ACTIVE",
                )
                .order_by(DeploymentModel.runtime_target_id)
                .limit(1)
            )
        assert active is not None
        agent = await agents.get_agent(context(TENANT_A), agent_id=active.agent_id)
        assert agent is not None
        historical_snapshot_id = active.snapshot_id
        historical_bundles = await releases.list_runtime_bundles(
            context(TENANT_A), snapshot_id=historical_snapshot_id
        )
        assert historical_bundles

        publication = await snapshots.compile_snapshot(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=agent.id,
            expected_draft_resource_version=agent.resource_version,
            release_note="Publish a later Snapshot before rollback",
            idempotency_key="agent-snapshot-before-rollback",
            request_hash=canonical_request_hash(
                "agent.snapshot.compile",
                extra={
                    "agent_id": str(agent.id),
                    "draft_version": agent.resource_version,
                    "scenario": "before-rollback",
                },
            ),
            metadata=METADATA,
        )
        assert publication is not None
        assert publication.snapshot.id != historical_snapshot_id
        current_agent = await agents.get_agent(context(TENANT_A), agent_id=agent.id)
        assert current_agent is not None
        newer_bundle = RuntimeBundleRecord(
            id=uuid5(
                NAMESPACE_URL,
                f"rollback-integration-bundle/{publication.snapshot.id}",
            ),
            tenant_id=UUID(TENANT_A),
            snapshot_id=publication.snapshot.id,
            runtime_type="agentscope",
            compiler_name=BUNDLE_COMPILER_NAME,
            compiler_version=BUNDLE_COMPILER_VERSION,
            manifest_schema_version="1.0",
            manifest=admitted_manifest(),
            content_hash="sha256:" + "f" * 64,
            object_uri="s3://integration/bundles/agentscope-newer.tar",
            size_bytes=1024,
            signature_ref="sigstore://integration/agentscope-newer",
            sbom_ref="s3://integration/bundles/agentscope-newer.spdx.json",
            scan_status="PASSED",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        )
        await releases.store_runtime_bundle(context(TENANT_A), record=newer_bundle)

        async def activate_release(
            release: ReleaseRecord, bundle: RuntimeBundleRecord
        ) -> ReleaseRecord:
            current = await releases.transition_release(
                context(TENANT_A),
                release_id=release.id,
                expected_status="REQUESTED",
                target_status="VALIDATING",
            )
            current = await releases.transition_release(
                context(TENANT_A),
                release_id=current.id,
                expected_status="VALIDATING",
                target_status="COMPILING",
                snapshot_id=bundle.snapshot_id,
            )
            current = await releases.transition_release(
                context(TENANT_A),
                release_id=current.id,
                expected_status="COMPILING",
                target_status="SCANNING",
            )
            current = await releases.transition_release(
                context(TENANT_A),
                release_id=current.id,
                expected_status="SCANNING",
                target_status="ACTIVATING",
            )
            deployment_ids = await deployments.activate(
                context(TENANT_A), release=current, bundles=(bundle,)
            )
            return await releases.complete_release(
                context(TENANT_A),
                release_id=current.id,
                expected_status="ACTIVATING",
                deployment_ids=deployment_ids,
            )

        publish_outcome = await releases.request_release(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=agent.id,
            expected_agent_version=current_agent.resource_version,
            runtime_targets=("rt_agentscope_a", "rt_agentscope_b"),
            release_note="Activate later Snapshot",
            run_smoke_test=False,
            activate_on_success=True,
            idempotency_key="publish-before-agent-rollback",
            request_hash=canonical_request_hash(
                "agent.release.request",
                extra={"snapshot_id": str(publication.snapshot.id)},
            ),
            metadata=METADATA,
        )
        assert publish_outcome is not None and publish_outcome.value is not None
        published = await activate_release(publish_outcome.value, newer_bundle)
        assert published.status == "SUCCEEDED"

        async with admin_engine.connect() as connection:
            version_count_before = await connection.scalar(
                text("SELECT count(*) FROM agent_version")
            )
        rollback_hash = canonical_request_hash(
            "agent.rollback.request",
            extra={
                "agent_id": str(agent.id),
                "snapshot_id": str(historical_snapshot_id),
                "runtime_targets": ["rt_agentscope_a", "rt_agentscope_b"],
            },
        )
        assert (
            await releases.request_rollback(
                context(TENANT_B),
                actor_id=ACTOR,
                agent_id=agent.id,
                snapshot_id=historical_snapshot_id,
                runtime_targets=("rt_agentscope_a", "rt_agentscope_b"),
                release_note="Cross-tenant rollback must not resolve",
                idempotency_key="agent-rollback-cross-tenant",
                request_hash=rollback_hash,
                metadata=METADATA,
            )
            is None
        )
        rollback_outcome = await releases.request_rollback(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=agent.id,
            snapshot_id=historical_snapshot_id,
            runtime_targets=("rt_agentscope_a", "rt_agentscope_b"),
            release_note="Rollback to historical Snapshot",
            idempotency_key="agent-rollback-integration",
            request_hash=rollback_hash,
            metadata=METADATA,
        )
        assert rollback_outcome is not None and rollback_outcome.value is not None
        rollback_release = rollback_outcome.value
        assert rollback_release.release_kind == "ROLLBACK"
        assert rollback_release.expected_agent_version is None
        assert rollback_release.requested_snapshot_id == historical_snapshot_id
        replay = await releases.request_rollback(
            context(TENANT_A),
            actor_id=ACTOR,
            agent_id=agent.id,
            snapshot_id=historical_snapshot_id,
            runtime_targets=("rt_agentscope_a", "rt_agentscope_b"),
            release_note="Rollback to historical Snapshot",
            idempotency_key="agent-rollback-integration",
            request_hash=rollback_hash,
            metadata=METADATA,
        )
        assert replay is not None and replay.replay is not None
        assert replay.replay.response_body["release_id"] == str(rollback_release.id)

        rolled_back = await activate_release(rollback_release, historical_bundles[0])
        assert rolled_back.status == "SUCCEEDED"
        assert rolled_back.snapshot_id == historical_snapshot_id
        assert set(rolled_back.deployment_ids).isdisjoint(published.deployment_ids)
        async with admin_engine.connect() as connection:
            version_count_after = await connection.scalar(
                text("SELECT count(*) FROM agent_version")
            )
        assert version_count_after == version_count_before

        async with admin_engine.connect() as connection:
            active_rows = (
                await connection.execute(
                    text(
                        "SELECT snapshot_id, count(*) FROM deployment "
                        "WHERE tenant_id = :tenant_id AND agent_id = :agent_id "
                        "AND status = 'ACTIVE' GROUP BY snapshot_id"
                    ),
                    {"tenant_id": TENANT_A, "agent_id": agent.id},
                )
            ).all()
            assert [tuple(row) for row in active_rows] == [(historical_snapshot_id, 2)]
            rollback_row = (
                await connection.execute(
                    text(
                        "SELECT release_kind, expected_agent_version, "
                        "requested_snapshot_id, snapshot_id, status "
                        "FROM release WHERE id = :release_id"
                    ),
                    {"release_id": rollback_release.id},
                )
            ).one()
            assert tuple(rollback_row) == (
                "ROLLBACK",
                None,
                historical_snapshot_id,
                historical_snapshot_id,
                "SUCCEEDED",
            )
            audit_actions = list(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM audit_log "
                            "WHERE resource_id = :release_id"
                        ),
                        {"release_id": rollback_release.id},
                    )
                ).scalars()
            )
            assert audit_actions == ["agent.rollback.requested"]
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_resource_registry(database_url: str) -> None:
    admin_engine = create_async_engine(database_url)
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'")
            )
            await connection.execute(
                text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
            )
            await connection.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    f"IN SCHEMA public TO {APP_ROLE}"
                )
            )
            await connection.execute(
                text(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES "
                    f"IN SCHEMA public TO {APP_ROLE}"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant (id, code, name) VALUES "
                    "(:tenant_a, 'tenant-a', 'Tenant A'), "
                    "(:tenant_b, 'tenant-b', 'Tenant B')"
                ),
                {"tenant_a": TENANT_A, "tenant_b": TENANT_B},
            )
            await connection.execute(
                text(
                    "INSERT INTO app_user "
                    "(id, identity_issuer, external_subject, display_name) VALUES "
                    "(:actor_id, 'https://issuer.test', 'resource-owner', "
                    "'Resource Owner'), "
                    "(:other_actor_id, 'https://issuer.test', 'other-owner', "
                    "'Other Owner')"
                ),
                {"actor_id": ACTOR, "other_actor_id": OTHER_ACTOR},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenant_member (tenant_id, user_id) VALUES "
                    "(:tenant_a, :actor_id), (:tenant_a, :other_actor_id), "
                    "(:tenant_b, :actor_id)"
                ),
                {
                    "tenant_a": TENANT_A,
                    "tenant_b": TENANT_B,
                    "actor_id": ACTOR,
                    "other_actor_id": OTHER_ACTOR,
                },
            )

        app_engine = create_async_engine(app_url)
        try:
            registry = SqlAlchemyResourceRegistry(create_session_factory(app_engine))
            request = prompt_request()
            request_hash = canonical_request_hash("prompt.create", request)
            created = await registry.create_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                request=request,
                idempotency_key="prompt-create-welcome",
                request_hash=request_hash,
                metadata=METADATA,
            )
            assert created.value is not None
            definition = created.value
            assert definition.resource_version == 1
            replay = await registry.create_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                request=request,
                idempotency_key="prompt-create-welcome",
                request_hash=request_hash,
                metadata=METADATA,
            )
            assert replay.replay is not None
            assert replay.replay.response_body["id"] == str(definition.id)

            with pytest.raises(PlatformError) as reused:
                changed = prompt_request(template="Different request")
                await registry.create_definition(
                    context(TENANT_A),
                    actor_id=ACTOR,
                    resource_type="prompt",
                    request=changed,
                    idempotency_key="prompt-create-welcome",
                    request_hash=canonical_request_hash("prompt.create", changed),
                    metadata=METADATA,
                )
            assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"

            tenant_b = await registry.create_definition(
                context(TENANT_B),
                actor_id=ACTOR,
                resource_type="prompt",
                request=request,
                idempotency_key="prompt-create-welcome-b",
                request_hash=request_hash,
                metadata=METADATA,
            )
            assert tenant_b.value is not None
            assert tenant_b.value.code == definition.code
            assert (
                await registry.get_definition(
                    context(TENANT_B),
                    resource_type="prompt",
                    resource_id=definition.id,
                )
                is None
            )

            updated = await registry.update_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=1,
                request=ResourceUpdateRequest(
                    name="Welcome Prompt V2",
                    description=None,
                    visibility=None,
                    content_schema_version=None,
                    content=None,
                ),
                metadata=METADATA,
            )
            assert updated is not None
            assert updated.resource_version == 2
            with pytest.raises(PlatformError) as stale:
                await registry.update_definition(
                    context(TENANT_A),
                    actor_id=ACTOR,
                    resource_type="prompt",
                    resource_id=definition.id,
                    expected_version=1,
                    request=ResourceUpdateRequest(
                        name="Stale",
                        description=None,
                        visibility=None,
                        content_schema_version=None,
                        content=None,
                    ),
                    metadata=METADATA,
                )
            assert stale.value.code == "RESOURCE_VERSION_CONFLICT"

            publish_request = ResourcePublishRequest(
                expected_resource_version=2, release_note="Initial version"
            )
            published = await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="prompt-publish-v1",
                request_hash=canonical_request_hash(
                    "prompt.publish", publish_request, extra={"id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert published.value is not None
            version_one = published.value
            assert version_one.version_no == 1
            assert version_one.content_hash.startswith("sha256:")
            published_replay = await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="prompt-publish-v1",
                request_hash=canonical_request_hash(
                    "prompt.publish", publish_request, extra={"id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert published_replay.replay is not None
            assert published_replay.replay.response_body["id"] == str(version_one.id)

            revised_content = ResourceContentPrompt(
                resource_type="prompt",
                template="Hi {{ name }}",
                variables=[],
                language="en",
                compiler_policy_version="1",
            )
            revised = await registry.update_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=3,
                request=ResourceUpdateRequest(
                    name=None,
                    description=None,
                    visibility=None,
                    content_schema_version=None,
                    content=revised_content,
                ),
                metadata=METADATA,
            )
            assert revised is not None
            assert revised.resource_version == 4
            assert isinstance(version_one.content, ResourceContentPrompt)
            assert version_one.content.template == "Hello {{ name }}"

            second_publish = ResourcePublishRequest(
                expected_resource_version=4, release_note="Revised greeting"
            )
            version_two = await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=second_publish,
                idempotency_key="prompt-publish-v2",
                request_hash=canonical_request_hash(
                    "prompt.publish", second_publish, extra={"id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert version_two.value is not None
            assert version_two.value.version_no == 2

            duplicate_publish = ResourcePublishRequest(
                expected_resource_version=5, release_note="Duplicate content"
            )
            with pytest.raises(PlatformError) as duplicate:
                await registry.publish_version(
                    context(TENANT_A),
                    actor_id=ACTOR,
                    resource_type="prompt",
                    resource_id=definition.id,
                    request=duplicate_publish,
                    idempotency_key="prompt-publish-duplicate",
                    request_hash=canonical_request_hash(
                        "prompt.publish",
                        duplicate_publish,
                        extra={"id": str(definition.id)},
                    ),
                    metadata=METADATA,
                )
            assert duplicate.value.code == "RESOURCE_STATE_CONFLICT"

            versions, cursor = await registry.list_versions(
                context(TENANT_A),
                resource_type="prompt",
                resource_id=definition.id,
                limit=20,
                cursor=None,
            )
            assert [version.version_no for version in versions] == [2, 1]
            assert cursor is None
            assert isinstance(versions[1].content, ResourceContentPrompt)
            assert versions[1].content.template == "Hello {{ name }}"

            rollback_request = ResourceRollbackRequest(
                version_id=str(version_one.id),
                expected_resource_version=5,
                release_note="Restore initial wording",
            )
            rolled_back = await registry.rollback_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                source_version_id=version_one.id,
                request=rollback_request,
                idempotency_key="prompt-rollback-v1",
                request_hash=canonical_request_hash(
                    "prompt.rollback",
                    rollback_request,
                    extra={"resource_id": str(definition.id)},
                ),
                metadata=METADATA,
            )
            assert rolled_back is not None
            assert rolled_back.value is not None
            assert rolled_back.value.version_no == 3
            assert rolled_back.value.content_hash == version_one.content_hash
            assert isinstance(rolled_back.value.content, ResourceContentPrompt)
            assert rolled_back.value.content.template == "Hello {{ name }}"

            disabled = await registry.set_definition_status(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=6,
                enabled=False,
                request=ActionRequest(reason="Maintenance"),
                idempotency_key="prompt-disable-1",
                request_hash=canonical_request_hash(
                    "prompt.disable", extra={"resource_id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert disabled is not None
            assert disabled.value is not None
            assert disabled.value.status == "DISABLED"
            enabled = await registry.set_definition_status(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                expected_version=7,
                enabled=True,
                request=None,
                idempotency_key="prompt-enable-1",
                request_hash=canonical_request_hash(
                    "prompt.enable", extra={"resource_id": str(definition.id)}
                ),
                metadata=METADATA,
            )
            assert enabled is not None
            assert enabled.value is not None
            assert enabled.value.status == "ACTIVE"

            copy_request = ResourceCopyRequest(
                code="welcome_prompt_copy", name="Welcome Prompt Copy"
            )
            copied = await registry.copy_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=definition.id,
                request=copy_request,
                idempotency_key="prompt-copy-1",
                request_hash=canonical_request_hash(
                    "prompt.copy",
                    copy_request,
                    extra={"resource_id": str(definition.id)},
                ),
                metadata=METADATA,
            )
            assert copied is not None
            assert copied.value is not None
            assert copied.value.code == "welcome_prompt_copy"
            deleted = await registry.delete_definition(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="prompt",
                resource_id=copied.value.id,
                expected_version=1,
                idempotency_key="prompt-delete-copy",
                request_hash=canonical_request_hash(
                    "prompt.delete", extra={"resource_id": str(copied.value.id)}
                ),
                metadata=METADATA,
            )
            assert deleted is not None
            assert deleted.value is not None
            assert deleted.value.status == "SUCCEEDED"
            assert (
                await registry.get_definition(
                    context(TENANT_A),
                    resource_type="prompt",
                    resource_id=copied.value.id,
                )
                is None
            )
        finally:
            await app_engine.dispose()

        async with admin_engine.connect() as connection:
            actions = list(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM audit_log "
                            "WHERE request_id = :request_id ORDER BY created_at, id"
                        ),
                        {"request_id": METADATA.request_id},
                    )
                ).scalars()
            )
            assert actions.count("resource.create") == 2
            assert actions.count("resource.update") == 2
            assert actions.count("resource.publish") == 2
            assert actions.count("resource.rollback") == 1
            assert actions.count("resource.disable") == 1
            assert actions.count("resource.enable") == 1
            assert actions.count("resource.copy") == 1
            assert actions.count("resource.delete") == 1
            audit_payload = str(
                (
                    await connection.execute(
                        text(
                            "SELECT jsonb_agg(metadata_json) FROM audit_log "
                            "WHERE request_id = :request_id"
                        ),
                        {"request_id": METADATA.request_id},
                    )
                ).scalar_one()
            )
            assert "Hi {{ name }}" not in audit_payload
            assert "Maintenance" not in audit_payload
    finally:
        await admin_engine.dispose()


async def verify_skill_supply_chain_publication(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    try:
        session_factory = create_session_factory(app_engine)
        registry = SqlAlchemyResourceRegistry(session_factory)
        scan_store = SqlAlchemySkillScanStore(session_factory)
        request = skill_request()
        created = await registry.create_definition(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="skill",
            request=request,
            idempotency_key="skill-create-integration",
            request_hash=canonical_request_hash("skill.create", request),
            metadata=SKILL_METADATA,
        )
        assert created.value is not None
        definition = created.value
        content_hash = canonical_content_hash(definition.content)
        publish_request = ResourcePublishRequest(
            expected_resource_version=1, release_note="scanned release"
        )

        with pytest.raises(PlatformError) as missing:
            await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="skill",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="skill-publish-missing-scan",
                request_hash=canonical_request_hash(
                    "skill.publish.missing", publish_request
                ),
                metadata=SKILL_METADATA,
            )
        assert missing.value.code == "RESOURCE_STATE_CONFLICT"

        mismatched = await scan_store.record_scan(
            context(TENANT_A),
            definition_id=definition.id,
            draft_resource_version=1,
            content_hash="sha256:" + "9" * 64,
            result=skill_scan_result(),
            scanned_by=ACTOR,
            metadata=SKILL_METADATA,
        )
        with pytest.raises(PlatformError) as mismatch:
            await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="skill",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="skill-publish-mismatched-scan",
                request_hash=canonical_request_hash(
                    "skill.publish.mismatch", publish_request
                ),
                metadata=SKILL_METADATA,
                scan_attestation_id=mismatched.id,
            )
        assert mismatch.value.code == "RESOURCE_STATE_CONFLICT"

        rejected = await scan_store.record_scan(
            context(TENANT_A),
            definition_id=definition.id,
            draft_resource_version=1,
            content_hash=content_hash,
            result=skill_scan_result("REJECTED"),
            scanned_by=ACTOR,
            metadata=SKILL_METADATA,
        )
        with pytest.raises(PlatformError) as rejected_publish:
            await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="skill",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="skill-publish-rejected-scan",
                request_hash=canonical_request_hash(
                    "skill.publish.rejected", publish_request
                ),
                metadata=SKILL_METADATA,
                scan_attestation_id=rejected.id,
            )
        assert rejected_publish.value.code == "RESOURCE_STATE_CONFLICT"

        passed = await scan_store.record_scan(
            context(TENANT_A),
            definition_id=definition.id,
            draft_resource_version=1,
            content_hash=content_hash,
            result=skill_scan_result(),
            scanned_by=ACTOR,
            metadata=SKILL_METADATA,
        )
        publish_hash = canonical_request_hash("skill.publish", publish_request)
        published = await registry.publish_version(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="skill",
            resource_id=definition.id,
            request=publish_request,
            idempotency_key="skill-publish-passed-scan",
            request_hash=publish_hash,
            metadata=SKILL_METADATA,
            scan_attestation_id=passed.id,
        )
        assert published.value is not None
        version_one = published.value
        bundle_reader = SqlAlchemyBundleInputReader(session_factory)
        assert (
            await bundle_reader.get_resource_version(
                context(TENANT_A),
                resource_id=definition.id,
                version_id=version_one.id,
            )
            is not None
        )
        replay = await registry.publish_version(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="skill",
            resource_id=definition.id,
            request=publish_request,
            idempotency_key="skill-publish-passed-scan",
            request_hash=publish_hash,
            metadata=SKILL_METADATA,
            scan_attestation_id=passed.id,
        )
        assert replay.replay is not None
        assert replay.replay.response_body["id"] == str(version_one.id)

        rollback_scan = await scan_store.record_scan(
            context(TENANT_A),
            definition_id=definition.id,
            draft_resource_version=2,
            content_hash=version_one.content_hash,
            result=skill_scan_result(),
            scanned_by=ACTOR,
            metadata=SKILL_METADATA,
        )
        rollback_request = ResourceRollbackRequest(
            version_id=str(version_one.id),
            expected_resource_version=2,
            release_note="verified rollback",
        )
        rollback = await registry.rollback_version(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="skill",
            resource_id=definition.id,
            source_version_id=version_one.id,
            request=rollback_request,
            idempotency_key="skill-rollback-passed-scan",
            request_hash=canonical_request_hash("skill.rollback", rollback_request),
            metadata=SKILL_METADATA,
            scan_attestation_id=rollback_scan.id,
        )
        assert rollback is not None
        assert rollback.value is not None
        assert rollback.value.version_no == 2

        async with TenantUnitOfWork(session_factory, context(TENANT_A)) as unit_of_work:
            bound = await unit_of_work.session.scalar(
                select(SkillSupplyChainScanModel).where(
                    SkillSupplyChainScanModel.id == passed.id
                )
            )
            assert bound is not None
            assert bound.published_version_id == version_one.id
        async with TenantUnitOfWork(session_factory, context(TENANT_B)) as unit_of_work:
            assert (
                await unit_of_work.session.scalar(
                    select(SkillSupplyChainScanModel).where(
                        SkillSupplyChainScanModel.id == passed.id
                    )
                )
                is None
            )
        with pytest.raises(DBAPIError):
            async with TenantUnitOfWork(
                session_factory, context(TENANT_A)
            ) as unit_of_work:
                await unit_of_work.session.execute(
                    text(
                        "UPDATE skill_supply_chain_scan SET report_hash = :report_hash "
                        "WHERE id = :scan_id"
                    ),
                    {"report_hash": "sha256:" + "8" * 64, "scan_id": passed.id},
                )
    finally:
        await app_engine.dispose()


async def verify_mcp_capability_publication(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    try:
        session_factory = create_session_factory(app_engine)
        registry = SqlAlchemyResourceRegistry(session_factory)
        discoveries = SqlAlchemyMcpDiscoveryStore(session_factory)
        secret_ref = f"secret://tenant/{TENANT_A}/mcp/search"
        content = ResourceContentMcp(
            resource_type="mcp",
            transport="streamable_http",
            endpoint="https://mcp.example.test/v1",
            header_templates={"Authorization": f"Bearer ${{{secret_ref}}}"},
            secret_refs=[secret_ref],
            timeout_seconds=30,
            allowed_tools=["search.query"],
        )
        request = ResourceCreateRequest(
            code="integration_mcp",
            name="Integration MCP",
            description=None,
            visibility="tenant",
            content_schema_version="1.0",
            content=content,
        )
        created = await registry.create_definition(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="mcp",
            request=request,
            idempotency_key="mcp-create-integration",
            request_hash=canonical_request_hash("mcp.create", request),
            metadata=METADATA,
        )
        assert created.value is not None
        definition = created.value
        content_hash = canonical_content_hash(content)
        publish_request = ResourcePublishRequest(
            expected_resource_version=1, release_note="discovered release"
        )

        with pytest.raises(PlatformError) as missing:
            await registry.publish_version(
                context(TENANT_A),
                actor_id=ACTOR,
                resource_type="mcp",
                resource_id=definition.id,
                request=publish_request,
                idempotency_key="mcp-publish-missing-discovery",
                request_hash=canonical_request_hash(
                    "mcp.publish.missing", publish_request
                ),
                metadata=METADATA,
            )
        assert missing.value.code == "RESOURCE_STATE_CONFLICT"

        requested = await registry.request_mcp_capability_discovery(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_id=definition.id,
            expected_resource_version=1,
            content_hash=content_hash,
            idempotency_key="mcp-discover-integration",
            request_hash=canonical_request_hash(
                "mcp.discover", extra={"resource_id": str(definition.id)}
            ),
            metadata=METADATA,
        )
        assert requested is not None and requested.value is not None
        operation_id = requested.value.id
        discovery_target = McpDiscoveryTarget(
            definition_id=definition.id,
            draft_resource_version=1,
            content_hash=content_hash,
            transport="streamable_http",
            endpoint=content.endpoint,
            header_templates=content.header_templates or {},
            secret_refs=tuple(content.secret_refs),
            timeout_seconds=content.timeout_seconds,
            allowed_tools=tuple(content.allowed_tools or ()),
        )
        tool = McpDiscoveredTool(
            name="search.query",
            description="Search approved public sources.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema=None,
            schema_hash="sha256:" + "7" * 64,
            risk_level="MEDIUM",
        )
        discovery_result = McpDiscoveryResult(
            status="PASSED",
            protocol_version="2025-06-18",
            server_name="integration-mcp",
            server_version="1.0.0",
            tools=(tool,),
            capability_hash="sha256:" + "8" * 64,
            findings=(),
        )
        await discoveries.mark_running(context(TENANT_A), operation_id)
        evidence = await discoveries.record_terminal(
            context(TENANT_A),
            operation_id=operation_id,
            target=discovery_target,
            result=discovery_result,
            discovered_by=ACTOR,
            error=None,
        )
        publishable = await discoveries.get_publishable_evidence(
            context(TENANT_A),
            definition_id=definition.id,
            draft_resource_version=1,
            content_hash=content_hash,
            allowed_tools=("search.query",),
        )
        assert publishable is not None and publishable.id == evidence.id

        published = await registry.publish_version(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="mcp",
            resource_id=definition.id,
            request=publish_request,
            idempotency_key="mcp-publish-passed-discovery",
            request_hash=canonical_request_hash("mcp.publish", publish_request),
            metadata=METADATA,
            mcp_discovery_attestation_id=evidence.id,
        )
        assert published.value is not None
        version_one = published.value
        bundle_reader = SqlAlchemyBundleInputReader(session_factory)
        assert (
            await bundle_reader.get_resource_version(
                context(TENANT_A),
                resource_id=definition.id,
                version_id=version_one.id,
            )
            is not None
        )
        capability = await bundle_reader.get_mcp_capability_snapshot(
            context(TENANT_A), published_version_id=version_one.id
        )
        assert capability is not None
        assert capability.capability_hash == discovery_result.capability_hash
        assert capability.allowed_tools == ("search.query",)
        assert capability.tools == (tool,)

        source_evidence = await discoveries.get_published_evidence(
            context(TENANT_A),
            source_version_id=version_one.id,
            definition_id=definition.id,
            content_hash=version_one.content_hash,
            allowed_tools=("search.query",),
        )
        assert source_evidence is not None
        rollback_request = ResourceRollbackRequest(
            version_id=str(version_one.id),
            expected_resource_version=2,
            release_note="capability-preserving rollback",
        )
        rollback = await registry.rollback_version(
            context(TENANT_A),
            actor_id=ACTOR,
            resource_type="mcp",
            resource_id=definition.id,
            source_version_id=version_one.id,
            request=rollback_request,
            idempotency_key="mcp-rollback-passed-discovery",
            request_hash=canonical_request_hash("mcp.rollback", rollback_request),
            metadata=METADATA,
            mcp_discovery_attestation_id=source_evidence.id,
        )
        assert rollback is not None and rollback.value is not None
        rollback_capability = await bundle_reader.get_mcp_capability_snapshot(
            context(TENANT_A), published_version_id=rollback.value.id
        )
        assert rollback_capability is not None
        assert rollback_capability.capability_hash == capability.capability_hash

        async with TenantUnitOfWork(session_factory, context(TENANT_A)) as unit_of_work:
            rows = list(
                (
                    await unit_of_work.session.scalars(
                        select(McpCapabilityDiscoveryModel)
                        .where(
                            McpCapabilityDiscoveryModel.definition_id == definition.id
                        )
                        .order_by(McpCapabilityDiscoveryModel.source_discovery_id)
                    )
                ).all()
            )
            assert len(rows) == 2
            clone = next(row for row in rows if row.source_discovery_id is not None)
            assert clone.source_discovery_id == evidence.id
            assert clone.published_version_id == rollback.value.id
        async with TenantUnitOfWork(session_factory, context(TENANT_B)) as unit_of_work:
            assert (
                await unit_of_work.session.scalar(
                    select(McpCapabilityDiscoveryModel).where(
                        McpCapabilityDiscoveryModel.id == evidence.id
                    )
                )
                is None
            )
        with pytest.raises(DBAPIError):
            async with TenantUnitOfWork(
                session_factory, context(TENANT_A)
            ) as unit_of_work:
                await unit_of_work.session.execute(
                    text(
                        "UPDATE mcp_capability_discovery "
                        "SET capability_hash = :capability_hash WHERE id = :id"
                    ),
                    {"capability_hash": "sha256:" + "9" * 64, "id": evidence.id},
                )
    finally:
        await app_engine.dispose()


async def verify_approval_control_plane(database_url: str) -> None:
    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        sessions = SqlAlchemySessionStore(session_factory)
        runs = SqlAlchemyRunStore(session_factory)
        approvals = SqlAlchemyApprovalStore(session_factory)
        coordinator = ApprovalCoordinator(approvals)
        agent_records, _ = await agents.list_agents(
            context(TENANT_A), limit=50, cursor=None, status=None, keyword=None
        )
        agent = next(item for item in agent_records if item.active_deployment_id)
        now = datetime.now(UTC)

        async def create_pending(
            suffix: str, *, expires_at: datetime
        ) -> tuple[RunRecord, ApprovalRequestRecord, ApprovalRequestInput]:
            session_request = SessionCreateRequest.model_validate(
                {
                    "agent_id": str(agent.id),
                    "title": f"AP-E6-004 Approval {suffix}",
                }
            )
            session_outcome = await sessions.create_session(
                context(TENANT_A),
                user_id=ACTOR,
                request=session_request,
                metadata_value={},
                idempotency_key=f"approval-session-{suffix}",
                request_hash=canonical_request_hash("session.create", session_request),
                metadata=METADATA,
            )
            assert session_outcome.value is not None
            run_request = RunCreateRequest.model_validate(
                {
                    "session_id": str(session_outcome.value.id),
                    "input": {"text": f"approval integration {suffix}"},
                }
            )
            run_outcome = await runs.create_run(
                context(TENANT_A),
                user_id=ACTOR,
                request=run_request,
                idempotency_key=f"approval-run-{suffix}",
                request_hash=canonical_request_hash("run.create", run_request),
                metadata=METADATA,
            )
            assert run_outcome.value is not None
            run = run_outcome.value
            fencing_token_hash = "sha256:" + "7" * 64
            await runs.prepare_run(
                context(TENANT_A),
                run_id=run.id,
                workflow_id=f"run/{TENANT_A}/{run.id}",
                execution_attempt=1,
                fencing_token_hash=fencing_token_hash,
            )
            await runs.mark_run_running(
                context(TENANT_A),
                run_id=run.id,
                execution_attempt=1,
                worker_id=f"approval-worker-{suffix}",
                fencing_token_hash=fencing_token_hash,
            )
            request = ApprovalRequestInput(
                run_id=run.id,
                execution_attempt=1,
                requester_id=ACTOR,
                tool_call_id=f"tool-call-{suffix}",
                tool_name="production.write",
                tool_schema_hash="sha256:" + "8" * 64,
                parameter_digest=canonical_tool_parameter_digest(
                    {"resource": "redacted"}
                ),
                policy_version="sha256:" + "a" * 64,
                deployment_id=run.deployment_id,
                expires_at=expires_at,
            )
            approval = await coordinator.request_approval(
                context(TENANT_A), request=request, metadata=METADATA, now=now
            )
            return run, approval, request

        approved_run, pending, approval_request = await create_pending(
            "approved", expires_at=now + timedelta(minutes=20)
        )
        assert pending.status == "PENDING"
        assert pending.resource_version == 1
        duplicate = await coordinator.request_approval(
            context(TENANT_A),
            request=approval_request,
            metadata=METADATA,
            now=now,
        )
        assert duplicate == pending
        with pytest.raises(PlatformError) as changed_identity:
            await coordinator.request_approval(
                context(TENANT_A),
                request=replace(approval_request, tool_name="production.delete"),
                metadata=METADATA,
                now=now,
            )
        assert changed_identity.value.code == "RESOURCE_STATE_CONFLICT"
        assert (
            await approvals.get_approval(
                context(TENANT_B), approval_id=pending.id, now=now
            )
            is None
        )

        self_decision = ApprovalDecisionRequest(decision="APPROVED", comment=None)
        with pytest.raises(PlatformError) as self_approval:
            await approvals.decide(
                context(TENANT_A),
                approval_id=pending.id,
                actor_id=ACTOR,
                request=self_decision,
                expected_version=1,
                idempotency_key="approval-self-decision",
                request_hash=canonical_request_hash("approval.decision", self_decision),
                metadata=METADATA,
                now=now + timedelta(seconds=1),
            )
        assert self_approval.value.code == "RESOURCE_STATE_CONFLICT"

        approved_decision = ApprovalDecisionRequest(
            decision="APPROVED", comment="reviewed"
        )
        ticket_issuer = HmacExecutionTicketIssuer(SecretStr("t" * 32))
        ticket_credential = ticket_issuer.issue(
            tenant_id=UUID(TENANT_A), approval_id=pending.id
        )
        approved_hash = canonical_request_hash("approval.decision", approved_decision)
        with pytest.raises(PlatformError) as stale_version:
            await approvals.decide(
                context(TENANT_A, OTHER_ACTOR),
                approval_id=pending.id,
                actor_id=OTHER_ACTOR,
                request=approved_decision,
                expected_version=99,
                idempotency_key="approval-stale-version",
                request_hash=approved_hash,
                metadata=METADATA,
                now=now + timedelta(seconds=1),
            )
        assert stale_version.value.code == "RESOURCE_VERSION_CONFLICT"
        approved = await approvals.decide(
            context(TENANT_A, OTHER_ACTOR),
            approval_id=pending.id,
            actor_id=OTHER_ACTOR,
            request=approved_decision,
            expected_version=1,
            idempotency_key="approval-approved-decision",
            request_hash=approved_hash,
            metadata=METADATA,
            now=now + timedelta(seconds=2),
            ticket_issue=ExecutionTicketIssue(
                credential=ticket_credential,
                expires_at=now + timedelta(minutes=5),
            ),
        )
        assert approved is not None and approved.value is not None
        assert approved.value.status == "APPROVED"
        assert approved.value.resource_version == 2
        replay = await approvals.decide(
            context(TENANT_A, OTHER_ACTOR),
            approval_id=pending.id,
            actor_id=OTHER_ACTOR,
            request=approved_decision,
            expected_version=1,
            idempotency_key="approval-approved-decision",
            request_hash=approved_hash,
            metadata=METADATA,
            now=now + timedelta(seconds=3),
        )
        assert replay is not None and replay.replay is not None
        decision = await approvals.get_decision(
            context(TENANT_A, OTHER_ACTOR), approval_id=pending.id
        )
        assert decision is not None
        assert decision.actor_id == OTHER_ACTOR
        assert decision.comment == "reviewed"
        ticket = await approvals.get_ticket_for_approval(
            context(TENANT_A, OTHER_ACTOR), approval_id=pending.id
        )
        assert ticket is not None
        assert ticket.id == ticket_credential.ticket_id
        assert ticket.nonce_hash == ticket_credential.nonce_hash
        assert ticket.consumed_at is None
        assert ticket.expires_at == now + timedelta(minutes=5)

        execution_role_id = uuid5(
            NAMESPACE_URL, f"tool-execution-role/{TENANT_A}/{ACTOR}"
        )
        async with admin_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO role (id, tenant_id, code, name, built_in) "
                    "VALUES (:id, :tenant_id, 'tool_executor', 'Tool Executor', false)"
                ),
                {"id": execution_role_id, "tenant_id": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO role_permission "
                    "(tenant_id, role_id, resource_type, action) "
                    "VALUES (:tenant_id, :role_id, 'mcp', 'execute')"
                ),
                {"tenant_id": TENANT_A, "role_id": execution_role_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO role_binding "
                    "(tenant_id, subject_type, subject_id, role_id) "
                    "VALUES (:tenant_id, 'user', :subject_id, :role_id)"
                ),
                {
                    "tenant_id": TENANT_A,
                    "subject_id": ACTOR,
                    "role_id": execution_role_id,
                },
            )

        class ToolExecutor:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(
                self, context: TenantContext, *, request: ToolExecutionRequest
            ) -> ToolExecutionResult:
                self.calls += 1
                return ToolExecutionResult(status="SUCCEEDED", output={"ok": True})

        executor = ToolExecutor()
        gateway = ToolGatewayService(
            SqlAlchemyExecutionTicketStore(session_factory),
            SqlAlchemyToolAuthorizationResolver(session_factory),
            executor,
        )
        tool_request = ToolExecutionRequest(
            tenant_id=UUID(TENANT_A),
            run_id=approved_run.id,
            execution_attempt=1,
            approval_id=pending.id,
            requester_id=ACTOR,
            deployment_id=approved_run.deployment_id,
            ticket_ref=ticket_credential.ticket_ref,
            ticket_nonce=ticket_credential.nonce,
            tool_name=approval_request.tool_name,
            tool_schema_hash=approval_request.tool_schema_hash,
            parameter_digest=approval_request.parameter_digest,
            policy_version=approval_request.policy_version,
            arguments={"resource": "redacted"},
        )
        result = await gateway.execute(
            context(TENANT_A),
            request=tool_request,
            metadata=METADATA,
            now=now + timedelta(seconds=4),
        )
        assert result.status == "SUCCEEDED"
        assert executor.calls == 1
        with pytest.raises(ToolExecutionDenied) as replayed:
            await gateway.execute(
                context(TENANT_A),
                request=tool_request,
                metadata=METADATA,
                now=now + timedelta(seconds=5),
            )
        assert replayed.value.code == "EXECUTION_TICKET_REPLAY"
        assert executor.calls == 1
        async with admin_engine.connect() as connection:
            ticket_audits = list(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM audit_log "
                            "WHERE resource_id = :approval_id "
                            "AND action IN ('ticket.consume','ticket.replay','tool.execute') "
                            "ORDER BY created_at"
                        ),
                        {"approval_id": pending.id},
                    )
                ).scalars()
            )
        assert sorted(ticket_audits) == [
            "ticket.consume",
            "ticket.replay",
            "tool.execute",
        ]

        ticket_expired_run, ticket_pending, ticket_request = await create_pending(
            "ticket-expired", expires_at=now + timedelta(minutes=20)
        )
        expired_credential = ticket_issuer.issue(
            tenant_id=UUID(TENANT_A), approval_id=ticket_pending.id
        )
        ticket_approval = await approvals.decide(
            context(TENANT_A, OTHER_ACTOR),
            approval_id=ticket_pending.id,
            actor_id=OTHER_ACTOR,
            request=approved_decision,
            expected_version=1,
            idempotency_key="approval-ticket-expired-decision",
            request_hash=approved_hash,
            metadata=METADATA,
            now=now + timedelta(seconds=6),
            ticket_issue=ExecutionTicketIssue(
                credential=expired_credential,
                expires_at=now + timedelta(seconds=7),
            ),
        )
        assert ticket_approval is not None
        expired_tool_request = replace(
            tool_request,
            run_id=ticket_expired_run.id,
            approval_id=ticket_pending.id,
            deployment_id=ticket_expired_run.deployment_id,
            ticket_ref=expired_credential.ticket_ref,
            ticket_nonce=expired_credential.nonce,
            tool_name=ticket_request.tool_name,
            tool_schema_hash=ticket_request.tool_schema_hash,
            parameter_digest=ticket_request.parameter_digest,
            policy_version=ticket_request.policy_version,
        )
        with pytest.raises(ToolExecutionDenied) as expired_ticket:
            await gateway.execute(
                context(TENANT_A),
                request=expired_tool_request,
                metadata=METADATA,
                now=now + timedelta(seconds=8),
            )
        assert expired_ticket.value.code == "EXECUTION_TICKET_EXPIRED"
        expired_approval = await approvals.get_approval(
            context(TENANT_A), approval_id=ticket_pending.id, now=now
        )
        assert expired_approval is not None
        assert expired_approval.status == "EXPIRED"
        async with admin_engine.connect() as connection:
            ticket_expired_state = (
                await connection.execute(
                    text("SELECT status, error_code FROM agent_run WHERE id = :run_id"),
                    {"run_id": ticket_expired_run.id},
                )
            ).one()
        assert tuple(ticket_expired_state) == ("TIMEOUT", "EXECUTION_TICKET_EXPIRED")

        with pytest.raises(DBAPIError):
            async with TenantUnitOfWork(
                session_factory, context(TENANT_A, OTHER_ACTOR)
            ) as unit_of_work:
                await unit_of_work.session.execute(
                    text(
                        "UPDATE approval_request SET tool_name = 'tampered' "
                        "WHERE id = :approval_id"
                    ),
                    {"approval_id": pending.id},
                )
        with pytest.raises(DBAPIError):
            async with TenantUnitOfWork(
                session_factory, context(TENANT_A, OTHER_ACTOR)
            ) as unit_of_work:
                await unit_of_work.session.execute(
                    text(
                        "UPDATE approval_decision SET comment = 'tampered' "
                        "WHERE approval_id = :approval_id"
                    ),
                    {"approval_id": pending.id},
                )

        rejected_run, rejected_pending, _ = await create_pending(
            "rejected", expires_at=now + timedelta(minutes=20)
        )
        rejected_decision = ApprovalDecisionRequest(decision="REJECTED", comment=None)
        rejected = await approvals.decide(
            context(TENANT_A, OTHER_ACTOR),
            approval_id=rejected_pending.id,
            actor_id=OTHER_ACTOR,
            request=rejected_decision,
            expected_version=1,
            idempotency_key="approval-rejected-decision",
            request_hash=canonical_request_hash("approval.decision", rejected_decision),
            metadata=METADATA,
            now=now + timedelta(seconds=4),
        )
        assert rejected is not None and rejected.value is not None
        assert rejected.value.status == "REJECTED"

        expired_run, expired_pending, _ = await create_pending(
            "expired", expires_at=now + timedelta(minutes=1)
        )
        expired = await approvals.expire_due(
            context(TENANT_A), now=now + timedelta(minutes=2), limit=100
        )
        assert [item.id for item in expired] == [expired_pending.id]
        assert expired[0].status == "EXPIRED"

        async with TenantUnitOfWork(
            session_factory, context(TENANT_B), read_only=True
        ) as unit_of_work:
            assert (
                await unit_of_work.session.scalar(
                    select(ApprovalRequestModel).where(
                        ApprovalRequestModel.id == pending.id
                    )
                )
                is None
            )
            assert (
                await unit_of_work.session.scalar(
                    select(ExecutionTicketModel).where(
                        ExecutionTicketModel.approval_id == pending.id
                    )
                )
                is None
            )
            assert (
                await unit_of_work.session.scalar(
                    select(ApprovalDecisionModel).where(
                        ApprovalDecisionModel.approval_id == pending.id
                    )
                )
                is None
            )

        async with admin_engine.connect() as connection:
            run_rows = (
                await connection.execute(
                    text(
                        "SELECT id, status, error_code FROM agent_run "
                        "WHERE id IN (:approved_run_id, :rejected_run_id, "
                        ":expired_run_id)"
                    ),
                    {
                        "approved_run_id": approved_run.id,
                        "rejected_run_id": rejected_run.id,
                        "expired_run_id": expired_run.id,
                    },
                )
            ).all()
            run_states = {row.id: (row.status, row.error_code) for row in run_rows}
            assert run_states[approved_run.id] == ("RUNNING", None)
            assert run_states[rejected_run.id] == ("CANCELLING", None)
            assert run_states[expired_run.id] == (
                "TIMEOUT",
                "APPROVAL_EXPIRED",
            )
            event_rows = (
                await connection.execute(
                    text(
                        "SELECT run_id, event_type, payload_json "
                        "FROM run_event WHERE run_id IN "
                        "(:approved_run_id, :rejected_run_id, :expired_run_id) "
                        "AND event_type IN "
                        "('approval_required', 'approval_resolved') "
                        "ORDER BY run_id, sequence_no"
                    ),
                    {
                        "approved_run_id": approved_run.id,
                        "rejected_run_id": rejected_run.id,
                        "expired_run_id": expired_run.id,
                    },
                )
            ).all()
        by_run: dict[UUID, list[Any]] = {}
        for row in event_rows:
            by_run.setdefault(row.run_id, []).append(row)
        assert [row.event_type for row in by_run[approved_run.id]] == [
            "approval_required",
            "approval_resolved",
        ]
        assert by_run[approved_run.id][1].payload_json["decision"] == "APPROVED"
        assert by_run[rejected_run.id][1].payload_json["decision"] == "REJECTED"
        assert by_run[expired_run.id][1].payload_json["decision"] == "EXPIRED"
        assert by_run[expired_run.id][1].payload_json["decided_by"] == str(UUID(int=0))
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


async def verify_agentscope_approval_runtime_bridge(database_url: str) -> None:
    """Exercise AgentScope control events through durable approval and audit facts."""

    app_url = make_url(database_url).set(username=APP_ROLE, password=APP_PASSWORD)
    app_engine = create_async_engine(app_url)
    admin_engine = create_async_engine(database_url)
    try:
        session_factory = create_session_factory(app_engine)
        agents = SqlAlchemyAgentRegistry(session_factory)
        sessions = SqlAlchemySessionStore(session_factory)
        runs = SqlAlchemyRunStore(session_factory)
        approval_store = SqlAlchemyApprovalStore(session_factory)
        coordinator = ApprovalCoordinator(approval_store)
        ticket_issuer = HmacExecutionTicketIssuer(SecretStr("b" * 32))
        agent_records, _ = await agents.list_agents(
            context(TENANT_A), limit=50, cursor=None, status=None, keyword=None
        )
        agent = next(item for item in agent_records if item.active_deployment_id)
        session_request = SessionCreateRequest.model_validate(
            {
                "agent_id": str(agent.id),
                "title": "AP-E6-007 AgentScope Runtime Bridge",
            }
        )
        session_outcome = await sessions.create_session(
            context(TENANT_A),
            user_id=ACTOR,
            request=session_request,
            metadata_value={},
            idempotency_key="agentscope-bridge-session",
            request_hash=canonical_request_hash("session.create", session_request),
            metadata=METADATA,
        )
        assert session_outcome.value is not None
        run_request = RunCreateRequest.model_validate(
            {
                "session_id": str(session_outcome.value.id),
                "input": {"text": "execute the approved production write"},
            }
        )
        run_outcome = await runs.create_run(
            context(TENANT_A),
            user_id=ACTOR,
            request=run_request,
            idempotency_key="agentscope-bridge-run",
            request_hash=canonical_request_hash("run.create", run_request),
            metadata=METADATA,
        )
        assert run_outcome.value is not None
        run = run_outcome.value
        fencing_token = SecretStr("agentscope-bridge-fencing-token")
        fencing_hash = (
            "sha256:"
            + hashlib.sha256(fencing_token.get_secret_value().encode()).hexdigest()
        )
        await runs.prepare_run(
            context(TENANT_A),
            run_id=run.id,
            workflow_id=f"run/{TENANT_A}/{run.id}",
            execution_attempt=1,
            fencing_token_hash=fencing_hash,
        )
        await runs.mark_run_running(
            context(TENANT_A),
            run_id=run.id,
            execution_attempt=1,
            worker_id="agentscope-runtime-bridge-integration",
            fencing_token_hash=fencing_hash,
        )

        role_id = uuid5(NAMESPACE_URL, f"agentscope-bridge-role/{TENANT_A}/{ACTOR}")
        async with admin_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO role (id, tenant_id, code, name, built_in) "
                    "VALUES (:id, :tenant_id, 'agentscope_bridge_executor', "
                    "'AgentScope Bridge Executor', false)"
                ),
                {"id": role_id, "tenant_id": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO role_permission "
                    "(tenant_id, role_id, resource_type, action) "
                    "VALUES (:tenant_id, :role_id, 'mcp', 'execute')"
                ),
                {"tenant_id": TENANT_A, "role_id": role_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO role_binding "
                    "(tenant_id, subject_type, subject_id, role_id) "
                    "VALUES (:tenant_id, 'user', :subject_id, :role_id)"
                ),
                {
                    "tenant_id": TENANT_A,
                    "subject_id": ACTOR,
                    "role_id": role_id,
                },
            )

        class RuntimeSession:
            def __init__(self) -> None:
                self.state = AgentState(session_id="agentscope-e6-007")
                self.calls = 0

            async def reply_stream(
                self,
                inputs: (
                    Msg
                    | list[Msg]
                    | UserConfirmResultEvent
                    | ExternalExecutionResultEvent
                    | None
                ) = None,
                *,
                yield_final_msg: bool = False,
            ) -> AsyncIterator[AgentEvent | Msg]:
                assert yield_final_msg is False
                self.calls += 1
                tool_call = ToolCallBlock(
                    id="tool-call-agentscope-bridge",
                    name="production.write",
                    input='{"resource":"redacted"}',
                )
                if self.calls == 1:
                    yield ReplyStartEvent(
                        id="bridge-reply-start",
                        session_id=self.state.session_id,
                        reply_id="bridge-reply",
                        name="assistant",
                    )
                    yield ToolCallStartEvent(
                        id="bridge-tool-start",
                        reply_id="bridge-reply",
                        tool_call_id=tool_call.id,
                        tool_call_name=tool_call.name,
                    )
                    yield RequireUserConfirmEvent(
                        reply_id="bridge-reply", tool_calls=[tool_call]
                    )
                    return
                if isinstance(inputs, UserConfirmResultEvent):
                    yield RequireExternalExecutionEvent(
                        reply_id="bridge-reply", tool_calls=[tool_call]
                    )
                    return
                assert isinstance(inputs, ExternalExecutionResultEvent)
                yield ToolResultStartEvent(
                    id="bridge-result-start",
                    reply_id="bridge-reply",
                    tool_call_id=tool_call.id,
                    tool_call_name=tool_call.name,
                )
                yield ToolResultTextDeltaEvent(
                    id="bridge-result-delta",
                    reply_id="bridge-reply",
                    tool_call_id=tool_call.id,
                    delta='{"ok":true}',
                )
                yield ToolResultEndEvent(
                    id="bridge-result-end",
                    reply_id="bridge-reply",
                    tool_call_id=tool_call.id,
                    state=ToolResultState.SUCCESS,
                )
                yield TextBlockDeltaEvent(
                    id="bridge-text-delta",
                    reply_id="bridge-reply",
                    block_id="bridge-text",
                    delta="approved tool completed",
                )
                yield ReplyEndEvent(
                    id="bridge-reply-end",
                    session_id=self.state.session_id,
                    reply_id="bridge-reply",
                    finished_reason=ReplyFinishedReason.COMPLETED,
                )

        runtime_session = RuntimeSession()
        checkpoints: list[bytes] = []
        tool_calls: list[ToolExecutionRequest] = []
        bridge_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        class BridgePorts:
            async def create(
                self, context: TenantContext, *, request: RunExecutionRequest
            ) -> AgentScopeSessionStart:
                return AgentScopeSessionStart(
                    session=runtime_session,
                    initial_input=None,
                )

            async def save(self, context: TenantContext, **kwargs: object) -> str:
                checkpoints.append(cast(bytes, kwargs["state_json"]))
                return f"state://{TENANT_A}/{run.id}/1/checkpoint-1"

            async def resolve(
                self, context: TenantContext, **kwargs: object
            ) -> RuntimeToolBinding:
                return RuntimeToolBinding(
                    tool_call_id="tool-call-agentscope-bridge",
                    requester_id=ACTOR,
                    deployment_id=run.deployment_id,
                    tool_name="production.write",
                    tool_schema_hash="sha256:" + "6" * 64,
                    policy_version="effective-policy/v1",
                    arguments={"resource": "redacted"},
                    risk_level="HIGH",
                    approval_expires_at=bridge_expires_at,
                )

        class ToolExecutor:
            async def execute(
                self, context: TenantContext, *, request: ToolExecutionRequest
            ) -> ToolExecutionResult:
                tool_calls.append(request)
                return ToolExecutionResult(status="SUCCEEDED", output={"ok": True})

        ports = BridgePorts()
        bridge = AgentScopeRuntimeBridge(
            ports,
            ports,
            ports,
            AgentScopeApprovalBridge(coordinator, approval_store),
            ticket_issuer,
            ToolGatewayService(
                SqlAlchemyExecutionTicketStore(session_factory),
                SqlAlchemyToolAuthorizationResolver(session_factory),
                ToolExecutor(),
            ),
            approval_poll_seconds=0.01,
        )
        approval_id = uuid5(
            NAMESPACE_URL,
            f"approval/{TENANT_A}/{run.id}/1/tool-call-agentscope-bridge",
        )

        async def approve() -> None:
            pending = None
            for _ in range(200):
                pending = await approval_store.get_approval(
                    context(TENANT_A, OTHER_ACTOR),
                    approval_id=approval_id,
                    now=datetime.now(UTC),
                )
                if pending is not None:
                    break
                await asyncio.sleep(0.01)
            assert pending is not None
            decision = ApprovalDecisionRequest(
                decision="APPROVED", comment="AP-E6-007 integration approval"
            )
            credential = ticket_issuer.issue(
                tenant_id=UUID(TENANT_A), approval_id=approval_id
            )
            outcome = await approval_store.decide(
                context(TENANT_A, OTHER_ACTOR),
                approval_id=approval_id,
                actor_id=OTHER_ACTOR,
                request=decision,
                expected_version=pending.resource_version,
                idempotency_key="agentscope-bridge-approval",
                request_hash=canonical_request_hash("approval.decision", decision),
                metadata=METADATA,
                now=datetime.now(UTC),
                ticket_issue=ExecutionTicketIssue(
                    credential=credential,
                    expires_at=pending.expires_at,
                ),
            )
            assert outcome is not None and outcome.value is not None

        heartbeats: list[str] = []
        runtime_request = RunExecutionRequest(
            tenant_id=UUID(TENANT_A),
            run_id=run.id,
            execution_attempt=1,
            run_spec=RunSpecReference(
                uri=f"immutable://run-spec/{run.id}/1",
                content_hash="sha256:" + "5" * 64,
                size_bytes=1024,
            ),
            timeout_seconds=600,
            runtime_type="agentscope",
            fencing_token=fencing_token,
            heartbeat=heartbeats.append,
        )
        approval_task = asyncio.create_task(approve())
        completion = await bridge.execute(
            TenantContext(
                tenant_id=TENANT_A,
                subject_type=SubjectType.SERVICE,
                subject_id=str(run.id),
                auth_time=datetime.now(UTC),
                request_id="agentscope-bridge-request",
                trace_id="agentscope-bridge-trace",
            ),
            request=runtime_request,
            event_publisher=SqlAlchemyRuntimeEventCandidatePublisher(
                RunEventIngestionService(SqlAlchemyRunEventStore(session_factory))
            ),
        )
        await approval_task

        assert completion.status == "SUCCEEDED"
        assistant_part = completion.assistant_content_parts[0]
        assert isinstance(assistant_part, AssistantTextPart)
        assert assistant_part.text == "approved tool completed"
        assert len(checkpoints) == 1
        assert b"agentscope-e6-007" in checkpoints[0]
        assert len(tool_calls) == 1
        assert tool_calls[0].requester_id == ACTOR
        assert heartbeats[0] == "waiting_approval"
        assert heartbeats[-1] == "external_execution_completed"

        stored_approval = await approval_store.get_approval(
            context(TENANT_A), approval_id=approval_id, now=datetime.now(UTC)
        )
        stored_ticket = await approval_store.get_ticket_for_approval(
            context(TENANT_A), approval_id=approval_id
        )
        assert stored_approval is not None and stored_approval.status == "CONSUMED"
        assert stored_ticket is not None and stored_ticket.consumed_at is not None
        async with admin_engine.connect() as connection:
            event_types = list(
                (
                    await connection.execute(
                        text(
                            "SELECT event_type FROM run_event WHERE run_id = :run_id "
                            "ORDER BY sequence_no"
                        ),
                        {"run_id": run.id},
                    )
                ).scalars()
            )
            audit_actions = list(
                (
                    await connection.execute(
                        text(
                            "SELECT action FROM audit_log "
                            "WHERE resource_id = :approval_id ORDER BY created_at"
                        ),
                        {"approval_id": approval_id},
                    )
                ).scalars()
            )
        assert event_types == [
            "text_message_start",
            "tool_call_start",
            "approval_required",
            "approval_resolved",
            "tool_call_result",
            "text_delta",
            "text_message_end",
        ]
        assert set(audit_actions) >= {
            "approval.request",
            "approval.approve",
            "ticket.consume",
            "tool.execute",
        }
    finally:
        await app_engine.dispose()
        await admin_engine.dispose()


def test_resource_registry_postgresql_integration() -> None:
    database_url = require_database_url()
    asyncio.run(drop_test_role(database_url))
    migrate(database_url, "base")
    migrate(database_url, "0004_resource_registry")
    asyncio.run(seed_tenant_admin_role(database_url))
    migrate(database_url, "head")
    try:
        asyncio.run(verify_prompt_permission_backfill(database_url))
        asyncio.run(verify_skill_permission_backfill(database_url))
        asyncio.run(verify_mcp_permission_backfill(database_url))
        asyncio.run(verify_model_permission_backfill(database_url))
        asyncio.run(verify_agent_permission_backfill(database_url))
        asyncio.run(verify_session_permission_backfill(database_url))
        asyncio.run(verify_message_permission_backfill(database_url))
        asyncio.run(verify_run_permission_backfill(database_url))
        asyncio.run(verify_approval_permission_backfill(database_url))
        asyncio.run(verify_resource_registry(database_url))
        asyncio.run(verify_skill_supply_chain_publication(database_url))
        asyncio.run(verify_mcp_capability_publication(database_url))
        asyncio.run(verify_model_resources(database_url))
        asyncio.run(verify_agent_draft(database_url))
        asyncio.run(verify_release_request_and_failure_protection(database_url))
        asyncio.run(verify_deployment_activation_history_and_fencing(database_url))
        asyncio.run(verify_session_lifecycle_and_deployment_pinning(database_url))
        asyncio.run(verify_message_history_branching_and_immutability(database_url))
        asyncio.run(verify_run_creation_atomicity_and_guards(database_url))
        asyncio.run(verify_run_event_store_constraints(database_url))
        asyncio.run(verify_run_workflow_attempt_and_message_finalization(database_url))
        asyncio.run(verify_run_cancel_retry_and_recovery_fencing(database_url))
        asyncio.run(verify_approval_control_plane(database_url))
        asyncio.run(verify_agentscope_approval_runtime_bridge(database_url))
        asyncio.run(verify_publication_queries_are_read_only(database_url))
        asyncio.run(
            verify_agent_rollback_creates_new_release_and_deployment(database_url)
        )
        asyncio.run(verify_session_remains_pinned_after_rollback(database_url))
        asyncio.run(verify_run_uses_retired_session_deployment(database_url))
        asyncio.run(verify_epic3_vertical_acceptance(database_url))
        asyncio.run(verify_sandbox_lifecycle_and_fencing(database_url))
    finally:
        asyncio.run(drop_test_role(database_url))
        migrate(database_url, "base")
