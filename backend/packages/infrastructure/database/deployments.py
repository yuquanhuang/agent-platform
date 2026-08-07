"""PostgreSQL Deployment activation and tenant-scoped history reads."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.application.temporal import RuntimeTargetReleaseConfig
from packages.contracts.public import TenantContext, resource_state_conflict
from packages.domain.public import (
    DeploymentRecord,
    DeploymentStatus,
    ReleaseRecord,
    RuntimeBundleRecord,
    deployment_compatibility_hash,
    deployment_id,
    ensure_deployment_transition,
)
from packages.infrastructure.database.models import (
    AgentDefinitionModel,
    AgentSnapshotModel,
    AgentVersionModel,
    AuditLogModel,
    DeploymentModel,
    ReleaseModel,
    RuntimeBundleModel,
)
from packages.infrastructure.database.uow import TenantUnitOfWork

_DIGEST_RE = re.compile(r"^.+@sha256:[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class _ActivationPlan:
    runtime_target_id: str
    bundle: RuntimeBundleRecord
    compatibility_hash: str
    deployment_id: UUID


class SqlAlchemyDeploymentStore:
    """Serialize one Agent's multi-target activation in a single transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_targets: dict[str, RuntimeTargetReleaseConfig],
    ) -> None:
        self._session_factory = session_factory
        self._runtime_targets = dict(runtime_targets)

    async def get_deployment(
        self, context: TenantContext, *, deployment_id: UUID
    ) -> DeploymentRecord | None:
        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            model = await unit_of_work.session.scalar(
                select(DeploymentModel).where(
                    DeploymentModel.tenant_id == UUID(context.tenant_id),
                    DeploymentModel.id == deployment_id,
                )
            )
            return _deployment_record(model) if model is not None else None

    async def activate(
        self,
        context: TenantContext,
        *,
        release: ReleaseRecord,
        bundles: tuple[RuntimeBundleRecord, ...],
    ) -> tuple[UUID, ...]:
        tenant_id = UUID(context.tenant_id)
        if release.tenant_id != tenant_id:
            raise ValueError("Release tenant does not match TenantContext")
        plans = self._activation_plans(release, bundles)

        async with TenantUnitOfWork(self._session_factory, context) as unit_of_work:
            session = unit_of_work.session
            agent = await session.scalar(
                select(AgentDefinitionModel)
                .where(
                    AgentDefinitionModel.tenant_id == tenant_id,
                    AgentDefinitionModel.id == release.agent_id,
                    AgentDefinitionModel.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if agent is None:
                raise resource_state_conflict("Release Agent is unavailable.")
            if agent.status in {"DISABLED", "DELETING", "DELETED"}:
                raise resource_state_conflict(
                    "The Agent cannot activate a Deployment in its current state."
                )

            stored_release = await session.scalar(
                select(ReleaseModel)
                .where(
                    ReleaseModel.tenant_id == tenant_id,
                    ReleaseModel.id == release.id,
                )
                .with_for_update()
            )
            if stored_release is None:
                raise resource_state_conflict("Release is unavailable.")
            self._validate_stored_release(stored_release, release)
            await self._validate_snapshot_agent(session, stored_release)
            await self._validate_stored_bundles(session, plans, stored_release)

            target_ids = tuple(plan.runtime_target_id for plan in plans)
            latest_token_rows = (
                await session.execute(
                    select(
                        DeploymentModel.runtime_target_id,
                        func.max(DeploymentModel.activation_fencing_token),
                    )
                    .where(
                        DeploymentModel.tenant_id == tenant_id,
                        DeploymentModel.agent_id == release.agent_id,
                        DeploymentModel.runtime_target_id.in_(target_ids),
                    )
                    .group_by(DeploymentModel.runtime_target_id)
                )
            ).all()
            latest_tokens = {
                str(row[0]): int(row[1])
                for row in latest_token_rows
                if row[1] is not None
            }
            if any(
                token > stored_release.activation_fencing_token
                for token in latest_tokens.values()
            ):
                raise resource_state_conflict(
                    "A newer Release has fenced this Deployment activation."
                )

            existing_rows = (
                await session.scalars(
                    select(DeploymentModel)
                    .where(
                        DeploymentModel.tenant_id == tenant_id,
                        DeploymentModel.release_id == release.id,
                    )
                    .with_for_update()
                )
            ).all()
            existing_by_target = {row.runtime_target_id: row for row in existing_rows}
            if set(existing_by_target) - set(target_ids):
                raise resource_state_conflict(
                    "Release Deployment targets do not match the frozen Release."
                )

            desired: list[DeploymentModel] = []
            newly_activated: list[DeploymentModel] = []
            now = datetime.now(UTC)
            for plan in plans:
                model = existing_by_target.get(plan.runtime_target_id)
                if model is None:
                    model = DeploymentModel(
                        id=plan.deployment_id,
                        tenant_id=tenant_id,
                        release_id=release.id,
                        agent_id=release.agent_id,
                        snapshot_id=cast(UUID, stored_release.snapshot_id),
                        bundle_id=plan.bundle.id,
                        runtime_target_id=plan.runtime_target_id,
                        status="STAGED",
                        compatibility_hash=plan.compatibility_hash,
                        activation_fencing_token=(
                            stored_release.activation_fencing_token
                        ),
                        created_at=now,
                    )
                    session.add(model)
                else:
                    self._validate_existing(model, plan, stored_release)
                desired.append(model)

            await session.flush()
            desired_ids = {model.id for model in desired}
            serving_rows = (
                await session.scalars(
                    select(DeploymentModel)
                    .where(
                        DeploymentModel.tenant_id == tenant_id,
                        DeploymentModel.agent_id == release.agent_id,
                        DeploymentModel.runtime_target_id.in_(target_ids),
                        DeploymentModel.status.in_(("ACTIVE", "DEGRADED")),
                    )
                    .with_for_update()
                )
            ).all()
            retired_any = False
            for current in serving_rows:
                if current.id in desired_ids:
                    continue
                ensure_deployment_transition(
                    cast(DeploymentStatus, current.status), "RETIRED"
                )
                current.status = "RETIRED"
                current.retired_at = now
                retired_any = True

            # PostgreSQL checks the partial unique index per statement. Flush the
            # retirement updates first while keeping both phases in this transaction.
            if retired_any:
                await session.flush()

            for model in desired:
                if model.status == "STAGED":
                    ensure_deployment_transition("STAGED", "ACTIVE")
                    model.status = "ACTIVE"
                    model.activated_at = now
                    newly_activated.append(model)
                elif model.status not in {"ACTIVE", "DEGRADED"}:
                    raise resource_state_conflict(
                        "A terminal Deployment cannot be activated again."
                    )

            agent.active_deployment_id = desired[0].id
            for model in newly_activated:
                _add_activation_audit(session, context, model)
            await session.flush()
            return tuple(model.id for model in desired)

    def _activation_plans(
        self,
        release: ReleaseRecord,
        bundles: tuple[RuntimeBundleRecord, ...],
    ) -> tuple[_ActivationPlan, ...]:
        if release.snapshot_id is None:
            raise resource_state_conflict("Release Snapshot is unavailable.")
        if release.status not in {"ACTIVATING", "SUCCEEDED"}:
            raise resource_state_conflict(
                "Release is not ready for Deployment activation."
            )
        bundle_by_runtime: dict[str, list[RuntimeBundleRecord]] = {}
        for bundle in bundles:
            if bundle.tenant_id != release.tenant_id:
                raise ValueError("Runtime Bundle tenant does not match Release")
            if bundle.snapshot_id != release.snapshot_id:
                raise resource_state_conflict(
                    "Runtime Bundle does not belong to the Release Snapshot."
                )
            bundle_by_runtime.setdefault(bundle.runtime_type, []).append(bundle)

        plans: list[_ActivationPlan] = []
        for target_id in sorted(release.runtime_targets):
            config = self._runtime_targets.get(target_id)
            if config is None:
                raise resource_state_conflict("Runtime Target is unavailable.")
            if config.image_digest is None or not _DIGEST_RE.fullmatch(
                config.image_digest
            ):
                raise resource_state_conflict(
                    "Runtime image Registry manifest digest is unavailable."
                )
            candidates = bundle_by_runtime.get(config.runtime_type, [])
            if len(candidates) != 1:
                raise resource_state_conflict(
                    "Runtime Target requires exactly one matching Runtime Bundle."
                )
            bundle = candidates[0]
            if (
                bundle.scan_status != "PASSED"
                or not bundle.signature_ref
                or not bundle.sbom_ref
            ):
                raise resource_state_conflict(
                    "Runtime Bundle supply-chain checks are incomplete."
                )
            plans.append(
                _ActivationPlan(
                    runtime_target_id=target_id,
                    bundle=bundle,
                    compatibility_hash=deployment_compatibility_hash(
                        agent_id=release.agent_id,
                        snapshot_id=release.snapshot_id,
                        bundle_id=bundle.id,
                        bundle_content_hash=bundle.content_hash,
                        runtime_target_id=target_id,
                        runtime_type=config.runtime_type,
                        runtime_image_digest=config.image_digest,
                    ),
                    deployment_id=deployment_id(
                        release.tenant_id, release.id, target_id
                    ),
                )
            )
        return tuple(plans)

    @staticmethod
    def _validate_stored_release(stored: ReleaseModel, release: ReleaseRecord) -> None:
        if (
            stored.agent_id != release.agent_id
            or stored.snapshot_id != release.snapshot_id
            or tuple(sorted(stored.runtime_targets_json))
            != tuple(sorted(release.runtime_targets))
            or stored.status not in {"ACTIVATING", "SUCCEEDED"}
        ):
            raise resource_state_conflict(
                "Stored Release does not match the activation request."
            )

    @staticmethod
    async def _validate_snapshot_agent(
        session: AsyncSession, release: ReleaseModel
    ) -> None:
        snapshot_id = cast(UUID, release.snapshot_id)
        valid_snapshot = await session.scalar(
            select(AgentSnapshotModel.id)
            .join(
                AgentVersionModel,
                (AgentVersionModel.tenant_id == AgentSnapshotModel.tenant_id)
                & (AgentVersionModel.id == AgentSnapshotModel.agent_version_id),
            )
            .where(
                AgentSnapshotModel.tenant_id == release.tenant_id,
                AgentSnapshotModel.id == snapshot_id,
                AgentVersionModel.agent_id == release.agent_id,
            )
        )
        if valid_snapshot is None:
            raise resource_state_conflict(
                "Release Snapshot does not belong to the Release Agent."
            )

    @staticmethod
    async def _validate_stored_bundles(
        session: AsyncSession,
        plans: tuple[_ActivationPlan, ...],
        release: ReleaseModel,
    ) -> None:
        bundle_ids = {plan.bundle.id for plan in plans}
        stored_bundles = (
            await session.scalars(
                select(RuntimeBundleModel)
                .where(
                    RuntimeBundleModel.tenant_id == release.tenant_id,
                    RuntimeBundleModel.snapshot_id == release.snapshot_id,
                    RuntimeBundleModel.id.in_(bundle_ids),
                )
                .with_for_update()
            )
        ).all()
        by_id = {bundle.id: bundle for bundle in stored_bundles}
        for plan in plans:
            stored = by_id.get(plan.bundle.id)
            if (
                stored is None
                or stored.runtime_type != plan.bundle.runtime_type
                or stored.content_hash != plan.bundle.content_hash
                or stored.scan_status != "PASSED"
                or not stored.signature_ref
                or not stored.sbom_ref
            ):
                raise resource_state_conflict(
                    "Stored Runtime Bundle is not eligible for activation."
                )

    @staticmethod
    def _validate_existing(
        model: DeploymentModel,
        plan: _ActivationPlan,
        release: ReleaseModel,
    ) -> None:
        if (
            model.id != plan.deployment_id
            or model.agent_id != release.agent_id
            or model.snapshot_id != release.snapshot_id
            or model.bundle_id != plan.bundle.id
            or model.compatibility_hash != plan.compatibility_hash
            or model.activation_fencing_token != release.activation_fencing_token
        ):
            raise resource_state_conflict(
                "Deployment identity was reused with different immutable inputs."
            )


def _deployment_record(model: DeploymentModel) -> DeploymentRecord:
    return DeploymentRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        release_id=model.release_id,
        agent_id=model.agent_id,
        snapshot_id=model.snapshot_id,
        bundle_id=model.bundle_id,
        runtime_target_id=model.runtime_target_id,
        status=cast(DeploymentStatus, model.status),
        compatibility_hash=model.compatibility_hash,
        activation_fencing_token=model.activation_fencing_token,
        created_at=model.created_at,
        activated_at=model.activated_at,
        retired_at=model.retired_at,
    )


def _add_activation_audit(
    session: AsyncSession, context: TenantContext, deployment: DeploymentModel
) -> None:
    change = {
        "release_id": str(deployment.release_id),
        "agent_id": str(deployment.agent_id),
        "snapshot_id": str(deployment.snapshot_id),
        "bundle_id": str(deployment.bundle_id),
        "runtime_target_id": deployment.runtime_target_id,
        "status": deployment.status,
        "activation_fencing_token": deployment.activation_fencing_token,
    }
    encoded = json.dumps(change, sort_keys=True, separators=(",", ":")).encode()
    session.add(
        AuditLogModel(
            tenant_id=deployment.tenant_id,
            actor_type=context.subject_type.value,
            actor_id=UUID(context.subject_id),
            action="deployment.activated",
            resource_type="deployment",
            resource_id=deployment.id,
            result="SUCCESS",
            reason_codes=[],
            change_digest=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
            request_id=context.request_id,
            trace_id=context.trace_id,
            metadata_schema_version=1,
            metadata_json={"changed_fields": sorted(change)},
        )
    )
