"""Frozen Skill route registration and fail-closed composition tests."""

import json
import re

import httpx
import pytest

from apps.api.app import create_app
from packages.infrastructure.public import AppSettings


def test_app_openapi_registers_all_frozen_skill_operations() -> None:
    operation_ids: set[str] = set(
        re.findall(r'"operationId":\s*"([^"]+)"', json.dumps(create_app().openapi()))
    )

    assert {
        "listSkills",
        "createSkill",
        "getSkill",
        "updateSkill",
        "deleteSkill",
        "publishSkill",
        "copySkill",
        "disableSkill",
        "enableSkill",
        "rollbackSkill",
        "listSkillVersions",
        "diffSkillVersions",
        "listSkillReferences",
    } <= operation_ids


@pytest.mark.asyncio
async def test_unconfigured_skill_route_fails_closed() -> None:
    transport = httpx.ASGITransport(app=create_app(AppSettings()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/api/v1/skills")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
