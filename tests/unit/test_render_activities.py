"""Render Activity（ADR-0019）。SQLite + メモリストア + fake のエンジン・計画・probe。"""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from contracts.artifacts import parse_final_video
from contracts.render_activities import (
    RenderAdmitRequest,
    RenderFinalVideoRequest,
    RenderMarkReadyRequest,
    RenderRecordFailureRequest,
)
from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from domain.artifact.hashing import sha256_hex
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    DurationReconciliationError,
    FinalVideoValidationError,
    MediaValidationError,
    RenderEngineFailedError,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.workdir import WorkDirectory
from tests.support.render_activity import (
    FakeFinalVideoProbe,
    FakePlanning,
    FakeRenderer,
    RenderSeed,
    record_scene_video,
    seed_render_inputs,
)
from workers.render.activities import RenderActivities, required_free_bytes

WF = "episode-x-render"


class Harness:
    def __init__(self, session_factory, store, tmp_path: Path, **overrides: Any) -> None:
        font = tmp_path / "font.ttc"
        font.write_bytes(b"fake font")
        self.store = store
        self.engine = FakeRenderer()
        self.planning = FakePlanning()
        self.probe = FakeFinalVideoProbe()
        self.heartbeats: list[tuple[Any, ...]] = []
        self.free_bytes = 10**15
        self.work_root = tmp_path / "work"
        kwargs: dict[str, Any] = {
            "session_factory": session_factory,
            "store": store,
            "bucket": "artifacts",
            "workdir": WorkDirectory(self.work_root, forbidden=()),
            "engine": self.engine,
            "probe": self.probe,
            "planning": self.planning,
            "font_path": font,
            "font_sha256": sha256_hex(b"fake font"),
            "render_timeout_seconds": 60,
            "min_free_bytes": 1_000,
            "disk_usage": lambda _path: SimpleNamespace(free=self.free_bytes),
            "heartbeat": lambda *d: self.heartbeats.append(d),
        }
        kwargs.update(overrides)
        self.activities = RenderActivities(**kwargs)


@pytest.fixture
def harness(session_factory, artifact_store, tmp_path) -> Harness:
    return Harness(session_factory, artifact_store, tmp_path)


def _req(seed: RenderSeed, profile: str = "shorts_vertical", run: str = "run-1"):
    return RenderFinalVideoRequest(seed.episode_id, WF, run, profile)


async def _admit(h: Harness, seed: RenderSeed, wf: str = WF, run: str = "run-1"):
    return await h.activities.admit(RenderAdmitRequest(seed.episode_id, wf, run))


async def _jobs(session_factory, episode_id):
    async with session_factory() as session:
        return [
            j
            for j in await JobRepository(session).list_for_episode(episode_id)
            if j.type is JobType.RENDER_FINAL_VIDEO
        ]


async def _status(session_factory, episode_id) -> EpisodeStatus:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
        assert episode is not None
        return episode.status


async def _final_rows(session_factory, episode_id):
    async with session_factory() as session:
        return [
            a
            for a in await ArtifactMetadataRepository(session).list_for_episode(episode_id)
            if a.artifact_type is ArtifactType.FINAL_VIDEO
        ]


def _job_dirs(h: Harness, episode_id: str) -> list[Path]:
    base = h.work_root / "episodes" / episode_id
    return list(base.iterdir()) if base.exists() else []


# --------------------------------------------------------------------------- admit


async def test_admit_from_assets_ready_enters_in_progress_and_creates_one_job(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)

    result = await _admit(harness, seed)
    again = await _admit(harness, seed)

    assert result.admitted and result.status == EpisodeStatus.IN_PROGRESS.value
    assert again.admitted  # 同じトークンの再実行
    jobs = await _jobs(session_factory, seed.episode_id)
    assert len(jobs) == 1 and jobs[0].status is JobStatus.QUEUED


