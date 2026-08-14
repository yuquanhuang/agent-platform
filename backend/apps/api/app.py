"""FastAPI application factory."""

from fastapi import FastAPI

from apps.api.http import RequestContextMiddleware, install_exception_handlers
from apps.api.routes.agents import create_agent_router
from apps.api.routes.approvals import create_approval_router
from apps.api.routes.artifacts import create_artifact_router
from apps.api.routes.audit import create_audit_router
from apps.api.routes.auth import create_auth_router
from apps.api.routes.budget_policies import create_budget_policy_router
from apps.api.routes.events import InternalServiceIdentityProvider, create_event_router
from apps.api.routes.health import create_health_router
from apps.api.routes.iam import create_iam_router
from apps.api.routes.mcp import create_mcp_router
from apps.api.routes.metrics import create_metrics_router
from apps.api.routes.models import create_model_router
from apps.api.routes.prompts import create_prompt_router
from apps.api.routes.quota_policies import create_quota_policy_router
from apps.api.routes.releases import create_release_router
from apps.api.routes.runs import create_run_router
from apps.api.routes.sessions import create_session_router
from apps.api.routes.skills import create_skill_router
from apps.api.routes.storage_policies import create_storage_policy_router
from packages.application.event_service import (
    RunEventIngestionService,
    RunEventQueryService,
    RunEventStreamService,
)
from packages.application.public import (
    AgentManagementService,
    ApprovalManagementService,
    ArtifactDownloadGatewayService,
    ArtifactManagementService,
    AuditManagementService,
    BudgetPolicyManagementService,
    CurrentIdentityService,
    DeploymentManagementService,
    HealthService,
    IamManagementService,
    IdentityReader,
    McpManagementService,
    MessageHistoryService,
    ModelManagementService,
    PromptManagementService,
    PublicationQueryService,
    QuotaPolicyManagementService,
    ReleaseManagementService,
    RunManagementService,
    SessionManagementService,
    SkillManagementService,
    StoragePolicyManagementService,
)
from packages.contracts.public import IdentityProvider
from packages.infrastructure.auth.public import MockIdentityProvider
from packages.infrastructure.observability import PlatformMetrics
from packages.infrastructure.public import AppSettings, AuthMode, get_settings


def create_app(
    settings: AppSettings | None = None,
    *,
    identity_reader: IdentityReader | None = None,
    identity_provider: IdentityProvider | None = None,
    iam_service: IamManagementService | None = None,
    agent_service: AgentManagementService | None = None,
    model_service: ModelManagementService | None = None,
    prompt_service: PromptManagementService | None = None,
    quota_policy_service: QuotaPolicyManagementService | None = None,
    budget_policy_service: BudgetPolicyManagementService | None = None,
    storage_policy_service: StoragePolicyManagementService | None = None,
    skill_service: SkillManagementService | None = None,
    mcp_service: McpManagementService | None = None,
    release_service: ReleaseManagementService | None = None,
    deployment_service: DeploymentManagementService | None = None,
    publication_query_service: PublicationQueryService | None = None,
    session_service: SessionManagementService | None = None,
    message_history_service: MessageHistoryService | None = None,
    run_service: RunManagementService | None = None,
    run_event_query_service: RunEventQueryService | None = None,
    run_event_stream_service: RunEventStreamService | None = None,
    artifact_service: ArtifactManagementService | None = None,
    artifact_download_gateway: ArtifactDownloadGatewayService | None = None,
    approval_service: ApprovalManagementService | None = None,
    audit_service: AuditManagementService | None = None,
    internal_service_identity_provider: InternalServiceIdentityProvider | None = None,
    event_ingestion_service: RunEventIngestionService | None = None,
    metrics: PlatformMetrics | None = None,
) -> FastAPI:
    """Build the API application without connecting to external dependencies."""

    resolved_settings = settings or get_settings()
    resolved_metrics = metrics or PlatformMetrics()
    application = FastAPI(
        title="Agent Platform API",
        version="0.1.0",
    )
    application.state.metrics = resolved_metrics
    application.add_middleware(RequestContextMiddleware, metrics=resolved_metrics)
    install_exception_handlers(application)
    health_service = HealthService(service_name=resolved_settings.service_name)
    resolved_identity_provider = identity_provider
    if (
        resolved_identity_provider is None
        and resolved_settings.auth_mode is AuthMode.MOCK
    ):
        resolved_identity_provider = MockIdentityProvider(resolved_settings)
    identity_service = (
        CurrentIdentityService(identity_reader) if identity_reader is not None else None
    )
    application.include_router(create_health_router(health_service))
    application.include_router(
        create_auth_router(resolved_identity_provider, identity_service)
    )
    application.include_router(
        create_iam_router(resolved_identity_provider, iam_service)
    )
    application.include_router(
        create_quota_policy_router(resolved_identity_provider, quota_policy_service)
    )
    application.include_router(
        create_budget_policy_router(resolved_identity_provider, budget_policy_service)
    )
    application.include_router(
        create_storage_policy_router(resolved_identity_provider, storage_policy_service)
    )
    application.include_router(
        create_agent_router(resolved_identity_provider, agent_service)
    )
    application.include_router(
        create_prompt_router(resolved_identity_provider, prompt_service)
    )
    application.include_router(
        create_skill_router(resolved_identity_provider, skill_service)
    )
    application.include_router(
        create_mcp_router(resolved_identity_provider, mcp_service)
    )
    application.include_router(
        create_model_router(resolved_identity_provider, model_service)
    )
    application.include_router(
        create_release_router(
            resolved_identity_provider,
            release_service,
            deployment_service,
            publication_query_service,
        )
    )
    application.include_router(
        create_session_router(
            resolved_identity_provider, session_service, message_history_service
        )
    )
    application.include_router(
        create_run_router(
            resolved_identity_provider,
            run_service,
            run_event_query_service,
            run_event_stream_service,
            resolved_settings.sse_send_timeout_seconds,
            resolved_metrics,
        )
    )
    application.include_router(
        create_artifact_router(
            resolved_identity_provider,
            artifact_service,
            artifact_download_gateway,
        )
    )
    application.include_router(
        create_approval_router(resolved_identity_provider, approval_service)
    )
    application.include_router(
        create_audit_router(resolved_identity_provider, audit_service)
    )
    application.include_router(
        create_event_router(
            internal_service_identity_provider,
            event_ingestion_service,
        )
    )
    application.include_router(
        create_metrics_router(
            resolved_metrics,
            resolved_settings.metrics_allowed_networks,
        )
    )
    return application


app = create_app()
