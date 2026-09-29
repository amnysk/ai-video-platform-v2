"""Render Activity（ADR-0019）。SQLite + メモリストア + fake のエンジン・計画・probe。"""

from __future__ import annotations

import asyncio
import dataclasses
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
from domain.artifact.verification import ArtifactVerdict
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    MediaValidationError,
    RenderEngineFailedError,
)
from domain.render.identity import render_plan_sha256
from infrastructure.artifact.verify import verify_artifact
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.workdir import WorkDirectory
from tests.support.fake_render_engine import FakeFinalVideoProbe, FakeRenderEngine
from tests.support.render_activity import (
    PassingSourceProbe,
    RenderSeed,
    record_manifest,
    record_scene_video,
    seed_render_inputs,
)
from workers.render.activities import RenderActivities, required_free_bytes

WF = "episode-x-render"


class _Probe(FakeFinalVideoProbe):
    def __init__(self) -> None:
        super().__init__()
        self.error: BaseException | None = None

    def probe_final_video(self, path: str):
        if self.error is not None:
            raise self.error
        return super().probe_final_video(path)


class Harness:
    def __init__(self, session_factory, store, tmp_path: Path, **overrides: Any) -> None:
        font = tmp_path / "font.ttc"
        font.write_bytes(b"fake font")
        self.store = store
        self.probe = _Probe()
        self.source_probe = PassingSourceProbe()
        self.started = asyncio.Event()
        self.engine = FakeRenderEngine(on_render=self._on_render)
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
            "source_probe": self.source_probe,
            "render_threads": 4,
            "font_path": font,
            "font_sha256": sha256_hex(b"fake font"),
            "render_timeout_seconds": 60,
            "min_free_bytes": 1_000,
            "disk_usage": lambda _path: SimpleNamespace(free=self.free_bytes),
            "heartbeat": lambda *d: self.heartbeats.append(d),
        }
        kwargs.update(overrides)
        self.activities = RenderActivities(**kwargs)
        self.activities.heartbeat_interval_seconds = 0.01

    def _on_render(self, request) -> None:
        self.probe.plan = request.plan
        self.started.set()


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
    """試行ごとの作業領域（``<job>/attempt-<n>``）。"""
    return sorted((h.work_root / "episodes" / episode_id).glob("*/attempt-*"))


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
    assert row.id == result.artifact_id
    final = parse_final_video(await artifact_store.get_json(row.object_key))
    assert final.source_production_manifest.artifact_id == seed.manifest.id  # type: ignore[union-attr]
    assert final.source_script.sha256 == seed.script.sha256
    assert final.render_profile.profile_id == "shorts_vertical"
    assert final.render_engine == harness.engine.identity()
    assert final.media.object_key == f"media/{seed.episode_id}/final_video/{final.media.sha256}.mp4"
    body = await artifact_store.get_bytes(final.media.object_key)
    assert sha256_hex(body) == final.media.sha256 and final.technical_qa.passed
    assert final.render_plan_sha256 == render_plan_sha256(harness.engine.requests[0].plan)
    # 入力は作業領域に置かれ、読み戻し検証済み。終わったら片付く
    (render_job,) = harness.engine.requests
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
    assert len(harness.engine.requests) == 1
    statuses = sorted(j.status.value for j in await _jobs(session_factory, seed.episode_id))
    assert statuses == ["skipped", "succeeded"]
    assert len(await _final_rows(session_factory, seed.episode_id)) == 1


