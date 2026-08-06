"""Persistence-neutral identity snapshots."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class MembershipSnapshot:
    tenant_id: str
    tenant_name: str
    status: Literal["ACTIVE", "DISABLED"]
    role_ids: tuple[str, ...]
    membership_version: int


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    user_id: str
    external_subject: str
    display_name: str
    auth_time: datetime
    memberships: tuple[MembershipSnapshot, ...]
