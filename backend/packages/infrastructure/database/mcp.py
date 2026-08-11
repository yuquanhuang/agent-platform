"""PostgreSQL persistence for immutable MCP capability discovery evidence."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.outbox import PermanentOutboxError
from packages.contracts.generated.resource_content import ResourceContentMcp
from packages.contracts.public import TenantContext
from packages.domain.public import (
    McpCapabilityEvidenceRecord,
    McpCapabilitySnapshotInput,
    McpDiscoveredTool,
    McpDiscoveryResult,
    McpDiscoveryStatus,
    McpDiscoveryTarget,
    McpToolRiskLevel,
    parse_resource_content,
)
from packages.infrastructure.database.models import (
    AuditLogModel,
    McpCapabilityDiscoveryModel,
    OperationRecordModel,
    ResourceVersionModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork


class SqlAlchemyMcpDiscoveryStore:
    """Terminalize Operations and expose only exact immutable MCP evidence."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def mark_running(self, context: TenantContext, operation_id: UUID) -> bool:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            operation = await _locked_operation(
                unit_of_work.session, context, operation_id
            )
            if operation.status == "ACCEPTED":
                operation.status = "RUNNING"
                operation.updated_at = datetime.now(UTC)
                return True
            if operation.status == "RUNNING":
                return True
            evidence = await unit_of_work.session.scalar(
                select(McpCapabilityDiscoveryModel.id).where(
                    McpCapabilityDiscoveryModel.tenant_id == UUID(context.tenant_id),
                    McpCapabilityDiscoveryModel.operation_id == operation_id,
                )
            )
            if evidence is None:
                raise PermanentOutboxError(
                    "terminal MCP discovery Operation has no evidence"
                )
            return False

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
        tenant_id = UUID(context.tenant_id)
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            operation = await _locked_operation(session, context, operation_id)
            existing = await session.scalar(
                select(McpCapabilityDiscoveryModel).where(
                    McpCapabilityDiscoveryModel.tenant_id == tenant_id,
                    McpCapabilityDiscoveryModel.operation_id == operation_id,
                )
            )
            if existing is not None:
                return _evidence_record(existing)
            if operation.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise PermanentOutboxError(
                    "terminal MCP discovery Operation has no evidence"
                )
            if (
                operation.resource_id != target.definition_id
                or operation.actor_id != discovered_by
            ):
                raise PermanentOutboxError(
                    "MCP discovery Operation does not match its event payload"
                )
            _validate_terminal_result(result)
            now = datetime.now(UTC)
            model = McpCapabilityDiscoveryModel(
                id=uuid4(),
                tenant_id=tenant_id,
                definition_id=target.definition_id,
                operation_id=operation_id,
                draft_resource_version=target.draft_resource_version,
                content_hash=target.content_hash,
                status=result.status,
                protocol_version=result.protocol_version,
                server_name=result.server_name,
                server_version=result.server_version,
                tools_json=_tools_json(result.tools),
                capability_hash=result.capability_hash,
                findings_json=cast(
                    list[dict[str, object]], [dict(item) for item in result.findings]
                ),
                discovered_at=now,
                discovered_by=discovered_by,
            )
            session.add(model)
            await session.flush()
            operation.status = "SUCCEEDED" if result.status == "PASSED" else "FAILED"
            operation.result_json = (
                {
                    "discovery_id": str(model.id),
                    "status": result.status,
                    "capability_hash": result.capability_hash,
                    "tool_names": [tool.name for tool in result.tools],
                }
                if result.status == "PASSED"
                else None
            )
            operation.error_json = error
            operation.updated_at = now
            operation.finished_at = now
            await _audit_terminal(
                session,
                context=context,
                model=model,
                operation_status=operation.status,
            )
            return _evidence_record(model)

    async def get_publishable_evidence(
        self,
        context: TenantContext,
        *,
        definition_id: UUID,
        draft_resource_version: int,
        content_hash: str,
        allowed_tools: tuple[str, ...],
    ) -> McpCapabilityEvidenceRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            rows = list(
                (
                    await unit_of_work.session.scalars(
                        select(McpCapabilityDiscoveryModel)
                        .where(
                            McpCapabilityDiscoveryModel.tenant_id
                            == UUID(context.tenant_id),
                            McpCapabilityDiscoveryModel.definition_id == definition_id,
                            McpCapabilityDiscoveryModel.draft_resource_version
                            == draft_resource_version,
                            McpCapabilityDiscoveryModel.content_hash == content_hash,
                            McpCapabilityDiscoveryModel.status == "PASSED",
                            McpCapabilityDiscoveryModel.capability_hash.is_not(None),
                            McpCapabilityDiscoveryModel.published_version_id.is_(None),
                        )
                        .order_by(
                            McpCapabilityDiscoveryModel.discovered_at.desc(),
                            McpCapabilityDiscoveryModel.id.desc(),
                        )
                    )
                ).all()
            )
            model = next(
                (
                    row
                    for row in rows
                    if _allowed_tools_match(row.tools_json, allowed_tools)
                ),
                None,
            )
            return _evidence_record(model) if model is not None else None

    async def get_published_evidence(
        self,
        context: TenantContext,
        *,
        source_version_id: UUID,
        definition_id: UUID,
        content_hash: str,
        allowed_tools: tuple[str, ...],
    ) -> McpCapabilityEvidenceRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(McpCapabilityDiscoveryModel).where(
                    McpCapabilityDiscoveryModel.tenant_id == UUID(context.tenant_id),
                    McpCapabilityDiscoveryModel.definition_id == definition_id,
                    McpCapabilityDiscoveryModel.published_version_id
                    == source_version_id,
                    McpCapabilityDiscoveryModel.content_hash == content_hash,
                    McpCapabilityDiscoveryModel.status == "PASSED",
                    McpCapabilityDiscoveryModel.capability_hash.is_not(None),
                )
            )
            if model is None or not _allowed_tools_match(
                model.tools_json, allowed_tools
            ):
                return None
            return _evidence_record(model)

    async def get_capability_snapshot(
        self,
        context: TenantContext,
        *,
        published_version_id: UUID,
    ) -> McpCapabilitySnapshotInput | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            model = await session.scalar(
                select(McpCapabilityDiscoveryModel).where(
                    McpCapabilityDiscoveryModel.tenant_id == UUID(context.tenant_id),
                    McpCapabilityDiscoveryModel.published_version_id
                    == published_version_id,
                    McpCapabilityDiscoveryModel.status == "PASSED",
                    McpCapabilityDiscoveryModel.capability_hash.is_not(None),
                )
            )
            version = await session.scalar(
                select(ResourceVersionModel).where(
                    ResourceVersionModel.tenant_id == UUID(context.tenant_id),
                    ResourceVersionModel.id == published_version_id,
                )
            )
            if model is None or version is None:
                return None
            content = parse_resource_content(version.content_json)
            if (
                not isinstance(content, ResourceContentMcp)
                or version.definition_id != model.definition_id
                or version.content_hash != model.content_hash
                or model.protocol_version is None
                or model.server_name is None
            ):
                return None
            allowed_tools = tuple(content.allowed_tools or ())
            tools = _tools(model.tools_json)
            if tools is None or not _allowed_tools_match(
                model.tools_json, allowed_tools
            ):
                return None
            return McpCapabilitySnapshotInput(
                id=model.id,
                tenant_id=model.tenant_id,
                definition_id=model.definition_id,
                published_version_id=published_version_id,
                content_hash=model.content_hash,
                capability_hash=cast(str, model.capability_hash),
                protocol_version=model.protocol_version,
                server_name=model.server_name,
                server_version=model.server_version,
                allowed_tools=allowed_tools,
                tools=tools,
            )