async def test_another_profile_is_a_new_version_that_supersedes_the_old(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.payload = b"shorts"
    first = await harness.activities.render_final_video(_req(seed))
    harness.engine.payload = b"long-form"

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
    assert harness.engine.requests == [] and len(_job_dirs(harness, seed.episode_id)) == 1
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
    assert len(_job_dirs(harness, seed.episode_id)) == 1  # 失敗時は調査のため残す


async def test_manifest_pointing_at_a_superseded_scene_video_is_stale(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    await record_scene_video(session_factory, artifact_store, seed, "sb1", 8000, salt="v2")

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderInputStaleError", non_retryable=True)
    assert harness.engine.requests == []


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
    # sb2 は 4000ms。1000ms の素材は freeze の上限（2000ms）を超えて短い
    await record_scene_video(session_factory, artifact_store, seed, "sb2", 1000, salt="short")
    await record_manifest(session_factory, artifact_store, seed)

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
    assert harness.engine.requests == []


async def test_enospc_during_render_is_workspace_full(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.failures.append(OSError(errno.ENOSPC, "No space left on device"))

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderWorkspaceFullError", non_retryable=False)
    assert len(_job_dirs(harness, seed.episode_id)) == 1


async def test_engine_failure_is_retryable_and_the_retry_reuses_the_job(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.failures.append(RenderEngineFailedError("exit 1"))

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
    harness.probe.overrides["width"] = 720

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "FinalVideoValidationError", non_retryable=True)
    assert await _final_rows(session_factory, seed.episode_id) == []
    (job,) = await _jobs(session_factory, seed.episode_id)
    assert job.status is JobStatus.TERMINAL_FAILED
    # H2: 不合格の本体は保存しない
    assert not [k for k in artifact_store._objects if "/final_video/" in k]
    assert len(_job_dirs(harness, seed.episode_id)) == 1


async def test_duration_mismatch_in_final_video_is_validation_error(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.probe.overrides["duration_ms"] = 10_000  # 計画の総尺 25000 と許容差を超えて違う

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "FinalVideoValidationError", non_retryable=True)


async def test_cancellation_reaches_the_engine_and_cleans_the_work_directory(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.delay_seconds = 30

    task = asyncio.create_task(harness.activities.render_final_video(_req(seed)))
    await asyncio.wait_for(harness.started.wait(), timeout=5)
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
    ready = await harness.activities.mark_ready(
        RenderMarkReadyRequest(seed.episode_id, WF, "run-1")
    )
    again = await harness.activities.mark_ready(
        RenderMarkReadyRequest(seed.episode_id, WF, "run-1")
    )

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


async def test_failure_details_carry_the_job_id(harness, session_factory, artifact_store) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.failures.append(RenderEngineFailedError("exit 1"))

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    (job,) = await _jobs(session_factory, seed.episode_id)
    assert list(info.value.details) == [job.id]


async def test_undecodable_source_media_is_needs_input_before_rendering(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    video = await artifact_store.get_json(seed.videos["sb3"].object_key)
    harness.source_probe.failing.add(artifact_store._objects[video["media"]["object_key"]])

    with pytest.raises(ApplicationError) as info:
        await harness.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "RenderSourceMediaError", non_retryable=True)
    assert harness.engine.requests == []


async def test_all_sources_are_probed(harness, session_factory, artifact_store) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)

    await harness.activities.render_final_video(_req(seed))

    assert sorted(harness.source_probe.probed) == ["audio"] * 3 + ["video"] * 4


async def test_slow_post_render_steps_keep_heartbeating(
    harness, session_factory, artifact_store
) -> None:
    import time

    seed = await seed_render_inputs(session_factory, artifact_store)
    original = harness.probe.probe_final_video

    def slow_probe(path: str):
        time.sleep(0.1)
        return original(path)

    harness.probe.probe_final_video = slow_probe  # type: ignore[method-assign]

    await harness.activities.render_final_video(_req(seed))

    assert ("probe",) in harness.heartbeats


class _BadReadbackStore:
    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def sha256_of(self, key: str) -> str:
        if "/final_video/" in key and key.endswith(".mp4"):
            return "0" * 64
        return await self._inner.sha256_of(key)


async def test_media_readback_mismatch_is_corrupt_and_records_nothing(
    session_factory, artifact_store, tmp_path
) -> None:
    h = Harness(session_factory, _BadReadbackStore(artifact_store), tmp_path)
    seed = await seed_render_inputs(session_factory, artifact_store)

    with pytest.raises(ApplicationError) as info:
        await h.activities.render_final_video(_req(seed))

    _assert_app_error(info.value, "FinalVideoCorruptError", non_retryable=False)
    assert await _final_rows(session_factory, seed.episode_id) == []


async def test_recorded_qa_reflects_actual_readback_and_source_checks(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    result = await harness.activities.render_final_video(_req(seed))

    (row,) = await _final_rows(session_factory, seed.episode_id)
    assert row.id == result.artifact_id
    final = parse_final_video(await artifact_store.get_json(row.object_key))
    checks = {c.check: c for c in final.technical_qa.checks}
    assert checks["media_readback"].passed and checks["source_readback"].passed


async def test_corrupt_current_final_video_is_re_rendered_not_reused(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    first = await harness.activities.render_final_video(_req(seed))
    final = parse_final_video(
        await artifact_store.get_json(
            (await _final_rows(session_factory, seed.episode_id))[0].object_key
        )
    )
    artifact_store._objects[final.media.object_key] = b"bit rot"
    harness.engine.payload = b"re-rendered"
    await _admit(harness, seed, run="run-2")

    second = await harness.activities.render_final_video(_req(seed, run="run-2"))

    assert not second.skipped and second.version == 2 and second.artifact_id != first.artifact_id
    assert len(harness.engine.requests) == 2


async def test_final_video_with_a_retired_render_profile_is_version_mismatch(
    harness, session_factory, artifact_store
) -> None:
    """ADR-0033: RENDER_PROFILES に無い profile_id を指す final_video は再利用できない。

    現行 verdict の分岐を実際の FinalVideoArtifact 形状（他の全フィールドは有効）で検査する
    （独立レビュー指摘: この分岐は当時どのテストにも通っていなかった）。
    """
    seed = await seed_render_inputs(session_factory, artifact_store)
    await harness.activities.render_final_video(_req(seed))
    row = (await _final_rows(session_factory, seed.episode_id))[0]
    payload = await artifact_store.get_json(row.object_key)
    payload["render_profile"] = {**payload["render_profile"], "profile_id": "retired_profile_v0"}
    stale_key = row.object_key + ".retired-profile-test"
    put = await artifact_store.put_json(stale_key, payload)
    stale_row = dataclasses.replace(
        row, object_key=stale_key, sha256=put.sha256, size_bytes=put.size
    )

    result = await verify_artifact(artifact_store, stale_row)

    assert result.verdict is ArtifactVerdict.VERSION_MISMATCH


async def test_attempts_get_their_own_work_directories(
    harness, session_factory, artifact_store, monkeypatch
) -> None:
    import workers.render.activities as acts

    seed = await seed_render_inputs(session_factory, artifact_store)
    harness.engine.failures.append(RenderEngineFailedError("exit 1"))
    monkeypatch.setattr(acts, "_attempt", lambda: 1)
    with pytest.raises(ApplicationError):
        await harness.activities.render_final_video(_req(seed))
    monkeypatch.setattr(acts, "_attempt", lambda: 2)

    await harness.activities.render_final_video(_req(seed))

    assert [p.name for p in _job_dirs(harness, seed.episode_id)] == ["attempt-1"]


async def test_admit_returns_the_worker_render_timeout(
    harness, session_factory, artifact_store
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    result = await _admit(harness, seed)
    assert result.render_timeout_seconds == 60
