# ruff: noqa: F811
"""Phase 3 independent review (Sonnet E): real bearer token -> actor -> approver -> wired gate.

Expected to PASS at 7fd36e4 (it is evidence that the merge wiring of
``get_upload_approver`` -> ``start_upload`` holds end to end; the existing tests override the
approver dependency, so the Depends chain itself was not exercised).
Review finding: docs/architecture/v3-phase3-review.md section "API auth".
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from apps.api.auth import Actor, ApiTokens, get_api_tokens
from apps.api.dependencies import (
    get_quality_service,
    get_session_factory,
    get_settings,
    get_workflow_starter,
)
from apps.api.main import create_app
from contracts.quality import QUALITY_GATE_ENFORCE_ENV
from infrastructure.config import Settings
from infrastructure.db.models import QualityManualApprovalRow
from infrastructure.db.repositories import EpisodeRepository
from tests.unit.test_quality_gate_persisted_wiring import (  # noqa: F401  (fixtures)
    Starter,
    factory,
    world,
)

pytestmark = pytest.mark.real_api_auth

OWNER_TOKEN = "o" * 40
AGENT_TOKEN = "a" * 40


@pytest_asyncio.fixture
async def api(world):  # noqa: F811
    starter = Starter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: world.factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    app.dependency_overrides[get_api_tokens] = lambda: ApiTokens.from_pairs(
        [(Actor.OWNER, OWNER_TOKEN), (Actor.AGENT, AGENT_TOKEN)]
    )
    settings = Settings(_env_file=None, **{QUALITY_GATE_ENFORCE_ENV: True})  # type: ignore[arg-type]
    app.dependency_overrides[get_settings] = lambda: settings
    service = world.service()
    app.dependency_overrides[get_quality_service] = lambda: service
    async with world.factory() as s:
        await EpisodeRepository(s).set_workflow_id(
            world.episode_id, f"episode-{world.episode_id}-upload:run-1"
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter


async def _approvals(world: Any) -> int:
    async with world.factory() as s:
        return (
            await s.execute(select(func.count()).select_from(QualityManualApprovalRow))
        ).scalar_one()


async def test_review_agent_token_cannot_release_a_review_with_the_wired_gate(api, world) -> None:
    client, starter = api  # no usage declarations -> REVIEW
    res = await client.post(
        f"/episodes/{world.episode_id}/upload", headers={"Authorization": f"Bearer {AGENT_TOKEN}"}
    )
    assert res.status_code == 403, res.text
    assert starter.started == [] and await _approvals(world) == 0


async def test_review_no_or_wrong_token_starts_nothing(api, world) -> None:
    client, starter = api
    for headers in (
        {},
        {"Authorization": "Bearer " + "x" * 40},
        {"Authorization": "bearer " + OWNER_TOKEN},
    ):
        res = await client.post(f"/episodes/{world.episode_id}/upload", headers=headers)
        assert res.status_code == 401, (headers, res.text)
    assert starter.started == [] and await _approvals(world) == 0


async def test_review_owner_token_releases_a_review_and_records_an_owner_approval(
    api, world
) -> None:
    client, starter = api
    res = await client.post(
        f"/episodes/{world.episode_id}/upload", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    )
    assert res.status_code == 202, res.text
    assert starter.started == [world.episode_id]
    async with world.factory() as s:
        (row,) = (await s.execute(select(QualityManualApprovalRow))).scalars().all()
    assert row.approver_kind == "owner"