async def _locked_operation(
    session: AsyncSession, context: TenantContext, operation_id: UUID
) -> OperationRecordModel:
    operation = await session.scalar(
        select(OperationRecordModel)
        .where(
            OperationRecordModel.tenant_id == UUID(context.tenant_id),
            OperationRecordModel.id == operation_id,
            OperationRecordModel.operation_type == "mcp.discover",
            OperationRecordModel.resource_type == "mcp",
        )
        .with_for_update()
    )
    if operation is None:
        raise PermanentOutboxError("MCP discovery Operation is unavailable")
    return operation


def _validate_terminal_result(result: McpDiscoveryResult) -> None:
    if result.status == "PASSED" and (
        result.capability_hash is None
        or result.protocol_version is None
        or result.server_name is None
    ):
        raise PermanentOutboxError("passed MCP discovery result is incomplete")
    if result.status == "FAILED" and (
        result.capability_hash is not None or result.tools
    ):
        raise PermanentOutboxError("failed MCP discovery result contains capabilities")


def _tools_json(tools: tuple[McpDiscoveredTool, ...]) -> list[dict[str, object]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "schema_hash": tool.schema_hash,
            "risk_level": tool.risk_level,
        }
        for tool in sorted(tools, key=lambda item: item.name)
    ]


def _tools(
    values: list[dict[str, object]],
) -> tuple[McpDiscoveredTool, ...] | None:
    result: list[McpDiscoveredTool] = []
    for value in values:
        name = value.get("name")
        schema_hash = value.get("schema_hash")
        risk_level = value.get("risk_level")
        if (
            not isinstance(name, str)
            or not isinstance(schema_hash, str)
            or risk_level not in {"MEDIUM", "HIGH", "CRITICAL"}
        ):
            return None
        description_value = value.get("description")
        result.append(
            McpDiscoveredTool(
                name=name,
                description=(
                    description_value if isinstance(description_value, str) else None
                ),
                input_schema=cast(JsonValue, value.get("input_schema")),
                output_schema=cast(JsonValue | None, value.get("output_schema")),
                schema_hash=schema_hash,
                risk_level=cast(McpToolRiskLevel, risk_level),
            )
        )
    return tuple(result)