async def test_admit_rejects_statuses_outside_the_render_entry(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(
        session_factory, artifact_store, events=[EpisodeEvent.WORKFLOW_STARTED]
    )
    seed2 = await seed_render_inputs(
        session_factory,
        artifact_store,
        events=[EpisodeEvent.WORKFLOW_STARTED, EpisodeEvent.SCRIPT_READY],
    )

    assert not (await _admit(harness, seed)).admitted  # 他の run が in_progress
    rejected = await _admit(harness, seed2)
    assert not rejected.admitted and rejected.status == EpisodeStatus.SCRIPT_READY.value
    assert await _jobs(session_factory, seed2.episode_id) == []


async def test_admit_resumes_only_episodes_render_itself_blocked(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit(harness, seed)
    await harness.activities.record_failure(
        RenderRecordFailureRequest(seed.episode_id, WF, "run-1", "needs_input", "x", False)
    )
    assert await _status(session_factory, seed.episode_id) is EpisodeStatus.BLOCKED

    other = await _admit(harness, seed, wf=f"episode-{seed.episode_id}-production", run="r2")
    resumed = await _admit(harness, seed, run="run-2")

    assert not other.admitted
    assert resumed.admitted and resumed.status == EpisodeStatus.IN_PROGRESS.value


async def test_admit_from_render_ready_allows_a_re_render(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit(harness, seed)
    await harness.activities.render_final_video(_req(seed))
    await harness.activities.mark_ready(RenderMarkReadyRequest(seed.episode_id, WF, "run-1"))
    assert await _status(session_factory, seed.episode_id) is EpisodeStatus.RENDER_READY

    again = await _admit(harness, seed, run="run-2")

    assert again.admitted and again.status == EpisodeStatus.IN_PROGRESS.value


# --------------------------------------------------------------------------- render


async def test_render_stores_a_verified_final_video_and_cleans_the_work_directory(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit(harness, seed)

    result = await harness.activities.render_final_video(_req(seed))

    assert not result.skipped and result.version == 1
    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.status is JobStatus.SUCCEEDED and result.job_id == job.id
    (row,) = await _final_rows(session_factory, seed.episode_id)
    assert row.id == result.artifact_id and row.produced_by_job_id is None or True
    final = parse_final_video(await artifact_store.get_json(row.object_key))
    assert final.source_production_manifest.artifact_id == seed.manifest.id  # type: ignore[union-attr]
    assert final.source_script.sha256 == seed.script.sha256
    assert final.render_profile.profile_id == "shorts_vertical"
    assert final.render_engine.binary_sha256 == "e" * 64
    assert final.media.object_key == f"media/{seed.episode_id}/final_video/{final.media.sha256}.mp4"
    body = await artifact_store.get_bytes(final.media.object_key)
    assert sha256_hex(body) == final.media.sha256 == harness.planning.qa_calls[0]["readback"]
    assert final.render_plan_sha256 == harness.planning.plan_sha256(
        harness.engine.jobs[0].plan
    )
    # 入力は作業領域に置かれ、読み戻し検証済み。終わったら片付く
    (render_job,) = harness.engine.jobs
    assert sorted(render_job.scene_video_paths) == ["sb1", "sb2", "sb3", "sb4"]
    assert sorted(render_job.voice_paths) == ["s1", "s2", "s3"]
    assert _job_dirs(harness, seed.episode_id) == []
    assert ("rendering",) in harness.heartbeats and ("rendered",) in harness.heartbeats


async def test_same_input_is_skipped_without_rendering(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit(harness, seed)
    first = await harness.activities.render_final_video(_req(seed))
    await _admit(harness, seed, run="run-2")  # 新しい job を作る（前の job は終端）

    second = await harness.activities.render_final_video(_req(seed, run="run-2"))

    assert second.skipped and second.artifact_id == first.artifact_id
    assert len(harness.engine.jobs) == 1
    statuses = sorted(j.status.value for j in await _jobs(session_factory, seed.episode_id))
    assert statuses == ["skipped", "succeeded"]
    assert len(await _final_rows(session_factory, seed.episode_id)) == 1


async def test_another_profile_is_a_new_version_that_supersedes_the_old(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    first = await harness.activities.render_final_video(_req(seed))
    harness.probe.width, harness.probe.height = 1920, 1080

    second = await harness.activities.render_final_video(_req(seed, "long_form_horizontal"))

    assert not second.skipped and second.version == 2 and second.artifact_id != first.artifact_id
    async with session_factory() as session:
        current = await ArtifactMetadataRepository(session).find_current_by_type(
            seed.episode_id, ArtifactType.FINAL_VIDEO
        )
    assert current is not None and current.id == second.artifact_id
    assert len(await _final_rows(session_factory, seed.episode_id)) == 2


def _assert_app_error(err: ApplicationError, type_name: str, *, non_retryable: bool) -> None:
    assert err.type == type_name, err
    assert err.non_retryable is non_retryable


async def test_media_sha_mismatch_is_an_integrity_error(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    video = await artifact_store.get_json(seed.videos["sb2"].object_key)
    artifact_store._objects[video["media"]["object_key"]] = b"tampered"

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderInputIntegrityError", non_retryable=True)
    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.status is JobStatus.TERMINAL_FAILED
    assert harness.engine.jobs == [] and _job_dirs(harness, seed.episode_id) == []
    assert await _final_rows(session_factory, seed.episode_id) == []


async def test_artifact_json_sha_mismatch_is_an_integrity_error(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    payload = await artifact_store.get_json(seed.voices["s1"].object_key)
    payload["voice_id"] = "someone-else"
    import json

    artifact_store._objects[seed.voices["s1"].object_key] = json.dumps(payload).encode()

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderInputIntegrityError", non_retryable=True)


async def test_missing_manifest_is_needs_input(harness, session_factory, artifact_store) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store, with_manifest=False)

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderInputMissingError", non_retryable=True)


async def test_missing_media_object_is_needs_input(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    video = await artifact_store.get_json(seed.videos["sb1"].object_key)
    del artifact_store._objects[video["media"]["object_key"]]

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderInputMissingError", non_retryable=True)
    assert _job_dirs(harness, seed.episode_id) == []


async def test_manifest_pointing_at_a_superseded_scene_video_is_stale(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await record_scene_video(session_factory, artifact_store, seed, "sb1", 8000, salt="v2")

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderInputStaleError", non_retryable=True)
    assert harness.engine.jobs == []


async def test_unknown_profile_is_permanent(harness, session_factory, artifact_store) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed, "no_such_profile"))

    _assert_app_error(info.value, "UnknownRenderProfileError", non_retryable=True)


async def test_font_sha_mismatch_is_engine_unavailable(
    session_factory, artifact_store, tmp_path
) -> None:
    h = Harness(session_factory, artifact_store, tmp_path, font_sha256="0" * 64)
    seed = await seed_render_inputs(session_factory, artifact_store)

    with pytest.raises(ApplicationError) as info:
        await h.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderEngineUnavailableError", non_retryable=True)


async def test_timeline_errors_from_planning_are_needs_input(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.planning.plan_error = DurationReconciliationError("sb2 too short")

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "DurationReconciliationError", non_retryable=True)


async def test_disk_preflight_refuses_without_deleting_anything(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.free_bytes = required_free_bytes(min_free_bytes=1_000, input_bytes=1) - 1

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderWorkspaceFullError", non_retryable=False)
    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.status is JobStatus.RETRYABLE_FAILED
    assert harness.engine.jobs == []


async def test_enospc_during_render_is_workspace_full(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.errors.append(OSError(errno.ENOSPC, "No space left on device"))

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderWorkspaceFullError", non_retryable=False)
    assert _job_dirs(harness, seed.episode_id) == []


async def test_engine_failure_is_retryable_and_the_retry_reuses_the_job(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.errors.append(RenderEngineFailedError("exit 1"))

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))
    _assert_app_error(info.value, "RenderEngineFailedError", non_retryable=False)
    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.status is JobStatus.RETRYABLE_FAILED

    result = await harness.activities.render_final_video(_req(seed))  # Temporal の retry

    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.id == result.job_id and job.status is JobStatus.SUCCEEDED and job.attempts == 2


async def test_undecodable_output_is_corrupt_and_nothing_is_recorded(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.probe.error = MediaValidationError("no frames")

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "FinalVideoCorruptError", non_retryable=False)
    assert await _final_rows(session_factory, seed.episode_id) == []


async def test_failed_technical_qa_stores_no_final_video_metadata(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.planning.qa_error = FinalVideoValidationError("width 720 != 1080")

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "FinalVideoValidationError", non_retryable=True)
    assert await _final_rows(session_factory, seed.episode_id) == []
    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.status is JobStatus.TERMINAL_FAILED


async def test_contract_violation_in_final_video_is_validation_error(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.probe.duration_ms = 10_000  # 計画の総尺 25000 と許容差を超えて違う

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "FinalVideoValidationError", non_retryable=True)


async def test_cancellation_reaches_the_engine_and_cleans_the_work_directory(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.hang = True

    task = asyncio.create_task(harness.activities.render_final_video(_req(seed)))
    await asyncio.wait_for(harness.engine.started.wait(), timeout=5)
    assert len(_job_dirs(harness, seed.episode_id)) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.engine.cancelled
    assert _job_dirs(harness, seed.episode_id) == []
    assert await _final_rows(session_factory, seed.episode_id) == []
    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.status is JobStatus.RUNNING  # record_failure が閉じる


# --------------------------------------------------------------------------- 完了 / 失敗


async def test_mark_ready_parks_at_render_ready_and_refuses_other_tokens(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit(harness, seed)

    stranger = await harness.activities.mark_ready(
        RenderMarkReadyRequest(seed.episode_id, WF, "other-run")
    )
    ready = await harness.activities.mark_ready(RenderMarkReadyRequest(seed.episode_id, WF, "run-1"))
    again = await harness.activities.mark_ready(RenderMarkReadyRequest(seed.episode_id, WF, "run-1"))

    assert not stranger.owned
    assert ready.status == again.status == EpisodeStatus.RENDER_READY.value


@pytest.mark.parametrize(
    ("failure_class", "exhausted", "episode", "job"),
    [
        ("retryable", True, EpisodeStatus.BLOCKED, JobStatus.RETRYABLE_FAILED),
        ("retryable", False, EpisodeStatus.NEEDS_WORK, JobStatus.RETRYABLE_FAILED),
        ("needs_input", False, EpisodeStatus.BLOCKED, JobStatus.RETRYABLE_FAILED),
        ("permanent", False, EpisodeStatus.FAILED, JobStatus.TERMINAL_FAILED),
    ],
)
async def test_record_failure_maps_classes_and_never_fails_terminally_for_retryable(
    harness, session_factory, artifact_store, failure_class, exhausted, episode, job
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit(harness, seed)

    outcome = await harness.activities.record_failure(
        RenderRecordFailureRequest(seed.episode_id, WF, "run-1", failure_class, "boom", exhausted)
    )

    assert outcome.episode_status == episode.value
    (row,) = await _jobs(session_factory, seed.episode_id)
    assert row.status is job


async def test_record_failure_with_a_foreign_token_writes_nothing(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await _admit(harness, seed)

    outcome = await harness.activities.record_failure(
        RenderRecordFailureRequest(seed.episode_id, WF, "run-9", "permanent", "boom", False)
    )

    assert not outcome.owned
    assert await _status(session_factory, seed.episode_id) is EpisodeStatus.IN_PROGRESS
