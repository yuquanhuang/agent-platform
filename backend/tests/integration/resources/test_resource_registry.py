"""Real PostgreSQL resource registry, RLS, CAS and idempotency verification."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from packages.application.model_gateway import (
    ModelProviderConnectionTestHandler,
    ProviderAdapterRegistry,
)
from packages.application.outbox import OutboxDispatcher, OutboxEventRouter
from packages.application.public import RequestMetadata, canonical_request_hash
from packages.application.temporal import RuntimeTargetReleaseConfig
from packages.contracts.generated.core_models import (
    AgentCreateRequest,
    AgentUpdateRequest,
    CopyAgentRequest,
)
from packages.contracts.generated.resource_content import (
    ResourceContentModelConfig,
    ResourceContentModelProvider,
    ResourceContentPrompt,
)
from packages.contracts.generated.resources_models import (
    ActionRequest,
    ResourceCopyRequest,
    ResourceCreateRequest,
    ResourcePublishRequest,
    ResourceRollbackRequest,
    ResourceUpdateRequest,
)
from packages.contracts.model_gateway import ModelGatewayRequest
from packages.contracts.public import PlatformError, SubjectType, TenantContext
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
    AgentBindingRecord,
    ReleaseRecord,
    RuntimeBundleRecord,
)
from packages.infrastructure.database.public import (
    AgentSnapshotModel,
    AgentVersionModel,
    DeploymentModel,
    SqlAlchemyAgentRegistry,
    SqlAlchemyAgentResourceReferenceProvider,
    SqlAlchemyBundleInputReader,
    SqlAlchemyDeploymentStore,
    SqlAlchemyOutboxStore,
    SqlAlchemyReleaseStore,
    SqlAlchemyResourceRegistry,
    SqlAlchemySnapshotCompilationStore,
    TenantUnitOfWork,
    create_session_factory,
)
from packages.infrastructure.model_gateway import (
    MappingSecretReferenceResolver,
    SqlAlchemyModelBindingReader,
    SqlAlchemyModelGatewayStore,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "AP_TEST_DATABASE_URL"
APP_ROLE = "agent_platform_resource_test"
APP_PASSWORD = "resource-test-only"
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
ACTOR = UUID("33333333-3333-4333-8333-333333333333")
PERMISSION_TENANT = UUID("44444444-4444-4444-8444-444444444444")
METADATA = RequestMetadata(
    request_id="req-resource-integration", trace_id="trace-resource-integration"
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


def context(tenant_id: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        subject_type=SubjectType.USER,
        subject_id=str(ACTOR),
        membership_version=1,
        auth_time=datetime(2026, 8, 6, tzinfo=UTC),
        request_id=METADATA.request_id,
        trace_id=METADATA.trace_id,
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
            compiler_name="AgentScopeBundleCompiler",
            compiler_version="1.0.0",
            manifest_schema_version="1.0",
            manifest={"schema_version": "1.0"},
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
            compiler_name="AgentScopeBundleCompiler",
            compiler_version="1.0.0",
            manifest_schema_version="1.0",
            manifest={"schema_version": "1.0"},
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
                    "'Resource Owner')"
                ),
                {"actor_id": ACTOR},
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


def test_resource_registry_postgresql_integration() -> None:
    database_url = require_database_url()
    asyncio.run(drop_test_role(database_url))
    migrate(database_url, "base")
    migrate(database_url, "0004_resource_registry")
    asyncio.run(seed_tenant_admin_role(database_url))
    migrate(database_url, "head")
    try:
        asyncio.run(verify_prompt_permission_backfill(database_url))
        asyncio.run(verify_model_permission_backfill(database_url))
        asyncio.run(verify_agent_permission_backfill(database_url))
        asyncio.run(verify_resource_registry(database_url))
        asyncio.run(verify_model_resources(database_url))
        asyncio.run(verify_agent_draft(database_url))
        asyncio.run(verify_release_request_and_failure_protection(database_url))
        asyncio.run(verify_deployment_activation_history_and_fencing(database_url))
        asyncio.run(verify_publication_queries_are_read_only(database_url))
        asyncio.run(
            verify_agent_rollback_creates_new_release_and_deployment(database_url)
        )
    finally:
        asyncio.run(drop_test_role(database_url))
        migrate(database_url, "base")
