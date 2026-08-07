"""Agent Draft use cases and ResourceBinding policy validation."""

from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from pydantic import BaseModel, JsonValue

from packages.application.metadata import RequestMetadata
from packages.application.resources.hashing import canonical_request_hash
from packages.contracts.generated.core_models import (
    Agent,
    AgentCreateRequest,
    AgentPage,
    AgentUpdateRequest,
    CopyAgentRequest,
    DisableAgentRequest,
    OperationAccepted,
    Reference,
    ReferencePage,
    ResourceBinding,
)
from packages.contracts.public import (
    AuthenticatedPrincipal,
    TenantContext,
    permission_denied,
    resource_not_found,
    validation_error,
)
from packages.domain.public import (
    AgentBindingRecord,
    AgentRecord,
    AgentReferenceRecord,
    MutationOutcome,
    OperationRecord,
    TenantAccess,
    format_etag,
    parse_etag,
)


class AgentAccessResolver(Protocol):
    async def resolve_tenant_access(
        self, principal: AuthenticatedPrincipal, metadata: RequestMetadata
    ) -> TenantAccess: ...


class AgentRegistry(Protocol):
    async def list_agents(
        self,
        context: TenantContext,
        *,
        limit: int,
        cursor: str | None,
        status: str | None,
        keyword: str | None,
    ) -> tuple[list[AgentRecord], str | None]: ...

    async def create_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        request: AgentCreateRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[AgentRecord]: ...

    async def get_agent(
        self, context: TenantContext, *, agent_id: UUID
    ) -> AgentRecord | None: ...

    async def list_agent_references(
        self, context: TenantContext, *, agent_id: UUID
    ) -> list[AgentReferenceRecord] | None: ...

    async def update_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_version: int,
        request: AgentUpdateRequest,
        bindings: list[AgentBindingRecord] | None,
        metadata: RequestMetadata,
    ) -> AgentRecord | None: ...

    async def copy_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        request: CopyAgentRequest,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[AgentRecord] | None: ...

    async def set_agent_disabled(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_version: int,
        request: DisableAgentRequest | None,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[AgentRecord] | None: ...

    async def delete_agent(
        self,
        context: TenantContext,
        *,
        actor_id: UUID,
        agent_id: UUID,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        metadata: RequestMetadata,
    ) -> MutationOutcome[OperationRecord] | None: ...


class AgentManagementService:
    """Authorize Agent Draft operations and enforce binding policy."""

    def __init__(
        self, access_resolver: AgentAccessResolver, registry: AgentRegistry
    ) -> None:
        self._access_resolver = access_resolver
        self._registry = registry

    async def list_agents(
        self,
        principal: AuthenticatedPrincipal,
        *,
        limit: int,
        cursor: str | None,
        status: str | None,
        keyword: str | None,
        metadata: RequestMetadata,
    ) -> AgentPage:
        access = await self._access(principal, "list", metadata)
        records, next_cursor = await self._registry.list_agents(
            access.context,
            limit=limit,
            cursor=cursor,
            status=status,
            keyword=keyword,
        )
        return AgentPage(
            items=[_agent(record) for record in records],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        request: AgentCreateRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Agent, str]:
        access = await self._access(principal, "create", metadata)
        outcome = await self._registry.create_agent(
            access.context,
            actor_id=_actor_id(access.context),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash("agent.create", request),
            metadata=metadata,
        )
        return _versioned_outcome(outcome, Agent, _agent)

    async def get_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        metadata: RequestMetadata,
    ) -> tuple[Agent, str]:
        access = await self._access(principal, "read", metadata)
        record = await self._registry.get_agent(
            access.context, agent_id=_resource_id(agent_id)
        )
        if record is None:
            raise resource_not_found()
        return _agent(record), format_etag(record.resource_version)

    async def list_agent_references(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        metadata: RequestMetadata,
    ) -> ReferencePage:
        access = await self._access(principal, "read", metadata)
        references = await self._registry.list_agent_references(
            access.context, agent_id=_resource_id(agent_id)
        )
        if references is None:
            raise resource_not_found()
        return ReferencePage(
            items=[
                Reference(
                    resource_type=reference.resource_type,
                    resource_id=str(reference.resource_id),
                    reference_type=reference.reference_type,
                )
                for reference in references
            ],
            next_cursor=None,
            has_more=False,
        )

    async def update_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        if_match: str,
        request: AgentUpdateRequest,
        metadata: RequestMetadata,
    ) -> tuple[Agent, str]:
        if not request.model_fields_set:
            raise validation_error("Agent update requires at least one field.")
        for field_name in ("name", "visibility", "tags"):
            if (
                field_name in request.model_fields_set
                and getattr(request, field_name) is None
            ):
                raise validation_error(f"Agent {field_name} cannot be null.")
        access = await self._access(principal, "update", metadata)
        bindings = (
            _normalize_bindings(request.bindings)
            if "bindings" in request.model_fields_set
            else None
        )
        record = await self._registry.update_agent(
            access.context,
            actor_id=_actor_id(access.context),
            agent_id=_resource_id(agent_id),
            expected_version=_expected_version(if_match),
            request=request,
            bindings=bindings,
            metadata=metadata,
        )
        if record is None:
            raise resource_not_found()
        return _agent(record), format_etag(record.resource_version)

    async def copy_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        request: CopyAgentRequest,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Agent, str]:
        access = await self._access(principal, "create", metadata)
        outcome = await self._registry.copy_agent(
            access.context,
            actor_id=_actor_id(access.context),
            agent_id=_resource_id(agent_id),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "agent.copy", request, extra={"agent_id": agent_id}
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Agent, _agent)

    async def disable_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        if_match: str,
        request: DisableAgentRequest | None,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> tuple[Agent, str]:
        access = await self._access(principal, "disable", metadata)
        outcome = await self._registry.set_agent_disabled(
            access.context,
            actor_id=_actor_id(access.context),
            agent_id=_resource_id(agent_id),
            expected_version=_expected_version(if_match),
            request=request,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "agent.disable", request, extra={"agent_id": agent_id}
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        return _versioned_outcome(outcome, Agent, _agent)

    async def delete_agent(
        self,
        principal: AuthenticatedPrincipal,
        *,
        agent_id: str,
        if_match: str,
        idempotency_key: str,
        metadata: RequestMetadata,
    ) -> OperationAccepted:
        access = await self._access(principal, "delete", metadata)
        outcome = await self._registry.delete_agent(
            access.context,
            actor_id=_actor_id(access.context),
            agent_id=_resource_id(agent_id),
            expected_version=_expected_version(if_match),
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                "agent.delete",
                None,
                extra={"agent_id": agent_id, "if_match": if_match},
            ),
            metadata=metadata,
        )
        if outcome is None:
            raise resource_not_found()
        if outcome.replay is not None:
            return OperationAccepted.model_validate(outcome.replay.response_body)
        if outcome.value is None:
            raise RuntimeError("Agent delete outcome is missing its operation")
        return OperationAccepted(
            operation_id=str(outcome.value.id),
            status="ACCEPTED",
            status_url=f"/api/v1/operations/{outcome.value.id}",
        )

    async def _access(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        metadata: RequestMetadata,
    ) -> TenantAccess:
        access = await self._access_resolver.resolve_tenant_access(principal, metadata)
        if not access.allows("agent", action):
            raise permission_denied()
        return access


