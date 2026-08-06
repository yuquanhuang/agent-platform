"""Reference provider aggregation and pagination."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from packages.application.resources import CompositeResourceReferenceReader
from packages.contracts.public import SubjectType, TenantContext
from packages.domain.resources import ResourceReferenceRecord, ResourceType

TARGET = UUID("11111111-1111-4111-8111-111111111111")


class FakeProvider:
    def __init__(self, rows: list[ResourceReferenceRecord]) -> None:
        self.rows = rows

    async def list_references(
        self,
        context: TenantContext,
        *,
        target_type: ResourceType,
        target_id: UUID,
        limit: int,
        after: ResourceReferenceRecord | None,
    ) -> list[ResourceReferenceRecord]:
        assert context.tenant_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        assert target_type == "prompt"
        assert target_id == TARGET
        order = {
            "draft_binding": 0,
            "snapshot": 1,
            "deployment": 2,
            "schedule": 3,
            "session": 4,
        }

        def key(row: ResourceReferenceRecord) -> tuple[int, str, str, str]:
            return (
                order[row.reference_type],
                row.resource_type,
                str(row.resource_id),
                str(row.version_id or ""),
            )

        rows = sorted(self.rows, key=key)
        if after is not None:
            rows = [row for row in rows if key(row) > key(after)]
        return rows[:limit]


def context() -> TenantContext:
    return TenantContext(
        tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        subject_type=SubjectType.USER,
        subject_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        membership_version=1,
        auth_time=datetime(2026, 8, 6, tzinfo=UTC),
        request_id="req-reference-test",
        trace_id="trace-reference-test",
    )


@pytest.mark.asyncio
async def test_reference_query_deduplicates_and_uses_stable_cursor() -> None:
    first = ResourceReferenceRecord(
        resource_type="agent",
        resource_id=UUID("22222222-2222-4222-8222-222222222222"),
        reference_type="draft_binding",
    )
    second = ResourceReferenceRecord(
        resource_type="deployment",
        resource_id=UUID("33333333-3333-4333-8333-333333333333"),
        reference_type="deployment",
    )
    third = ResourceReferenceRecord(
        resource_type="snapshot",
        resource_id=UUID("44444444-4444-4444-8444-444444444444"),
        reference_type="snapshot",
    )
    reader = CompositeResourceReferenceReader(
        (FakeProvider([second, third, first]), FakeProvider([first]))
    )

    page_one, cursor = await reader.list_references(
        context(), target_type="prompt", target_id=TARGET, limit=1, cursor=None
    )
    page_two, cursor_two = await reader.list_references(
        context(), target_type="prompt", target_id=TARGET, limit=1, cursor=cursor
    )
    page_three, final_cursor = await reader.list_references(
        context(), target_type="prompt", target_id=TARGET, limit=1, cursor=cursor_two
    )

    assert page_one == [first]
    assert page_two == [third]
    assert page_three == [second]
    assert final_cursor is None


@pytest.mark.asyncio
async def test_reference_query_rejects_unbounded_limit_and_invalid_cursor() -> None:
    reader = CompositeResourceReferenceReader(())

    with pytest.raises(ValueError, match="between 1 and 200"):
        await reader.list_references(
            context(), target_type="prompt", target_id=TARGET, limit=201, cursor=None
        )
    with pytest.raises(ValueError, match="cursor is invalid"):
        await reader.list_references(
            context(), target_type="prompt", target_id=TARGET, limit=20, cursor="bad"
        )
