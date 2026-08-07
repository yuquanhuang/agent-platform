"""Stable public exports for PostgreSQL infrastructure."""

from packages.infrastructure.database.agents import SqlAlchemyAgentRegistry
from packages.infrastructure.database.base import NAMING_CONVENTION, Base
from packages.infrastructure.database.bundles import SqlAlchemyBundleInputReader
from packages.infrastructure.database.deployments import SqlAlchemyDeploymentStore
from packages.infrastructure.database.iam import SqlAlchemyIamPersistence
from packages.infrastructure.database.identity import SqlAlchemyIdentityReader
from packages.infrastructure.database.models import (
    AgentBindingModel,
    AgentDefinitionModel,
    AgentSnapshotModel,
    AgentVersionModel,
    AppUserModel,
    AuditLogModel,
    BudgetReservationModel,
    DeploymentModel,
    IdempotencyRecordModel,
    ModelBindingSnapshotModel,
    ModelRateLimitWindowModel,
    ModelUsageModel,
    OperationRecordModel,
    OutboxEventModel,
    ReleaseModel,
    ResourceDefinitionModel,
    ResourceVersionModel,
    RoleBindingModel,
    RoleModel,
    RolePermissionModel,
    RuntimeBundleModel,
    TenantMemberModel,
    TenantModel,
)
from packages.infrastructure.database.outbox import (
    SqlAlchemyOutboxStore,
    SqlAlchemyOutboxWriter,
)
from packages.infrastructure.database.publishing import (
    SqlAlchemyAgentResourceReferenceProvider,
    SqlAlchemySnapshotCompilationStore,
)
from packages.infrastructure.database.releases import SqlAlchemyReleaseStore
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
    "AgentBindingModel",
    "AgentDefinitionModel",
    "AgentSnapshotModel",
    "AgentVersionModel",
    "AppUserModel",
    "AuditLogModel",
    "Base",
    "BudgetReservationModel",
    "DeploymentModel",
    "IdempotencyRecordModel",
    "ModelBindingSnapshotModel",
    "ModelRateLimitWindowModel",
    "ModelUsageModel",
    "OperationRecordModel",
    "OutboxEventModel",
    "PlatformUnitOfWork",
    "ReleaseModel",
    "ResourceDefinitionModel",
    "ResourceVersionModel",
    "RoleBindingModel",
    "RoleModel",
    "RolePermissionModel",
    "RuntimeBundleModel",
    "SqlAlchemyAgentRegistry",
    "SqlAlchemyAgentResourceReferenceProvider",
    "SqlAlchemyBundleInputReader",
    "SqlAlchemyDeploymentStore",
    "SqlAlchemyIamPersistence",
    "SqlAlchemyIdentityReader",
    "SqlAlchemyOutboxStore",
    "SqlAlchemyOutboxWriter",
    "SqlAlchemyReleaseStore",
    "SqlAlchemyResourceRegistry",
    "SqlAlchemySnapshotCompilationStore",
    "TenantMemberModel",
    "TenantModel",
    "TenantUnitOfWork",
    "bind_tenant_context",
    "create_database_engine",
    "create_session_factory",
]
