"""Capacity Domain scheduling policy tests without a database dependency."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.infrastructure.database import runs as run_store_module
from packages.infrastructure.database.models import RunAdmissionQueueModel
from packages.infrastructure.database.runs import SqlAlchemyRunStore

NOW = datetime(2026, 8, 13, 8, tzinfo=UTC)
TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")


def queue(
    tenant_id: UUID, run_id: UUID, *, queued_seconds: int
) -> RunAdmissionQueueModel:
    return RunAdmissionQueueModel(
        id=run_id,
        tenant_id=tenant_id,
        run_id=run_id,
        priority="NORMAL",
        capacity_domain="rt_agentscope_default",
        status="WAITING",
        queued_at=NOW + timedelta(seconds=queued_seconds),
        deadline_at=NOW + timedelta(minutes=5),
        admitted_at=None,
        cancelled_at=None,
        quota_policy_version_id=None,
        capacity_snapshot_json=None,
        resource_version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def test_fair_candidates_apply_tenant_quantum_before_backlog_repeats() -> None:
    a1 = queue(TENANT_A, UUID(int=1), queued_seconds=0)
    a2 = queue(TENANT_A, UUID(int=2), queued_seconds=1)
    a3 = queue(TENANT_A, UUID(int=3), queued_seconds=2)
    b1 = queue(TENANT_B, UUID(int=4), queued_seconds=3)

    ordered = run_store_module._fair_capacity_candidates(  # pyright: ignore[reportPrivateUsage]
        [a1, a2, a3, b1], last_admitted={}, tenant_quantum=1
    )

    assert [item.run_id for item in ordered] == [
        a1.run_id,
        b1.run_id,
        a2.run_id,
        a3.run_id,
    ]


def test_recently_admitted_tenant_yields_to_older_domain_peer() -> None:
    a1 = queue(TENANT_A, UUID(int=1), queued_seconds=0)
    b1 = queue(TENANT_B, UUID(int=2), queued_seconds=1)

    ordered = run_store_module._fair_capacity_candidates(  # pyright: ignore[reportPrivateUsage]
        [a1, b1],
        last_admitted={
            ("rt_agentscope_default", TENANT_A): NOW,
            ("rt_agentscope_default", TENANT_B): NOW - timedelta(minutes=1),
        },
        tenant_quantum=1,
    )

    assert [item.tenant_id for item in ordered] == [TENANT_B, TENANT_A]


def test_capacity_configuration_hash_is_stable_and_slot_sensitive() -> None:
    assert run_store_module._capacity_config_hash(  # pyright: ignore[reportPrivateUsage]
        "rt_agentscope_default", 10
    ) == run_store_module._capacity_config_hash(  # pyright: ignore[reportPrivateUsage]
        "rt_agentscope_default", 10
    )
    assert run_store_module._capacity_config_hash(  # pyright: ignore[reportPrivateUsage]
        "rt_agentscope_default", 10
    ) != run_store_module._capacity_config_hash(  # pyright: ignore[reportPrivateUsage]
        "rt_agentscope_default", 11
    )


def test_capacity_candidate_query_ranks_each_tenant_before_global_limit() -> None:
    ranked = run_store_module._ranked_capacity_candidates(  # pyright: ignore[reportPrivateUsage]
        now=NOW,
        capacity_domains=("rt_agentscope_default",),
    )

    sql = str(
        ranked.select().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "row_number() OVER" in sql
    assert (
        "PARTITION BY run_admission_queue.capacity_domain, run_admission_queue.tenant_id"
        in sql
    )
    assert "LIMIT" not in sql
    assert "run_admission_queue.priority = 'HIGH' DESC" in sql


def test_store_rejects_unsafe_lease_and_fairness_settings() -> None:
    factory = cast(async_sessionmaker[AsyncSession], object())

    try:
        SqlAlchemyRunStore(factory, capacity_lease_ttl=timedelta(seconds=29))
    except ValueError as error:
        assert "lease TTL" in str(error)
    else:
        raise AssertionError("unsafe lease TTL was accepted")

    try:
        SqlAlchemyRunStore(factory, capacity_tenant_quantum=101)
    except ValueError as error:
        assert "tenant quantum" in str(error)
    else:
        raise AssertionError("unsafe tenant quantum was accepted")
