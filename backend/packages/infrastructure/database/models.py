"""Initial IAM persistence mappings required by the tenant foundation."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.infrastructure.database.audit_security import (
    audit_change_digest,
    infer_audit_run_id,
    sanitize_audit_metadata,
)
from packages.infrastructure.database.base import Base

UUID_TYPE = PostgreSQLUUID(as_uuid=True)


class TenantModel(Base):
    """Platform tenant root."""

    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("code ~ '^[a-z][a-z0-9_-]{2,63}$'", name="code_format"),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="status",
        ),
        CheckConstraint("resource_version >= 1", name="resource_version"),
        Index("ix_tenant__status", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ACTIVE'")
    )
    quota_policy_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    resource_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AppUserModel(Base):
    """Platform user mapped to an external identity subject."""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint(
            "identity_issuer",
            "external_subject",
            name="uq_app_user__identity_issuer_external_subject",
        ),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED', 'DELETED')", name="status"),
        Index("ix_app_user__status", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    identity_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ACTIVE'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class TenantMemberModel(Base):
    """Authoritative user membership and authorization invalidation version."""

    __tablename__ = "tenant_member"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", name="uq_tenant_member__tenant_id_user_id"
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="status",
        ),
        CheckConstraint("resource_version >= 1", name="resource_version"),
        CheckConstraint("membership_version >= 1", name="membership_version"),
        Index("ix_tenant_member__tenant_id_id", "tenant_id", "id"),
        Index("ix_tenant_member__tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Tenant-local profile fields avoid cross-tenant writes to app_user.
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT(), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ACTIVE'")
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resource_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    membership_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RoleModel(Base):
    """Tenant-scoped role metadata; permissions are added with AP-E0-006."""

    __tablename__ = "role"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_role__tenant_id_code"),
        UniqueConstraint("tenant_id", "id", name="uq_role__tenant_id_id"),
        CheckConstraint("code ~ '^[a-z][a-z0-9_-]{2,63}$'", name="code_format"),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="status",
        ),
        CheckConstraint("resource_version >= 1", name="resource_version"),
        Index("ix_role__tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'TENANT'")
    )
    built_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ACTIVE'")
    )
    resource_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RoleBindingModel(Base):
    """Tenant-scoped binding from a user or service subject to a role."""

    __tablename__ = "role_binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["role.tenant_id", "role.id"],
            name="fk_role_binding__tenant_id_role_id__role",
            ondelete="RESTRICT",
        ),
        CheckConstraint("subject_type IN ('user', 'service')", name="subject_type"),
        Index(
            "ix_role_binding__tenant_id_subject_type_subject_id",
            "tenant_id",
            "subject_type",
            "subject_id",
        ),
        Index(
            "ix_role_binding__tenant_id_resource_type_resource_id",
            "tenant_id",
            "resource_type",
            "resource_id",
        ),
        Index("ix_role_binding__tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    role_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class RolePermissionModel(Base):
    """Tenant-scoped basic resource:action grants for one role."""

    __tablename__ = "role_permission"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["role.tenant_id", "role.id"],
            name="fk_role_permission__tenant_id_role_id__role",
            ondelete="RESTRICT",
        ),
        Index("ix_role_permission__tenant_id_role_id", "tenant_id", "role_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role_id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(64), primary_key=True)
    condition_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    condition_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )


class AuditLogModel(Base):
    """Append-only security and business audit fact."""

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'service')", name="actor_type"),
        CheckConstraint("result IN ('SUCCESS', 'DENIED', 'FAILED')", name="result"),
        CheckConstraint("metadata_schema_version >= 1", name="metadata_schema_version"),
        Index("ix_audit_log__tenant_id_created_at", "tenant_id", "created_at"),
        Index(
            "ix_audit_log__tenant_created_id",
            "tenant_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_audit_log__tenant_run_created_id",
            "tenant_id",
            "run_id",
            "created_at",
            "id",
        ),
        Index("ix_audit_log__actor_type_actor_id", "actor_type", "actor_id"),
        Index(
            "ix_audit_log__resource_type_resource_id",
            "resource_type",
            "resource_id",
        ),
        Index("ix_audit_log__action", "action"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=True,
    )
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    run_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    change_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


@event.listens_for(AuditLogModel, "before_insert")
def _protect_audit_log_before_insert(  # pyright: ignore[reportUnusedFunction]
    _mapper: object, _connection: object, target: AuditLogModel
) -> None:
    original_metadata = target.metadata_json or {}
    if target.run_id is None:
        target.run_id = infer_audit_run_id(
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            metadata=original_metadata,
        )
    target.metadata_json = sanitize_audit_metadata(original_metadata)
    target.change_digest = audit_change_digest(target.metadata_json)


class IdempotencyRecordModel(Base):
    """Durable request fingerprint and replayable response summary."""

    __tablename__ = "idempotency_record"
    __table_args__ = (
        CheckConstraint("status IN ('IN_PROGRESS', 'COMPLETED')", name="status"),
        CheckConstraint("response_status >= 100", name="response_status"),
        Index(
            "ix_idempotency_record__tenant_actor_operation",
            "tenant_id",
            "actor_id",
            "operation_type",
            "idempotency_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=True
    )
    actor_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'IN_PROGRESS'")
    )
    response_status: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("200")
    )
    response_body_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )
    response_etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OperationRecordModel(Base):
    """Queryable terminal or asynchronous operation state."""

    __tablename__ = "operation_record"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACCEPTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="status",
        ),
        Index("ix_operation_record__tenant_id_created_at", "tenant_id", "created_at"),
        Index(
            "ix_operation_record__tenant_id_resource",
            "tenant_id",
            "resource_type",
            "resource_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ModelUsageModel(Base):
    """Immutable tenant-scoped normalized model usage fact."""

    __tablename__ = "model_usage"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="input_tokens"),
        CheckConstraint("output_tokens >= 0", name="output_tokens"),
        CheckConstraint("reasoning_tokens >= 0", name="reasoning_tokens"),
        CheckConstraint("cache_read_tokens >= 0", name="cache_read_tokens"),
        CheckConstraint("cache_write_tokens >= 0", name="cache_write_tokens"),
        CheckConstraint("cost_amount IS NULL OR cost_amount >= 0", name="cost_amount"),
        CheckConstraint(
            "(cost_amount IS NULL) = (cost_currency IS NULL)", name="cost_pair"
        ),
        CheckConstraint("finished_at >= started_at", name="time_order"),
        Index("ix_model_usage__tenant_id_run_id", "tenant_id", "run_id"),
        Index("ix_model_usage__tenant_id_finished_at", "tenant_id", "finished_at"),
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BudgetReservationModel(Base):
    """Conservative Run token reservation guarding concurrent model calls."""

    __tablename__ = "budget_reservation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "idempotency_key",
            name="uq_budget_reservation__tenant_run_idempotency",
        ),
        CheckConstraint(
            "status IN ('RESERVED', 'CONSUMED', 'RELEASED')", name="status"
        ),
        CheckConstraint("reserved_tokens >= 1", name="reserved_tokens"),
        CheckConstraint(
            "consumed_tokens IS NULL OR consumed_tokens >= 0", name="consumed_tokens"
        ),
        CheckConstraint(
            "(status = 'CONSUMED') = (consumed_tokens IS NOT NULL)",
            name="consumed_status",
        ),
        CheckConstraint(
            "(status = 'RESERVED') = (finished_at IS NULL)",
            name="finished_status",
        ),
        Index(
            "ix_budget_reservation__tenant_run_status_expires",
            "tenant_id",
            "run_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model_binding_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ModelRateLimitWindowModel(Base):
    """Tenant-scoped fixed UTC minute counter for a frozen model route."""

    __tablename__ = "model_rate_limit_window"
    __table_args__ = (
        CheckConstraint("request_count >= 1", name="request_count"),
        Index(
            "ix_model_rate_limit_window__window_started_at",
            "window_started_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    model_binding_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AgentDefinitionModel(Base):
    """Tenant-scoped editable Agent Draft metadata."""

    __tablename__ = "agent_definition"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_agent_definition__tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "active_deployment_id"],
            ["deployment.tenant_id", "deployment.id"],
            name="fk_agent_definition__tenant_active_deployment__deployment",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint("runtime_type IN ('agentscope', 'codex')", name="runtime_type"),
        CheckConstraint("visibility IN ('private', 'tenant')", name="visibility"),
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="status",
        ),
        CheckConstraint("resource_version >= 1", name="resource_version"),
        CheckConstraint("jsonb_typeof(tags_json) = 'array'", name="tags_json"),
        Index(
            "uq_agent_definition__tenant_code_active",
            "tenant_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_agent_definition__tenant_status_created_at",
            "tenant_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    runtime_type: Mapped[str] = mapped_column(String(20), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'private'")
    )
    tags_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    default_language: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'zh-CN'")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'DRAFT'")
    )
    active_deployment_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    resource_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_by: Mapped[UUID | None] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


class AgentBindingModel(Base):
    """Tenant-scoped resource binding owned by an Agent Draft."""

    __tablename__ = "agent_binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_agent_binding__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "resource_type IN ('prompt', 'skill', 'mcp', 'model', 'knowledge', "
            "'sandbox', 'agent')",
            name="resource_type",
        ),
        CheckConstraint(
            "version_policy IN ('fixed', 'resolve_on_publish')",
            name="version_policy",
        ),
        CheckConstraint(
            "(version_policy = 'fixed' AND fixed_version_id IS NOT NULL) OR "
            "(version_policy = 'resolve_on_publish' AND fixed_version_id IS NULL)",
            name="version_policy_version",
        ),
        CheckConstraint(
            "binding_role IS NULL OR binding_role IN "
            "('primary', 'fallback_1', 'fallback_2')",
            name="binding_role",
        ),
        CheckConstraint(
            "resource_type = 'model' OR "
            "(binding_role IS NULL AND configuration_json IS NULL AND "
            "configuration_schema_version IS NULL)",
            name="model_routing_fields",
        ),
        CheckConstraint(
            "configuration_json IS NULL OR "
            "(binding_role = 'primary' AND "
            "configuration_schema_version = 'model-routing/v1' AND "
            "jsonb_typeof(configuration_json) = 'object')",
            name="routing_configuration",
        ),
        CheckConstraint(
            "binding_role = 'primary' OR "
            "(configuration_json IS NULL AND configuration_schema_version IS NULL)",
            name="fallback_configuration",
        ),
        Index(
            "uq_agent_binding__agent_resource_role",
            "agent_id",
            "resource_type",
            "resource_id",
            text("coalesce(binding_role, '')"),
            unique=True,
        ),
        Index(
            "uq_agent_binding__agent_model_role",
            "agent_id",
            "binding_role",
            unique=True,
            postgresql_where=text(
                "resource_type = 'model' AND binding_role IS NOT NULL"
            ),
        ),
        Index("ix_agent_binding__tenant_agent", "tenant_id", "agent_id"),
        Index(
            "ix_agent_binding__tenant_resource",
            "tenant_id",
            "resource_type",
            "resource_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    version_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    fixed_version_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    binding_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    configuration_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    configuration_schema_version: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )


class AgentVersionModel(Base):
    """Immutable published version metadata for one Agent Draft."""

    __tablename__ = "agent_version"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_agent_version__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_from_version_id"],
            ["agent_version.tenant_id", "agent_version.id"],
            name="fk_agent_version__tenant_created_from__agent_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agent_version__tenant_id_id"),
        UniqueConstraint(
            "agent_id", "version_no", name="uq_agent_version__agent_id_version_no"
        ),
        CheckConstraint("version_no >= 1", name="version_no"),
        Index(
            "ix_agent_version__tenant_agent_version_no",
            "tenant_id",
            "agent_id",
            "version_no",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_from_version_id: Mapped[UUID | None] = mapped_column(
        UUID_TYPE, nullable=True
    )
    release_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )


class AgentSnapshotModel(Base):
    """Append-only canonical configuration compiled from one Agent Version."""

    __tablename__ = "agent_snapshot"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "agent_version_id"],
            ["agent_version.tenant_id", "agent_version.id"],
            name="fk_agent_snapshot__tenant_version__agent_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agent_snapshot__tenant_id_id"),
        UniqueConstraint(
            "agent_version_id", name="uq_agent_snapshot__agent_version_id"
        ),
        CheckConstraint("content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"),
        CheckConstraint(
            "compiler_input_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="compiler_input_hash",
        ),
        CheckConstraint("jsonb_typeof(content_json) = 'object'", name="content_json"),
        Index("ix_agent_snapshot__tenant_created_at", "tenant_id", "created_at"),
        Index(
            "ix_agent_snapshot__content_json_gin",
            "content_json",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    compiler_input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )


class ReleaseModel(Base):
    """Tenant-scoped asynchronous Agent publication state."""

    __tablename__ = "release"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_release__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_release__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "requested_snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_release__tenant_requested_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_release__tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "agent_id",
            "snapshot_id",
            name="uq_release__tenant_id_agent_snapshot",
        ),
        UniqueConstraint("operation_id", name="uq_release__operation_id"),
        UniqueConstraint("workflow_id", name="uq_release__workflow_id"),
        UniqueConstraint(
            "activation_fencing_token",
            name="uq_release__activation_fencing_token",
        ),
        CheckConstraint(
            "expected_agent_version IS NULL OR expected_agent_version >= 1",
            name="expected_agent_version",
        ),
        CheckConstraint(
            "release_kind IN ('PUBLISH', 'ROLLBACK')",
            name="release_kind",
        ),
        CheckConstraint(
            "(release_kind = 'PUBLISH' AND expected_agent_version IS NOT NULL "
            "AND requested_snapshot_id IS NULL) OR "
            "(release_kind = 'ROLLBACK' AND expected_agent_version IS NULL "
            "AND requested_snapshot_id IS NOT NULL)",
            name="release_source",
        ),
        CheckConstraint(
            "status IN ('REQUESTED', 'VALIDATING', 'COMPILING', 'SCANNING', "
            "'SMOKE_TESTING', 'ACTIVATING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="status",
        ),
        CheckConstraint(
            "jsonb_typeof(runtime_targets_json) = 'array' "
            "AND jsonb_array_length(runtime_targets_json) >= 1",
            name="runtime_targets_json",
        ),
        CheckConstraint(
            "jsonb_typeof(deployment_ids_json) = 'array'",
            name="deployment_ids_json",
        ),
        CheckConstraint(
            "error_detail_json IS NULL OR jsonb_typeof(error_detail_json) = 'object'",
            name="error_detail_json",
        ),
        Index(
            "ix_release__tenant_agent_created_at", "tenant_id", "agent_id", "created_at"
        ),
        Index(
            "ix_release__tenant_status_created_at", "tenant_id", "status", "created_at"
        ),
        Index(
            "ix_release__tenant_requested_snapshot",
            "tenant_id",
            "requested_snapshot_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    requested_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("operation_record.id", ondelete="RESTRICT"),
        nullable=False,
    )
    release_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PUBLISH'")
    )
    expected_agent_version: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    requested_snapshot_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    runtime_targets_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    release_note: Mapped[str] = mapped_column(String(2000), nullable=False)
    run_smoke_test: Mapped[bool] = mapped_column(Boolean, nullable=False)
    activate_on_success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'REQUESTED'")
    )
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    activation_fencing_token: Mapped[int] = mapped_column(
        BigInteger, Identity(), nullable=False
    )
    snapshot_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    deployment_ids_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RuntimeBundleModel(Base):
    """Stored immutable Runtime Bundle plus its security scan result."""

    __tablename__ = "runtime_bundle"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_runtime_bundle__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_runtime_bundle__tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "snapshot_id",
            "id",
            name="uq_runtime_bundle__tenant_snapshot_id",
        ),
        UniqueConstraint(
            "snapshot_id",
            "runtime_type",
            "compiler_version",
            "content_hash",
            name="uq_runtime_bundle__snapshot_runtime_compiler_hash",
        ),
        CheckConstraint("content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"),
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
        CheckConstraint(
            "scan_status IN ('PENDING', 'PASSED', 'FAILED')", name="scan_status"
        ),
        CheckConstraint("jsonb_typeof(manifest_json) = 'object'", name="manifest_json"),
        Index("ix_runtime_bundle__tenant_snapshot", "tenant_id", "snapshot_id"),
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    runtime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_name: Mapped[str] = mapped_column(String(128), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    object_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    signature_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    sbom_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scan_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DeploymentModel(Base):
    """Immutable Deployment identity with atomically switched lifecycle state."""

    __tablename__ = "deployment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "release_id", "agent_id", "snapshot_id"],
            [
                "release.tenant_id",
                "release.id",
                "release.agent_id",
                "release.snapshot_id",
            ],
            name="fk_deployment__tenant_release_agent_snapshot__release",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_deployment__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_deployment__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_id", "bundle_id"],
            [
                "runtime_bundle.tenant_id",
                "runtime_bundle.snapshot_id",
                "runtime_bundle.id",
            ],
            name="fk_deployment__tenant_snapshot_bundle__runtime_bundle",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_deployment__tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "agent_id",
            name="uq_deployment__tenant_id_agent_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "agent_id",
            "snapshot_id",
            name="uq_deployment__tenant_id_agent_snapshot",
        ),
        UniqueConstraint(
            "release_id",
            "runtime_target_id",
            name="uq_deployment__release_runtime_target",
        ),
        CheckConstraint(
            "status IN ('STAGED', 'ACTIVE', 'DEGRADED', 'RETIRED', 'FAILED')",
            name="status",
        ),
        CheckConstraint(
            "compatibility_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="compatibility_hash",
        ),
        CheckConstraint("activation_fencing_token >= 1", name="fencing_token"),
        Index(
            "uq_deployment__tenant_agent_runtime_target_active",
            "tenant_id",
            "agent_id",
            "runtime_target_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "ix_deployment__tenant_agent_created_at",
            "tenant_id",
            "agent_id",
            "created_at",
        ),
        Index(
            "ix_deployment__tenant_runtime_target_status",
            "tenant_id",
            "runtime_target_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    release_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    agent_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    bundle_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    runtime_target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'STAGED'")
    )
    compatibility_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    activation_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ChatSessionModel(Base):
    """User-owned platform Session pinned to one Deployment."""

    __tablename__ = "chat_session"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_chat_session__tenant_user__tenant_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agent_definition.tenant_id", "agent_definition.id"],
            name="fk_chat_session__tenant_agent__agent_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "default_deployment_id", "agent_id"],
            ["deployment.tenant_id", "deployment.id", "deployment.agent_id"],
            name="fk_chat_session__tenant_deployment_agent__deployment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cursor_message_id", "id"],
            ["chat_message.tenant_id", "chat_message.id", "chat_message.session_id"],
            name="fk_chat_session__tenant_cursor_session__chat_message",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint("tenant_id", "id", name="uq_chat_session__tenant_id_id"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED', 'DELETED')", name="status"),
        CheckConstraint("resource_version >= 1", name="resource_version"),
        CheckConstraint("jsonb_typeof(metadata_json) = 'object'", name="metadata_json"),
        CheckConstraint(
            "(status = 'ACTIVE' AND archived_at IS NULL AND deleted_at IS NULL) OR "
            "(status = 'ARCHIVED' AND archived_at IS NOT NULL "
            "AND deleted_at IS NULL) OR "
            "(status = 'DELETED' AND archived_at IS NOT NULL "
            "AND deleted_at IS NOT NULL)",
            name="lifecycle_timestamps",
        ),
        Index(
            "ix_chat_session__tenant_user_updated_at",
            "tenant_id",
            "user_id",
            text("updated_at DESC"),
        ),
        Index(
            "ix_chat_session__tenant_agent_created_at",
            "tenant_id",
            "agent_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    agent_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    default_deployment_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ACTIVE'")
    )
    cursor_message_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    branch_root_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    metadata_schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'session-metadata/v1'")
    )
    resource_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ChatMessageModel(Base):
    """Immutable content node in a Session message chain."""

    __tablename__ = "chat_message"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_session.tenant_id", "chat_session.id"],
            name="fk_chat_message__tenant_session__chat_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_message_id", "session_id"],
            ["chat_message.tenant_id", "chat_message.id", "chat_message.session_id"],
            name="fk_chat_message__tenant_parent_session__chat_message",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_chat_message__tenant_source_run_session__agent_run",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            "session_id",
            name="uq_chat_message__tenant_id_session_id",
        ),
        CheckConstraint("role IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')", name="role"),
        CheckConstraint(
            "jsonb_typeof(content_parts_json) = 'array' "
            "AND jsonb_array_length(content_parts_json) >= 1",
            name="content_parts_json",
        ),
        CheckConstraint(
            "parent_message_id IS NULL OR parent_message_id <> id",
            name="parent_not_self",
        ),
        Index(
            "ix_chat_message__tenant_session_branch_created_at",
            "tenant_id",
            "session_id",
            "branch_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_chat_message__tenant_session_parent",
            "tenant_id",
            "session_id",
            "parent_message_id",
        ),
        Index(
            "uq_chat_message__branch_parent",
            "tenant_id",
            "session_id",
            "branch_id",
            "parent_message_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    branch_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    parent_message_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content_parts_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False
    )
    content_schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'message-content/v1'")
    )
    source_run_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )


class AgentRunModel(Base):
    """Run identity and immutable execution inputs with materialized lifecycle state."""

    __tablename__ = "agent_run"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_session.tenant_id", "chat_session.id"],
            name="fk_agent_run__tenant_session__chat_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_message_id", "session_id"],
            ["chat_message.tenant_id", "chat_message.id", "chat_message.session_id"],
            name="fk_agent_run__tenant_user_message_session__chat_message",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assistant_message_id", "session_id"],
            ["chat_message.tenant_id", "chat_message.id", "chat_message.session_id"],
            name="fk_agent_run__tenant_assistant_message_session__chat_message",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id", "agent_id", "snapshot_id"],
            [
                "deployment.tenant_id",
                "deployment.id",
                "deployment.agent_id",
                "deployment.snapshot_id",
            ],
            name="fk_agent_run__tenant_deployment_agent_snapshot__deployment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            ["agent_snapshot.tenant_id", "agent_snapshot.id"],
            name="fk_agent_run__tenant_snapshot__agent_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_agent_run__tenant_created_by__tenant_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "retry_of_run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_agent_run__tenant_retry_session__agent_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agent_run__tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "session_id",
            name="uq_agent_run__tenant_id_session_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "created_by",
            "idempotency_key",
            name="uq_agent_run__tenant_created_by_idempotency",
        ),
        CheckConstraint(
            "status IN ('CREATED','QUEUED','PREPARING','RUNNING',"
            "'WAITING_APPROVAL','CANCELLING','SUCCEEDED','FAILED','CANCELLED','TIMEOUT')",
            name="status",
        ),
        CheckConstraint(
            "result_quality IS NULL OR result_quality IN "
            "('NORMAL','SUCCEEDED_WITH_WARNINGS')",
            name="result_quality",
        ),
        CheckConstraint("current_attempt >= 0", name="current_attempt"),
        CheckConstraint("latest_sequence_no >= 0", name="latest_sequence_no"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 86400", name="timeout_seconds"),
        CheckConstraint(
            "token_budget IS NULL OR token_budget >= 1", name="token_budget"
        ),
        CheckConstraint(
            "(cost_budget_amount IS NULL AND cost_budget_currency IS NULL) OR "
            "(cost_budget_amount >= 0 AND cost_budget_currency ~ '^[A-Z]{3}$')",
            name="cost_budget",
        ),
        CheckConstraint(
            "error_detail_json IS NULL OR jsonb_typeof(error_detail_json) = 'object'",
            name="error_detail_json",
        ),
        CheckConstraint(
            "(status = 'CREATED' AND queued_at IS NULL AND started_at IS NULL "
            "AND finished_at IS NULL) OR status <> 'CREATED'",
            name="created_timestamps",
        ),
        CheckConstraint(
            "queued_at IS NULL OR queued_at >= created_at", name="queued_at"
        ),
        CheckConstraint(
            "started_at IS NULL OR started_at >= created_at", name="started_at"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at", name="finished_at"
        ),
        CheckConstraint(
            "cancelling_at IS NULL OR cancelling_at >= created_at",
            name="cancelling_at",
        ),
        CheckConstraint(
            "(temporal_run_id IS NULL AND workflow_start_outcome IS NULL "
            "AND workflow_started_at IS NULL) OR "
            "(workflow_id IS NOT NULL AND temporal_run_id IS NOT NULL "
            "AND workflow_start_outcome IS NOT NULL "
            "AND workflow_started_at IS NOT NULL)",
            name="workflow_start_mapping",
        ),
        CheckConstraint(
            "workflow_start_outcome IS NULL OR workflow_start_outcome IN "
            "('STARTED','ALREADY_EXISTS')",
            name="workflow_start_outcome",
        ),
        UniqueConstraint(
            "tenant_id", "workflow_id", name="uq_agent_run__tenant_workflow_id"
        ),
        Index(
            "ix_agent_run__tenant_session_created_at",
            "tenant_id",
            "session_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_agent_run__tenant_status_created_at",
            "tenant_id",
            "status",
            text("created_at DESC"),
        ),
        Index(
            "ix_agent_run__tenant_status_cancelling_at",
            "tenant_id",
            "status",
            "cancelling_at",
        ),
        Index(
            "uq_agent_run__active_session_branch",
            "tenant_id",
            "session_id",
            text(
                "COALESCE(branch_id, " "'00000000-0000-0000-0000-000000000000'::uuid)"
            ),
            unique=True,
            postgresql_where=text(
                "status NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMEOUT')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    branch_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    user_message_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    assistant_message_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    agent_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    snapshot_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'CREATED'")
    )
    result_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    latest_sequence_no: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    client_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retry_of_run_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("600")
    )
    token_budget: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_budget_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True
    )
    cost_budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    temporal_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workflow_start_outcome: Mapped[str | None] = mapped_column(
        String(24), nullable=True
    )
    workflow_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_by: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelling_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunAttemptModel(Base):
    """One fenced execution attempt belonging to a Run."""

    __tablename__ = "run_attempt"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_run_attempt__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_run_attempt__tenant_id_id"),
        UniqueConstraint("run_id", "attempt_no", name="uq_run_attempt__run_attempt"),
        CheckConstraint("attempt_no >= 1", name="attempt_no"),
        CheckConstraint(
            "fencing_token_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="fencing_token_hash",
        ),
        CheckConstraint(
            "status IN ('ALLOCATED','STARTING','RUNNING','COMPLETED','LOST','CANCELLED')",
            name="status",
        ),
        CheckConstraint(
            "heartbeat_at IS NULL OR started_at IS NOT NULL", name="heartbeat_started"
        ),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NOT NULL", name="finished_started"
        ),
        Index("ix_run_attempt__tenant_run_status", "tenant_id", "run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runtime_handle_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ApprovalRequestModel(Base):
    """Durable approval request bound to one immutable tool invocation."""

    __tablename__ = "approval_request"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_approval_request__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "requester_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_approval_request__tenant_requester__tenant_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployment.tenant_id", "deployment.id"],
            name="fk_approval_request__tenant_deployment__deployment",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_approval_request__tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "execution_attempt",
            "tool_call_id",
            name="uq_approval_request__tenant_run_attempt_tool_call",
        ),
        CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        CheckConstraint("length(tool_call_id) >= 1", name="tool_call_id"),
        CheckConstraint("length(tool_name) >= 1", name="tool_name"),
        CheckConstraint(
            "tool_schema_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="tool_schema_hash",
        ),
        CheckConstraint(
            "parameter_digest ~ '^sha256:[a-f0-9]{64}$'",
            name="parameter_digest",
        ),
        CheckConstraint("length(policy_version) >= 1", name="policy_version"),
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED','CONSUMED')",
            name="status",
        ),
        CheckConstraint("expires_at > created_at", name="expires_at"),
        CheckConstraint("resource_version >= 1", name="resource_version"),
        CheckConstraint("updated_at >= created_at", name="updated_at"),
        Index(
            "ix_approval_request__tenant_status_expires_at",
            "tenant_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_approval_request__tenant_run_created_at",
            "tenant_id",
            "run_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    requester_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_schema_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    parameter_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resource_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    self_approval_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ApprovalDecisionModel(Base):
    """Append-only decision for one approval request."""

    __tablename__ = "approval_decision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_request.tenant_id", "approval_request.id"],
            name="fk_approval_decision__tenant_approval__approval_request",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_approval_decision__tenant_actor__tenant_member",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_approval_decision__tenant_id_id"),
        UniqueConstraint("approval_id", name="uq_approval_decision__approval_id"),
        CheckConstraint("decision IN ('APPROVED','REJECTED')", name="decision"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ExecutionTicketModel(Base):
    """One-time authorization bound to an immutable approved tool call."""

    __tablename__ = "execution_ticket"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_request.tenant_id", "approval_request.id"],
            name="fk_execution_ticket__tenant_approval__approval_request",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_execution_ticket__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "requester_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_execution_ticket__tenant_requester__tenant_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployment.tenant_id", "deployment.id"],
            name="fk_execution_ticket__tenant_deployment__deployment",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_execution_ticket__tenant_id_id"),
        UniqueConstraint("approval_id", name="uq_execution_ticket__approval_id"),
        CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        CheckConstraint("length(tool_name) >= 1", name="tool_name"),
        CheckConstraint(
            "tool_schema_hash ~ '^sha256:[a-f0-9]{64}$'", name="tool_schema_hash"
        ),
        CheckConstraint(
            "parameter_digest ~ '^sha256:[a-f0-9]{64}$'", name="parameter_digest"
        ),
        CheckConstraint("length(policy_version) >= 1", name="policy_version"),
        CheckConstraint("nonce_hash ~ '^sha256:[a-f0-9]{64}$'", name="nonce_hash"),
        CheckConstraint("single_use IS TRUE", name="single_use"),
        CheckConstraint("expires_at > created_at", name="expires_at"),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= created_at", name="consumed_at"
        ),
        Index(
            "ix_execution_ticket__tenant_run_expires_at",
            "tenant_id",
            "run_id",
            "expires_at",
        ),
        Index(
            "ix_execution_ticket__tenant_approval",
            "tenant_id",
            "approval_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    approval_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    run_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    requester_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tool_schema_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    parameter_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    nonce_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    single_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WorkspaceModel(Base):
    """Logical Workspace identity and bounded usage counters."""

    __tablename__ = "workspace"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_workspace__tenant_run_session__agent_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_workspace__tenant_user__tenant_member",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_workspace__tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "id",
            "run_id",
            "user_id",
            name="uq_workspace__tenant_id_run_user",
        ),
        UniqueConstraint("tenant_id", "uri", name="uq_workspace__tenant_uri"),
        UniqueConstraint("tenant_id", "run_id", name="uq_workspace__tenant_run"),
        CheckConstraint("quota_bytes > 0", name="quota_bytes"),
        CheckConstraint(
            "used_bytes >= 0 AND used_bytes <= quota_bytes", name="used_bytes"
        ),
        CheckConstraint("max_files > 0", name="max_files"),
        CheckConstraint(
            "file_count >= 0 AND file_count <= max_files", name="file_count"
        ),
        CheckConstraint(
            "max_file_bytes > 0 AND max_file_bytes <= quota_bytes",
            name="max_file_bytes",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','SEALED','QUARANTINED','DELETING','DELETED')",
            name="status",
        ),
        CheckConstraint("expires_at > created_at", name="expires_at"),
        Index(
            "ix_workspace__tenant_status_expires_at",
            "tenant_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_workspace__tenant_session_created_at",
            "tenant_id",
            "session_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    session_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    run_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    uri: Mapped[str] = mapped_column(String(4096), nullable=False)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    max_files: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    max_file_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ACTIVE'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ArtifactModel(Base):
    """User-owned Artifact metadata spanning quarantine and trusted storage."""

    __tablename__ = "artifact"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_artifact__tenant_owner__tenant_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id", "run_id", "owner_user_id"],
            [
                "workspace.tenant_id",
                "workspace.id",
                "workspace.run_id",
                "workspace.user_id",
            ],
            name="fk_artifact__tenant_workspace_run_owner__workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_artifact__tenant_id_id"),
        CheckConstraint(
            "(workspace_id IS NULL AND run_id IS NULL) OR "
            "(workspace_id IS NOT NULL AND run_id IS NOT NULL)",
            name="workspace_run_binding",
        ),
        CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 104857600", name="size_bytes"
        ),
        CheckConstraint("content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"),
        CheckConstraint(
            "status IN ('UPLOADING','SCANNING','AVAILABLE','REJECTED','FAILED',"
            "'EXPIRED','DELETING','DELETED')",
            name="status",
        ),
        CheckConstraint(
            "(status IN ('AVAILABLE','EXPIRED') AND object_uri IS NOT NULL) OR "
            "(status IN ('UPLOADING','SCANNING','REJECTED','FAILED') "
            "AND object_uri IS NULL) OR status IN ('DELETING','DELETED')",
            name="trusted_object_status",
        ),
        CheckConstraint(
            "scan_result_json IS NULL OR jsonb_typeof(scan_result_json) = 'object'",
            name="scan_result_json",
        ),
        CheckConstraint(
            "(status IN ('AVAILABLE','REJECTED','FAILED') "
            "AND scan_result_json IS NOT NULL) OR "
            "status NOT IN ('AVAILABLE','REJECTED','FAILED')",
            name="scan_result_status",
        ),
        CheckConstraint("upload_expires_at > created_at", name="upload_expires_at"),
        CheckConstraint("expires_at > created_at", name="expires_at"),
        CheckConstraint(
            "(status = 'DELETED' AND deleted_at IS NOT NULL) OR "
            "(status <> 'DELETED' AND deleted_at IS NULL)",
            name="deleted_at_status",
        ),
        Index(
            "ix_artifact__tenant_owner_created_at",
            "tenant_id",
            "owner_user_id",
            "created_at",
        ),
        Index(
            "ix_artifact__tenant_run_status",
            "tenant_id",
            "run_id",
            "status",
        ),
        Index(
            "ix_artifact__tenant_status_expires_at", "tenant_id", "status", "expires_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    run_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    owner_user_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    quarantine_object_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    object_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'UPLOADING'")
    )
    required_output: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    scan_result_json: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    upload_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SandboxInstanceModel(Base):
    """Tenant-isolated materialization of one Provider-backed Sandbox."""

    __tablename__ = "sandbox_instance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_sandbox_instance__tenant_run_session__agent_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["tenant_member.tenant_id", "tenant_member.user_id"],
            name="fk_sandbox_instance__tenant_user__tenant_member",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_uri"],
            ["workspace.tenant_id", "workspace.uri"],
            name="fk_sandbox_instance__tenant_workspace__workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_sandbox_instance__tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "execution_attempt",
            "policy_hash",
            name="uq_sandbox_instance__tenant_run_attempt_policy",
        ),
        UniqueConstraint(
            "provision_operation_id",
            name="uq_sandbox_instance__provision_operation_id",
        ),
        CheckConstraint("scope IN ('run','session')", name="scope"),
        CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        CheckConstraint(
            "status IN ('REQUESTED','PROVISIONING','READY','IN_USE','FAILED',"
            "'QUARANTINED','TERMINATING','TERMINATED')",
            name="status",
        ),
        CheckConstraint(
            "image_digest ~ '^[^@[:space:]]+@sha256:[a-f0-9]{64}$'",
            name="image_digest",
        ),
        CheckConstraint("policy_hash ~ '^sha256:[a-f0-9]{64}$'", name="policy_hash"),
        CheckConstraint("bundle_hash ~ '^sha256:[a-f0-9]{64}$'", name="bundle_hash"),
        CheckConstraint("jsonb_typeof(policy_json) = 'object'", name="policy_json"),
        CheckConstraint(
            "terminated_at IS NULL OR status = 'TERMINATED'",
            name="terminated_status",
        ),
        Index(
            "ix_sandbox_instance__tenant_run_attempt",
            "tenant_id",
            "run_id",
            "execution_attempt",
        ),
        Index(
            "ix_sandbox_instance__tenant_status_updated_at",
            "tenant_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    session_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    run_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(2048), nullable=False)
    policy_ref: Mapped[str] = mapped_column(String(2048), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    bundle_ref: Mapped[str] = mapped_column(String(2048), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    workspace_uri: Mapped[str] = mapped_column(String(4096), nullable=False)
    runtime_target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'REQUESTED'")
    )
    provider_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provision_operation_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("operation_record.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    terminated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class SandboxLeaseModel(Base):
    """One immutable fencing grant with an append-only release timestamp."""

    __tablename__ = "sandbox_lease"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "sandbox_id"],
            ["sandbox_instance.tenant_id", "sandbox_instance.id"],
            name="fk_sandbox_lease__tenant_sandbox__sandbox_instance",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "holder_run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_sandbox_lease__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_sandbox_lease__tenant_id_id"),
        CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        CheckConstraint(
            "fencing_token_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="fencing_token_hash",
        ),
        CheckConstraint("expires_at > acquired_at", name="expires_at"),
        CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at",
            name="released_at",
        ),
        Index(
            "uq_sandbox_lease__sandbox_active",
            "sandbox_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index(
            "ix_sandbox_lease__tenant_run_expires_at",
            "tenant_id",
            "holder_run_id",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    sandbox_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    holder_run_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunEventModel(Base):
    """Immutable user-visible execution event fact."""

    __tablename__ = "run_event"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "session_id"],
            ["agent_run.tenant_id", "agent_run.id", "agent_run.session_id"],
            name="fk_run_event__tenant_run_session__agent_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "sequence_no", name="uq_run_event__run_sequence"),
        UniqueConstraint(
            "run_id",
            "execution_attempt",
            "source_event_id",
            name="uq_run_event__run_attempt_source",
        ),
        CheckConstraint("sequence_no >= 1", name="sequence_no"),
        CheckConstraint("execution_attempt >= 1", name="execution_attempt"),
        CheckConstraint("schema_version = '1.0'", name="schema_version"),
        CheckConstraint("payload_version = '1.0'", name="payload_version"),
        CheckConstraint(
            "event_type IN ('run_created','run_queued','run_started',"
            "'text_message_start','text_delta','text_message_end','thinking_delta',"
            "'plan_updated','tool_call_start','tool_call_args','tool_call_result',"
            "'approval_required','approval_resolved','task_progress',"
            "'artifact_created','warning','run_succeeded','run_failed',"
            "'run_cancelled','run_timeout')",
            name="event_type",
        ),
        CheckConstraint("jsonb_typeof(payload_json) = 'object'", name="payload_json"),
        CheckConstraint(
            "octet_length(payload_json::text) <= 262144", name="payload_size"
        ),
        CheckConstraint("length(source_event_id) >= 1", name="source_event_id"),
        CheckConstraint("length(trace_id) >= 3", name="trace_id"),
        Index(
            "ix_run_event__tenant_run_sequence",
            "tenant_id",
            "run_id",
            "sequence_no",
        ),
        Index(
            "ix_run_event__tenant_type_recorded_at",
            "tenant_id",
            "event_type",
            "recorded_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    session_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_version: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)


class RunEventCounterModel(Base):
    """Per-Run sequence allocator state used by the Event Service."""

    __tablename__ = "run_event_counter"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.id"],
            name="fk_run_event_counter__tenant_run__agent_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint("next_sequence_no >= 1", name="next_sequence_no"),
        Index(
            "ix_run_event_counter__tenant_run",
            "tenant_id",
            "run_id",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(UUID_TYPE, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    next_sequence_no: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )


class ResourceDefinitionModel(Base):
    """Tenant-scoped editable resource metadata and current draft."""

    __tablename__ = "resource_definition"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_resource_definition__tenant_id_id"
        ),
        CheckConstraint(
            "resource_type IN ('prompt', 'skill', 'mcp', 'model_provider', "
            "'model_config', 'runtime_target', 'sandbox_profile')",
            name="resource_type",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'DISABLED', 'DELETING', 'DELETED')",
            name="status",
        ),
        CheckConstraint("visibility IN ('private', 'tenant')", name="visibility"),
        CheckConstraint("resource_version >= 1", name="resource_version"),
        Index(
            "uq_resource_definition__tenant_type_code_active",
            "tenant_id",
            "resource_type",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_resource_definition__tenant_type_status_created_at",
            "tenant_id",
            "resource_type",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'DRAFT'")
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'private'")
    )
    current_draft_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    draft_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_by: Mapped[UUID | None] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


class ResourceVersionModel(Base):
    """Immutable published resource content."""

    __tablename__ = "resource_version"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_resource_version__tenant_definition__resource_definition",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "definition_id",
            "version_no",
            name="uq_resource_version__definition_id_version_no",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_resource_version__tenant_id_id"),
        CheckConstraint(
            "publication_kind IN ('PUBLISH', 'ROLLBACK')",
            name="publication_kind",
        ),
        CheckConstraint("version_no >= 1", name="version_no"),
        CheckConstraint("content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"),
        CheckConstraint(
            "source_hash IS NULL OR source_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="source_hash",
        ),
        CheckConstraint("status IN ('PUBLISHED', 'DISABLED')", name="status"),
        Index(
            "ix_resource_version__tenant_definition_published_at",
            "tenant_id",
            "definition_id",
            "published_at",
        ),
        Index(
            "uq_resource_version__definition_content_publish",
            "definition_id",
            "content_hash",
            unique=True,
            postgresql_where=text("publication_kind = 'PUBLISH'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    definition_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    content_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    release_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    publication_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PUBLISH'")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PUBLISHED'")
    )
    source_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    published_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )


class SkillSupplyChainScanModel(Base):
    """Immutable terminal Skill scan evidence bound once to a published version."""

    __tablename__ = "skill_supply_chain_scan"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_skill_supply_chain_scan__tenant_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "published_version_id"],
            ["resource_version.tenant_id", "resource_version.id"],
            name="fk_skill_supply_chain_scan__tenant_published_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_skill_supply_chain_scan__tenant_id_id"
        ),
        UniqueConstraint(
            "published_version_id",
            name="uq_skill_supply_chain_scan__published_version_id",
        ),
        CheckConstraint("draft_resource_version >= 1", name="draft_resource_version"),
        CheckConstraint("status IN ('PASSED','REJECTED','FAILED')", name="status"),
        CheckConstraint("content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"),
        CheckConstraint("report_hash ~ '^sha256:[a-f0-9]{64}$'", name="report_hash"),
        CheckConstraint("sbom_hash ~ '^sha256:[a-f0-9]{64}$'", name="sbom_hash"),
        CheckConstraint(
            "signature_status IN ('VERIFIED','UNVERIFIED','NOT_PROVIDED')",
            name="signature_status",
        ),
        CheckConstraint(
            "provenance_status IN ('VERIFIED','UNVERIFIED','NOT_PROVIDED')",
            name="provenance_status",
        ),
        CheckConstraint("jsonb_typeof(findings_json) = 'array'", name="findings_json"),
        CheckConstraint("jsonb_typeof(sbom_json) = 'object'", name="sbom_json"),
        CheckConstraint(
            "published_version_id IS NULL OR status = 'PASSED'",
            name="published_version_status",
        ),
        Index(
            "ix_skill_supply_chain_scan__tenant_definition_scanned_at",
            "tenant_id",
            "definition_id",
            "scanned_at",
        ),
        Index(
            "ix_skill_supply_chain_scan__tenant_content_hash_status",
            "tenant_id",
            "content_hash",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    definition_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    draft_resource_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    scanner_name: Mapped[str] = mapped_column(String(128), nullable=False)
    scanner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    findings_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    report_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    sbom_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    sbom_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    signature_status: Mapped[str] = mapped_column(String(20), nullable=False)
    provenance_status: Mapped[str] = mapped_column(String(20), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    scanned_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    published_version_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)


class McpCapabilityDiscoveryModel(Base):
    """Immutable MCP capability discovery evidence bound once to a version."""

    __tablename__ = "mcp_capability_discovery"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_mcp_capability_discovery__tenant_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "published_version_id"],
            ["resource_version.tenant_id", "resource_version.id"],
            name="fk_mcp_capability_discovery__tenant_published_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_discovery_id"],
            [
                "mcp_capability_discovery.tenant_id",
                "mcp_capability_discovery.id",
            ],
            name="fk_mcp_capability_discovery__tenant_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "id", name="uq_mcp_capability_discovery__tenant_id_id"
        ),
        UniqueConstraint(
            "operation_id", name="uq_mcp_capability_discovery__operation_id"
        ),
        UniqueConstraint(
            "published_version_id",
            name="uq_mcp_capability_discovery__published_version_id",
        ),
        CheckConstraint("draft_resource_version >= 1", name="draft_resource_version"),
        CheckConstraint("status IN ('PASSED','REJECTED','FAILED')", name="status"),
        CheckConstraint("content_hash ~ '^sha256:[a-f0-9]{64}$'", name="content_hash"),
        CheckConstraint(
            "capability_hash IS NULL OR capability_hash ~ '^sha256:[a-f0-9]{64}$'",
            name="capability_hash",
        ),
        CheckConstraint("jsonb_typeof(findings_json) = 'array'", name="findings_json"),
        CheckConstraint("jsonb_typeof(tools_json) = 'array'", name="tools_json"),
        CheckConstraint(
            "published_version_id IS NULL OR status = 'PASSED'",
            name="published_version_status",
        ),
        Index(
            "ix_mcp_capability_discovery__tenant_definition_discovered_at",
            "tenant_id",
            "definition_id",
            "discovered_at",
        ),
        Index(
            "ix_mcp_capability_discovery__tenant_content_hash_status",
            "tenant_id",
            "content_hash",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    definition_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    draft_resource_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    protocol_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    server_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    server_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tools_json: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    capability_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    findings_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    discovered_by: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    source_discovery_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)
    published_version_id: Mapped[UUID | None] = mapped_column(UUID_TYPE, nullable=True)


class ModelBindingSnapshotModel(Base):
    """Immutable provider binding frozen with a Model Config version."""

    __tablename__ = "model_binding_snapshot"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "model_config_definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_model_binding_snapshot__config_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "model_config_version_id"],
            ["resource_version.tenant_id", "resource_version.id"],
            name="fk_model_binding_snapshot__config_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_definition_id"],
            ["resource_definition.tenant_id", "resource_definition.id"],
            name="fk_model_binding_snapshot__provider_definition",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "model_config_version_id",
            name="uq_model_binding_snapshot__model_config_version_id",
        ),
        CheckConstraint(
            "provider_type IN ('openai', 'qwen', 'deepseek')", name="provider_type"
        ),
        CheckConstraint("provider_timeout_seconds BETWEEN 1 AND 600", name="timeout"),
        CheckConstraint(
            "jsonb_typeof(capabilities_json) = 'array'", name="capabilities_json"
        ),
        CheckConstraint(
            "jsonb_typeof(default_parameters_json) = 'object'",
            name="default_parameters_json",
        ),
        CheckConstraint(
            "max_context_tokens IS NULL OR max_context_tokens >= 1",
            name="max_context_tokens",
        ),
        CheckConstraint(
            "rate_limit_rpm IS NULL OR rate_limit_rpm >= 1", name="rate_limit_rpm"
        ),
        CheckConstraint(
            "snapshot_hash ~ '^sha256:[a-f0-9]{64}$'", name="snapshot_hash"
        ),
        Index(
            "ix_model_binding_snapshot__tenant_model_config_definition",
            "tenant_id",
            "model_config_definition_id",
        ),
        Index(
            "ix_model_binding_snapshot__tenant_provider_definition",
            "tenant_id",
            "provider_definition_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False
    )
    model_config_definition_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    model_config_version_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    provider_definition_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    provider_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    default_parameters_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False
    )
    max_context_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OutboxEventModel(Base):
    """Tenant-scoped transactional message awaiting an external dispatcher."""

    __tablename__ = "outbox_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PUBLISHING', 'PUBLISHED', 'DEAD')",
            name="status",
        ),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint("payload_schema_version >= 1", name="payload_schema_version"),
        Index("ix_outbox_event__status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_outbox_event__tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        UUID_TYPE, primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(UUID_TYPE, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
