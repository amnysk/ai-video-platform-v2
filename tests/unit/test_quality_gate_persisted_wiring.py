"""検査記録に基づく gate の配線: upload_gate と手動投稿（ADR-0081 / INV-129, 130）。

テストの理由は docs/testing/v3-phase3-quality.md。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from contracts.approval import ApproverKind
from contracts.pipeline import UploadGateRequest
from contracts.quality import QUALITY_GATE_ENFORCE_ENV, QualityReason
from contracts.quality_records import UsageFeature
from contracts.states import ProviderCall
from domain.quality.approval import UploadApprover
from domain.quality.records import CheckSubject
from infrastructure.config import Settings
from infrastructure.db.models import (
    Base,
    QualityCheckRecordRow,
    QualityManualApprovalRow,
    QualityReportRow,
    QualityUsageDeclarationRow,
)
from infrastructure.db.quality_repositories import QualityRecordRepository, SqlQualityRecordWriter
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.quality.service import QualityGateService
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.upload import seed_render_ready
from workers.pipeline.activities import PipelineActivities

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wiring.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class World:
    def __init__(self, factory, store: InMemoryArtifactStore, episode_id: str) -> None:
        self.factory = factory
        self.store = store
        self.episode_id = episode_id
        self.subject: CheckSubject

    def service(self) -> QualityGateService:
        return QualityGateService(session_factory=self.factory, store=self.store, clock=lambda: NOW)

    async def declare(self, bgm: bool = False, map_: bool = False) -> None:
        writer = SqlQualityRecordWriter(self.factory)
        for feature, used in ((UsageFeature.BGM, bgm), (UsageFeature.MAP, map_)):
            await writer.declare_usage(
                self.subject,
                feature,
                used=used,
                declared_by="render",
                declared_at=NOW,
                evidence_ref="evidence-1" if used else None,
            )

    async def count(self, model) -> int:
        async with self.factory() as s:
            return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest_asyncio.fixture
async def world(factory, tmp_path: Path):
    store = InMemoryArtifactStore()
    ep = await seed_render_ready(factory, store, tmp_path)
    w = World(factory, store, ep)
    async with factory() as s:
        current = await QualityRecordRepository(s).current_final_video(ep)
    assert current is not None
    w.subject = CheckSubject(ep, current.final_video_sha256, current.input_hash)
    return w


def _acts(w: World, *, enforce: bool, service: Any) -> PipelineActivities:
    return PipelineActivities(
        session_factory=w.factory,
        paused_env=False,
        uploads_paused_env=False,
        quality_gate_enforce=enforce,
        quality_clock=lambda: NOW,
        quality_service=service,
    )


async def _gate(acts: PipelineActivities, ep: str):
    return await acts.upload_gate(UploadGateRequest(ep))


class ExplodingService:
    calls = 0

    async def evaluate(self, episode_id: str, *, manual: bool):
        type(self).calls += 1
        raise RuntimeError("must not be reached")


# --- upload_gate ---------------------------------------------------------------------------


async def test_on_with_complete_records_the_gate_admits(world: World) -> None:
    await world.declare()
    result = await _gate(_acts(world, enforce=True, service=world.service()), world.episode_id)
    assert result.allowed and result.reason is None


async def test_on_with_a_missing_declaration_the_gate_refuses_with_a_reason(world: World) -> None:
    result = await _gate(_acts(world, enforce=True, service=world.service()), world.episode_id)
    assert not result.allowed
    assert result.reason == f"quality: {QualityReason.REPORT_NEEDS_REVIEW.value}"


async def test_on_a_service_error_is_a_refusal(world: World) -> None:
    ExplodingService.calls = 0
    result = await _gate(_acts(world, enforce=True, service=ExplodingService()), world.episode_id)
    assert not result.allowed
    assert result.reason == f"quality: {QualityReason.REPORT_EVALUATION_ERROR.value}"


async def test_on_existing_refusals_come_before_the_service(world: World) -> None:
    async with world.factory() as s:
        repo = ProviderReservationRepository(s)
        r = await repo.reserve(
            episode_id=world.episode_id,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=f"upload:{world.episode_id}",
            input_hash="x" * 64,
            round=1,
        )
        await s.commit()
        await repo.mark_dispatched(r.id)
        await s.commit()
    ExplodingService.calls = 0
    result = await _gate(_acts(world, enforce=True, service=ExplodingService()), world.episode_id)
    assert not result.allowed and "reservation" in (result.reason or "")
    assert ExplodingService.calls == 0


async def test_off_never_touches_the_service_or_the_quality_tables(world: World) -> None:
    ExplodingService.calls = 0
    result = await _gate(_acts(world, enforce=False, service=ExplodingService()), world.episode_id)
    assert result.allowed and result.reason is None
    assert ExplodingService.calls == 0
    for model in (
        QualityCheckRecordRow,
        QualityUsageDeclarationRow,
        QualityReportRow,
        QualityManualApprovalRow,
    ):
        assert await world.count(model) == 0


async def test_the_decision_is_logged_with_the_video_hash(world: World, caplog) -> None:
    await world.declare()
    import logging

    with caplog.at_level(logging.INFO):
        await _gate(_acts(world, enforce=True, service=world.service()), world.episode_id)
    text = repr([r.__dict__ for r in caplog.records])
    assert world.subject.final_video_sha256 in text and "'path': 'auto'" in text


# --- 手動投稿 ------------------------------------------------------------------------------


class Starter:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start_upload_workflow(self, *, episode_id: str) -> str:
        self.started.append(episode_id)
        return f"episode-{episode_id}-upload"


@pytest_asyncio.fixture
async def api(world: World):
    from apps.api.auth import get_upload_approver
    from apps.api.dependencies import (
        get_quality_service,
        get_session_factory,
        get_settings,
        get_workflow_starter,
    )
    from apps.api.main import create_app

    state: dict[str, Any] = {
        "approver": None,
        "service": world.service(),
        "settings": Settings(_env_file=None, **{QUALITY_GATE_ENFORCE_ENV: True}),  # type: ignore[arg-type]
    }
    starter = Starter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: world.factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    app.dependency_overrides[get_settings] = lambda: state["settings"]
    app.dependency_overrides[get_quality_service] = lambda: state["service"]
    app.dependency_overrides[get_upload_approver] = lambda: state["approver"]
    async with world.factory() as s:
        await EpisodeRepository(s).set_workflow_id(
            world.episode_id, f"episode-{world.episode_id}-upload:run-1"
        )
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter, state


async def _post(client, ep: str):
    return await client.post(f"/episodes/{ep}/upload")


async def test_manual_review_without_an_approver_is_forbidden_and_starts_nothing(
    api, world: World
) -> None:
    client, starter, _state = api
    res = await _post(client, world.episode_id)
    assert res.status_code == 403
    assert res.json()["detail"] == f"quality: {QualityReason.APPROVER_UNKNOWN.value}"
    assert starter.started == []
    assert await world.count(QualityManualApprovalRow) == 0


async def test_manual_review_by_a_delegated_agent_is_forbidden(api, world: World) -> None:
    client, starter, state = api
    state["approver"] = UploadApprover(ApproverKind.AGENT_DELEGATED, "agent-1")
    res = await _post(client, world.episode_id)
    assert res.status_code == 403
    assert res.json()["detail"] == f"quality: {QualityReason.APPROVER_NOT_OWNER.value}"
    assert starter.started == [] and await world.count(QualityManualApprovalRow) == 0


async def test_manual_review_by_the_owner_records_the_approval_then_starts(
    api, world: World
) -> None:
    client, starter, state = api
    state["approver"] = UploadApprover(ApproverKind.OWNER, "owner-1")
    res = await _post(client, world.episode_id)
    assert res.status_code == 202 and starter.started == [world.episode_id]
    async with world.factory() as s:
        (row,) = (await s.execute(select(QualityManualApprovalRow))).scalars().all()
        report = await s.get(QualityReportRow, row.report_id)
    assert (row.approver_kind, row.approver_id) == ("owner", "owner-1")
    assert row.final_video_sha256 == world.subject.final_video_sha256
    assert report is not None and report.verdict == "review"


async def test_when_the_approval_cannot_be_recorded_nothing_is_started(
    api, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, starter, state = api
    state["approver"] = UploadApprover(ApproverKind.OWNER, "owner-1")

    async def boom(self, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    monkeypatch.setattr(QualityRecordRepository, "save_manual_approval", boom)
    res = await _post(client, world.episode_id)
    assert res.status_code == 409 and starter.started == []


async def test_manual_pass_needs_no_approver(api, world: World) -> None:
    client, starter, _state = api
    await world.declare()
    res = await _post(client, world.episode_id)
    assert res.status_code == 202 and starter.started == [world.episode_id]
    assert await world.count(QualityManualApprovalRow) == 0


async def test_off_leaves_the_manual_path_untouched(api, world: World) -> None:
    client, starter, state = api
    state["settings"] = Settings(_env_file=None)  # type: ignore[arg-type]
    state["service"] = ExplodingService()
    ExplodingService.calls = 0
    res = await _post(client, world.episode_id)
    assert res.status_code == 202 and starter.started == [world.episode_id]
    assert ExplodingService.calls == 0


async def test_the_default_dependencies_are_the_pre_existing_path_and_no_approver() -> None:
    from apps.api.auth import Actor, get_upload_approver
    from apps.api.dependencies import get_quality_service
    from contracts.approval import ApproverKind

    assert get_quality_service() is None
    # 認証されていない = 承認者なし。エージェントのトークンは代理承認で、所有者承認ではない
    assert get_upload_approver(None) is None
    owner = get_upload_approver(Actor.OWNER)
    agent = get_upload_approver(Actor.AGENT)
    assert owner is not None and owner.kind is ApproverKind.OWNER
    assert agent is not None and agent.kind is ApproverKind.AGENT_DELEGATED
