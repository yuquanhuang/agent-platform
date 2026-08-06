"""Initial IAM persistence mappings required by the tenant foundation."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

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
