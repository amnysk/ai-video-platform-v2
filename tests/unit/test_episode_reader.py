"""StoreBackedEpisodeReader（SQLite + 記憶上の ArtifactStore）。

理由は docs/testing/jpmas2-p2a-service-rationale.md。OpenSearch・Temporal には触れない。
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from application.errors import OperationError, OperationErrorCode
from contracts.agentic import QualityReview
from contracts.agentic_pipeline import PinWorkflowPlanRequest, PipelineKind
from contracts.intervention import ActorType, ChangeKind, ChangeSetStatus
from contracts.pipeline import pipeline_workflow_id
from contracts.states import ArtifactType, EpisodeStatus, FailureClass, JobType
from domain.job.transitions import JobEvent
from infrastructure.agents.episode_artifacts import write_episode_artifact
from infrastructure.application.episode_reader import StoreBackedEpisodeReader
from infrastructure.db.models import EpisodeRow
from infrastructure.db.repositories import ArtifactMetadataRepository, JobRepository
from infrastructure.intervention.store import Actor, InterventionStore
from tests.support.intervention import make_episode, seed_script_and_storyboard, store_artifact
from workers.pipeline.agentic_activities import AgenticPipelineActivities

BUCKET = "artifacts"
PIN = PinWorkflowPlanRequest(episode_id="", template_id="shorts_agentic", template_version="1")


@pytest.fixture
def reader(session_factory, artifact_store) -> StoreBackedEpisodeReader:
    return StoreBackedEpisodeReader(session_factory, lambda: artifact_store)


async def new_episode(session_factory, status: EpisodeStatus = EpisodeStatus.PLANNED) -> uuid.UUID:
    async with session_factory() as s:
        return uuid.UUID(await make_episode(s, status))


async def pin_agentic(session_factory, artifact_store, episode_id: uuid.UUID) -> None:
    acts = AgenticPipelineActivities(
        session_factory=session_factory, store=artifact_store, bucket=BUCKET
    )
    await acts.pin_workflow_plan(dataclasses.replace(PIN, episode_id=str(episode_id)))


async def add_change_set(
    session_factory, episode_id: uuid.UUID, base_revision_id: str, **over: Any
):
    async with session_factory() as s:
        cs = await InterventionStore(s).insert_change_set(
            episode_id=episode_id,
            kind=ChangeKind.SCENE_OVERRIDE_VISUAL,
            base_revision_id=base_revision_id,
            proposal={"scene_id": "sb1"},
            actor=Actor(ActorType.AI_ASSISTANT, "ai"),
            proposed_by_command_id=over.pop("command_id", f"cmd-{uuid.uuid4().hex[:12]}"),
            **over,
        )
        await s.commit()
        return cs


# ---------------------------------------------------------------------------- 存在・種別


async def test_a_missing_episode_is_episode_not_found_for_every_read(reader) -> None:
    missing = uuid.uuid4()
    for call in (
        reader.pipeline_kind,
        reader.workspace,
        reader.storyboard,
        reader.agent_decisions,
        reader.quality_review,
        reader.progress,
    ):
        with pytest.raises(OperationError) as err:
            await call(missing)
        assert err.value.code is OperationErrorCode.EPISODE_NOT_FOUND, call
    with pytest.raises(OperationError) as err:
        await reader.change_impact(missing, str(uuid.uuid4()))
    assert err.value.code is OperationErrorCode.EPISODE_NOT_FOUND


async def test_pipeline_kind_follows_the_pinned_plan(reader, session_factory, artifact_store):
    legacy = await new_episode(session_factory)
    agentic = await new_episode(session_factory)
    await pin_agentic(session_factory, artifact_store, agentic)
    assert await reader.pipeline_kind(legacy) is PipelineKind.LEGACY
    assert await reader.pipeline_kind(agentic) is PipelineKind.AGENTIC


# ---------------------------------------------------------------------------- 一覧


async def test_list_is_newest_first_with_a_keyset_cursor_and_kinds(
    reader, session_factory, artifact_store
) -> None:
    ids = [await new_episode(session_factory) for _ in range(3)]
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with session_factory() as s:
        for n, ep in enumerate(ids):
            row = await s.get(EpisodeRow, ep)
            assert row is not None
            row.created_at = base + timedelta(hours=n)
        await s.commit()
    await pin_agentic(session_factory, artifact_store, ids[1])

    first = await reader.list_episodes(limit=2, before=None)
    assert [e.episode_id for e in first.episodes] == [ids[2], ids[1]]
    assert [e.pipeline_kind for e in first.episodes] == [PipelineKind.LEGACY, PipelineKind.AGENTIC]
    assert first.next_before is not None
    second = await reader.list_episodes(limit=2, before=first.next_before)
    assert [e.episode_id for e in second.episodes] == [ids[0]]
    assert second.next_before is None


# ---------------------------------------------------------------------------- workspace


async def test_workspace_has_the_workflow_id_control_revision_and_pending_change_sets(
    reader, session_factory, artifact_store
) -> None:
    legacy = await new_episode(session_factory)
    agentic = await new_episode(session_factory)
    await pin_agentic(session_factory, artifact_store, agentic)
    ws = await reader.workspace(legacy)
    assert ws.workflow_id == pipeline_workflow_id(str(legacy))
    assert ws.episode.pipeline_kind is PipelineKind.LEGACY
    assert ws.control_revision == ws.read_model["control"]["control_revision"]
    ws2 = await reader.workspace(agentic)
    assert ws2.workflow_id == f"agentic-episode-{agentic}"

    async with session_factory() as s:
        await seed_script_and_storyboard(s, artifact_store, str(legacy))
    storyboard = (await reader.storyboard(legacy)).storyboard
    assert storyboard is not None
    unestimated = await add_change_set(session_factory, legacy, storyboard.revision_id)
    estimated = await add_change_set(
        session_factory, legacy, storyboard.revision_id, estimated_cost_usd=Decimal("0.25")
    )
    ws3 = await reader.workspace(legacy)
    by_id = {c.change_set_id: c for c in ws3.pending_change_sets}
    assert by_id[unestimated.change_set_id].cost_estimated is False
    assert by_id[unestimated.change_set_id].estimated_cost_usd is None  # 0 にしない
    assert by_id[estimated.change_set_id].estimated_cost_usd == "0.2500"


# ---------------------------------------------------------------------------- storyboard


async def test_storyboard_is_none_before_one_exists_and_the_current_revision_after(
    reader, session_factory, artifact_store
) -> None:
    ep = await new_episode(session_factory)
    assert (await reader.storyboard(ep)).storyboard is None
    async with session_factory() as s:
        await seed_script_and_storyboard(s, artifact_store, str(ep))
    view = (await reader.storyboard(ep)).storyboard
    assert view is not None
    assert [s.scene_id for s in view.scenes] == ["sb1", "sb2", "sb3", "sb4"]
    assert view.editable_fields == ("visual_description",)
    assert len(view.sha256) == 64 and view.schema_version == "1.0"


# ---------------------------------------------------------------------------- change impact


async def test_change_impact_reports_estimate_status_and_blockers(
    reader, session_factory, artifact_store
) -> None:
    ep = await new_episode(session_factory)
    async with session_factory() as s:
        await seed_script_and_storyboard(s, artifact_store, str(ep))
    rev = (await reader.storyboard(ep)).storyboard
    assert rev is not None
    ok = await add_change_set(
        session_factory, ep, rev.revision_id, estimated_cost_usd=Decimal("0.5")
    )
    preview = await reader.change_impact(ep, ok.change_set_id)
    assert (preview.status, preview.cost_estimated, preview.estimated_cost_usd) == (
        "proposed",
        True,
        "0.5000",
    )
    assert preview.blockers == ()

    none = await add_change_set(session_factory, ep, rev.revision_id)
    p2 = await reader.change_impact(ep, none.change_set_id)
    assert p2.estimated_cost_usd is None and not p2.cost_estimated
    assert p2.blockers == ("cost_not_estimated",)

    async with session_factory() as s:
        await InterventionStore(s).set_change_set_status(
            ok.change_set_id, ChangeSetStatus.REJECTED, decided_by_command_id="cmd-reject-01"
        )
        await s.commit()
    p3 = await reader.change_impact(ep, ok.change_set_id)
    assert "invalid_state" in p3.blockers


async def test_a_change_set_of_another_episode_or_unknown_is_not_found(
    reader, session_factory, artifact_store
) -> None:
    a = await new_episode(session_factory)
    b = await new_episode(session_factory)
    async with session_factory() as s:
        await seed_script_and_storyboard(s, artifact_store, str(a))
    rev = (await reader.storyboard(a)).storyboard
    assert rev is not None
    cs = await add_change_set(session_factory, a, rev.revision_id)
    for episode, change_set_id in (
        (b, cs.change_set_id),
        (a, str(uuid.uuid4())),
        (a, "not-a-uuid"),
    ):
        with pytest.raises(OperationError) as err:
            await reader.change_impact(episode, change_set_id)
        assert err.value.code is OperationErrorCode.CHANGE_SET_NOT_FOUND


# ---------------------------------------------------------------------------- decisions / review


async def test_decisions_and_agent_runs_come_from_the_read_model(reader, session_factory) -> None:
    ep = await new_episode(session_factory)
    result = await reader.agent_decisions(ep)
    assert result.episode_id == ep
    assert isinstance(result.decisions, tuple) and isinstance(result.agent_runs, tuple)


async def test_quality_review_is_not_reviewed_until_an_artifact_exists(
    reader, session_factory, artifact_store
) -> None:
    ep = await new_episode(session_factory)
    empty = await reader.quality_review(ep)
    assert (empty.status, empty.review, empty.artifact_id) == ("not_reviewed", None, None)
    review = QualityReview(episode_id=str(ep), overall="needs_changes", findings=())
    async with session_factory() as s:
        await write_episode_artifact(s, artifact_store, bucket=BUCKET, episode_id=ep, model=review)
        await s.commit()
    got = await reader.quality_review(ep)
    assert got.status == "reviewed" and got.review is not None
    assert got.review.overall == "needs_changes" and got.artifact_id is not None
    assert got.review.assessment_kind == "self_assessed"


# ---------------------------------------------------------------------------- progress


async def test_progress_lists_jobs_and_the_latest_failure(reader, session_factory) -> None:
    ep = await new_episode(session_factory)
    assert (await reader.progress(ep)).failure is None
    async with session_factory() as s:
        repo = JobRepository(s)
        job = await repo.create(episode_id=ep, type=JobType.WRITE_SCRIPT)
        await repo.start(job.id)
        await repo.record_failure(
            job.id, event=JobEvent.PERMANENT_FAILURE, failure_class=FailureClass.PERMANENT
        )
        await s.commit()
    progress = await reader.progress(ep)
    assert [j.type for j in progress.jobs] == ["write_script"]
    assert progress.failure is not None
    assert (progress.failure.source, progress.failure.code) == ("job", "permanent")
    assert progress.workflow_id == pipeline_workflow_id(str(ep))


async def test_a_change_on_a_superseded_storyboard_is_blocked_as_stale(
    reader, session_factory, artifact_store
) -> None:
    ep = await new_episode(session_factory)
    async with session_factory() as s:
        await seed_script_and_storyboard(s, artifact_store, str(ep))
    old = (await reader.storyboard(ep)).storyboard
    assert old is not None
    cs = await add_change_set(
        session_factory, ep, old.revision_id, estimated_cost_usd=Decimal("0.1")
    )
    assert (await reader.change_impact(ep, cs.change_set_id)).blockers == ()

    document = dict(await artifact_store.get_json(await _storyboard_key(session_factory, ep)))
    scenes = [dict(x) for x in document["scenes"]]
    scenes[0]["visual_description"] = "a different castle"
    document["scenes"] = scenes
    async with session_factory() as s:
        await store_artifact(s, artifact_store, str(ep), ArtifactType.STORYBOARD, document)
    new = (await reader.storyboard(ep)).storyboard
    assert new is not None and new.revision_id != old.revision_id
    assert "stale_target_revision" in (await reader.change_impact(ep, cs.change_set_id)).blockers


async def _storyboard_key(session_factory, ep: uuid.UUID) -> str:
    async with session_factory() as s:
        meta = await ArtifactMetadataRepository(s).find_current_by_type(ep, ArtifactType.STORYBOARD)
    assert meta is not None
    return meta.object_key