def _allowed_tools_match(
    tools_json: list[dict[str, object]], allowed_tools: tuple[str, ...]
) -> bool:
    names = {name for tool in tools_json if isinstance((name := tool.get("name")), str)}
    return len(names) == len(tools_json) and set(allowed_tools) <= names


def _evidence_record(
    model: McpCapabilityDiscoveryModel,
) -> McpCapabilityEvidenceRecord:
    return McpCapabilityEvidenceRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        definition_id=model.definition_id,
        operation_id=model.operation_id,
        draft_resource_version=model.draft_resource_version,
        content_hash=model.content_hash,
        status=cast(McpDiscoveryStatus, model.status),
        capability_hash=model.capability_hash,
        tool_names=tuple(
            sorted(
                name
                for tool in model.tools_json
                if isinstance((name := tool.get("name")), str)
            )
        ),
        published_version_id=model.published_version_id,
        discovered_at=model.discovered_at,
        discovered_by=model.discovered_by,
    )


async def _audit_terminal(
    session: AsyncSession,
    *,
    context: TenantContext,
    model: McpCapabilityDiscoveryModel,
    operation_status: str,
) -> None:
    reason_codes = [
        code
        for finding in model.findings_json
        if isinstance((code := finding.get("code")), str)
    ]
    summary = {
        "discovery_id": str(model.id),
        "operation_id": str(model.operation_id),
        "draft_resource_version": model.draft_resource_version,
        "content_hash": model.content_hash,
        "status": model.status,
        "capability_hash": model.capability_hash,
        "tool_names": sorted(
            name
            for tool in model.tools_json
            if isinstance((name := tool.get("name")), str)
        ),
        "reason_codes": reason_codes,
    }
    canonical = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            id=uuid4(),
            tenant_id=model.tenant_id,
            actor_type="service",
            actor_id=model.discovered_by,
            action="mcp.capability_discovery.completed",
            resource_type="mcp",
            resource_id=model.definition_id,
            result="SUCCESS" if operation_status == "SUCCEEDED" else "FAILED",
            reason_codes=reason_codes,
            change_digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            request_id=context.request_id,
            trace_id=context.trace_id,
            metadata_schema_version=1,
            metadata_json=summary,
            created_at=model.discovered_at,
        )
    )
