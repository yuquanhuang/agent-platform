"""Stable public exports for PostgreSQL infrastructure."""

from packages.infrastructure.database.base import NAMING_CONVENTION, Base
from packages.infrastructure.database.iam import SqlAlchemyIamPersistence
from packages.infrastructure.database.identity import SqlAlchemyIdentityReader
from packages.infrastructure.database.models import (
    AppUserModel,
    AuditLogModel,
    IdempotencyRecordModel,
    OperationRecordModel,
    OutboxEventModel,
    ResourceDefinitionModel,
    ResourceVersionModel,
    RoleBindingModel,
    RoleModel,
    RolePermissionModel,
    TenantMemberModel,
    TenantModel,
)
from packages.infrastructure.database.outbox import (
    SqlAlchemyOutboxStore,
    SqlAlchemyOutboxWriter,
)
from packages.infrastructure.database.resources import SqlAlchemyResourceRegistry
from packages.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from packages.infrastructure.database.tenant import (
    TENANT_SETTING_NAME,
    bind_tenant_context,
)
from packages.infrastructure.database.uow import PlatformUnitOfWork, TenantUnitOfWork

__all__ = [
    "NAMING_CONVENTION",
    "TENANT_SETTING_NAME",
    "AppUserModel",
    "AuditLogModel",
    "Base",
    "IdempotencyRecordModel",
    "OperationRecordModel",
    "OutboxEventModel",
    "PlatformUnitOfWork",
    "ResourceDefinitionModel",
    "ResourceVersionModel",
    "RoleBindingModel",
    "RoleModel",
    "RolePermissionModel",
    "SqlAlchemyIamPersistence",
    "SqlAlchemyIdentityReader",
    "SqlAlchemyOutboxStore",
    "SqlAlchemyOutboxWriter",
    "SqlAlchemyResourceRegistry",
    "TenantMemberModel",
    "TenantModel",
    "TenantUnitOfWork",
    "bind_tenant_context",
    "create_database_engine",
    "create_session_factory",
]
