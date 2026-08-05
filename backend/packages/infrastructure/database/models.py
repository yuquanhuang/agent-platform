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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
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