def _normalize_bindings(
    bindings: list[ResourceBinding] | None,
) -> list[AgentBindingRecord]:
    if bindings is None:
        raise validation_error("Agent bindings cannot be null.")
    normalized = list(bindings)
    model_bindings = [
        binding for binding in normalized if binding.resource_type == "model"
    ]
    if len(model_bindings) == 1 and model_bindings[0].binding_role is None:
        index = normalized.index(model_bindings[0])
        normalized[index] = model_bindings[0].model_copy(
            update={"binding_role": "primary"}
        )
    if len(model_bindings) > 1 and any(
        binding.binding_role is None for binding in model_bindings
    ):
        raise validation_error("Multiple model bindings require explicit roles.")
    if len(model_bindings) > 3:
        raise validation_error(
            "At most one primary and two fallback model bindings are allowed."
        )

    roles = [binding.binding_role for binding in model_bindings]
    if len(model_bindings) == 1 and roles[0] not in {None, "primary"}:
        raise validation_error("A single model binding must be primary.")
    if len(model_bindings) > 1:
        if roles.count("primary") != 1:
            raise validation_error("Model bindings must contain exactly one primary.")
        expected_roles = ["primary", "fallback_1", "fallback_2"][: len(model_bindings)]
        if sorted(cast(list[str], roles)) != sorted(expected_roles):
            raise validation_error("Model fallback roles must be continuous.")
    if "fallback_1" in roles or "fallback_2" in roles:
        primary = next(
            (
                binding
                for binding in model_bindings
                if binding.binding_role == "primary"
            ),
            None,
        )
        if primary is None or primary.configuration is None:
            raise validation_error(
                "Fallback routes require primary routing configuration."
            )

    records: list[AgentBindingRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for binding in normalized:
        resource_id = _resource_id(binding.resource_id)
        version_id = (
            _resource_id(binding.version_id) if binding.version_id is not None else None
        )
        if binding.version_policy == "fixed" and version_id is None:
            raise validation_error("Fixed bindings require version_id.")
        if binding.version_policy == "resolve_on_publish" and version_id is not None:
            raise validation_error("Resolve-on-publish bindings cannot set version_id.")
        if binding.resource_type != "model" and any(
            value is not None
            for value in (
                binding.binding_role,
                binding.configuration_schema_version,
                binding.configuration,
            )
        ):
            raise validation_error("Routing fields are only valid for model bindings.")
        configuration = (
            cast(dict[str, JsonValue], binding.configuration.model_dump(mode="json"))
            if binding.configuration is not None
            else None
        )
        if (configuration is None) != (binding.configuration_schema_version is None):
            raise validation_error(
                "Model routing configuration and schema version must be set together."
            )
        if configuration is not None:
            if binding.binding_role != "primary":
                raise validation_error(
                    "Routing configuration is only valid on primary."
                )
            if binding.configuration_schema_version != "model-routing/v1":
                raise validation_error(
                    "Unsupported model routing configuration version."
                )
            codes = configuration.get("fallback_error_codes")
            if not isinstance(codes, list) or len(codes) != len(set(codes)):
                raise validation_error("Fallback error codes must be unique.")
        key = (binding.resource_type, str(resource_id), binding.binding_role or "")
        if key in seen:
            raise validation_error(
                "Agent bindings must be unique by resource and role."
            )
        seen.add(key)
        records.append(
            AgentBindingRecord(
                resource_type=binding.resource_type,
                resource_id=resource_id,
                version_policy=binding.version_policy,
                version_id=version_id,
                binding_role=binding.binding_role,
                configuration_schema_version=binding.configuration_schema_version,
                configuration=configuration,
            )
        )
    return records


def _agent(record: AgentRecord) -> Agent:
    return Agent(
        id=str(record.id),
        code=record.code,
        name=record.name,
        description=record.description,
        runtime_type=record.runtime_type,
        visibility=record.visibility,
        tags=list(record.tags),
        bindings=[
            ResourceBinding.model_validate(
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
            )
            for binding in record.bindings
        ],
        status=record.status,
        resource_version=record.resource_version,
        active_deployment_id=(
            str(record.active_deployment_id)
            if record.active_deployment_id is not None
            else None
        ),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _versioned_outcome[RecordT, ModelT: BaseModel](
    outcome: MutationOutcome[RecordT],
    model_type: type[ModelT],
    mapper: Callable[[RecordT], ModelT],
) -> tuple[ModelT, str]:
    if outcome.replay is not None:
        if outcome.replay.response_etag is None:
            raise RuntimeError("Agent replay is missing ETag")
        return (
            model_type.model_validate(outcome.replay.response_body),
            outcome.replay.response_etag,
        )
    if outcome.value is None:
        raise RuntimeError("Agent mutation outcome is missing its value")
    version = getattr(outcome.value, "resource_version", None)
    if not isinstance(version, int):
        raise TypeError("Agent mutation outcome is missing resource_version")
    return mapper(outcome.value), format_etag(version)


def _request_hash(
    operation_type: str,
    request: BaseModel | None,
    *,
    extra: dict[str, object] | None = None,
) -> str:
    return canonical_request_hash(operation_type, request, extra=extra)


def _resource_id(value: str | None) -> UUID:
    if value is None:
        raise validation_error("Resource identifier is required.")
    try:
        return UUID(value)
    except ValueError as exc:
        raise validation_error("Resource identifier is invalid.") from exc


def _expected_version(value: str) -> int:
    try:
        return parse_etag(value)
    except ValueError as exc:
        raise validation_error(str(exc)) from exc


def _actor_id(context: TenantContext) -> UUID:
    return _resource_id(context.subject_id)
