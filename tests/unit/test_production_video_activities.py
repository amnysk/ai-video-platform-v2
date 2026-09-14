"""Production Video Activity（ADR-0017 Phase 4C）。SQLite + メモリストア + fake 生成器。"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from contracts.artifacts import build_scene_image_artifact, parse_scene_video_artifact
from contracts.production_activities import VideoAwaitRequest, VideoSubmitRequest
from contracts.states import ArtifactType, JobStatus, JobType, ProviderCall, ReservationStatus
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.errors import (
    MediaValidationError,
    ProductionInputInvalidError,
    ProviderInvocationError,
    ProviderPollDeadlineError,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeVideoGenerator, make_mp4, make_png, sample_storyboard
from workers.production_video.activities import VideoProductionActivities


def make_activities(
    session_factory, store, generator, tmp_path, **kwargs
) -> VideoProductionActivities:
    runner = PaidJobRunner(
        session_factory=session_factory,
        store=store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )
    return VideoProductionActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        probe=kwargs.pop("probe", PillowAvMediaProbe()),
        runner=runner,
        bucket="artifacts",
        poll_interval_seconds=0,
        **kwargs,
    )


_colors = itertools.count(1)


async def seed_scene_image(session_factory, store, episode_id, sb_meta, scene_id) -> str:
    """scene image Artifact を直接記録する（画像 worker を import しない / INV-3）。"""
    png = make_png(1080, 1920, (next(_colors) % 255, 90, 160))
    media_sha = sha256_hex(png)
    media_key = media_object_key(episode_id, "scene_image", scene_id, media_sha, "png")
    await store.put_bytes(media_key, png, "image/png")
    payload = build_scene_image_artifact(
        episode_id=episode_id,
        source_storyboard={
            "artifact_id": sb_meta.id,
            "sha256": sb_meta.sha256,
            "schema_version": "1.0",
        },
        scene_id=scene_id,
        media={
            "object_key": media_key,
            "sha256": media_sha,
            "bytes": len(png),
            "mime": "image/png",
        },
        width=1080,
        height=1920,
        generator={
            "generator": "fake-image",
            "generator_model": "fake",
            "generation_profile_id": "fake-image-profile-v1",
        },
    )
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode_id, "scene_image", digest, scene_id)
    await store.put_json(key, payload)
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.SCENE_IMAGE,
            schema_version="1.0",
            bucket="artifacts",
            object_key=key,
            sha256=digest,
            input_hash=sha256_hex(scene_id.encode()),
            scene_id=scene_id,
        )
        await session.commit()
    return meta.id


async def seed(session_factory, store, scenes=("sb1",)):
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    payload = sample_storyboard(episode.id).model_dump(mode="json")
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode.id, "storyboard", digest)
    await store.put_json(key, payload)
    async with session_factory() as session:
        sb = await ArtifactMetadataRepository(session).record(
            episode_id=episode.id,
            artifact_type=ArtifactType.STORYBOARD,
            schema_version="1.0",
            bucket="artifacts",
            object_key=key,
            sha256=digest,
        )
        await session.commit()
    images = {s: await seed_scene_image(session_factory, store, episode.id, sb, s) for s in scenes}
    return episode.id, sb.id, images


# sample_storyboard: sb1=8000ms, sb2=4000ms, sb3=5000ms
DURATIONS = {"sb1": 8000, "sb2": 4000, "sb3": 5000, "sb4": 8000}


def _submit(ep, sb, img, scene="sb1", round=1) -> VideoSubmitRequest:
    return VideoSubmitRequest(ep, "wf", "run", scene, sb, img, DURATIONS[scene], round)


def _await(ep, sb, img, reservation_id, scene="sb1") -> VideoAwaitRequest:
    return VideoAwaitRequest(ep, "wf", "run", scene, sb, img, DURATIONS[scene], reservation_id)


async def _reservations(session_factory, episode_id):
    async with session_factory() as session:
        return await ProviderReservationRepository(session).find_unreconciled(
            episode_id=episode_id, provider=ProviderCall.FAL_VIDEO, scene_id="sb1"
        )


async def _jobs(session_factory, episode_id):
    async with session_factory() as session:
        return [
            j
            for j in await JobRepository(session).list_for_episode(episode_id)
            if j.type is JobType.PRODUCE_SCENE_VIDEO
        ]


async def test_submit_await_produces_a_scene_video_and_reuses(
    session_factory, artifact_store, tmp_path
) -> None:
    ep, sb, images = await seed(session_factory, artifact_store)
    gen = FakeVideoGenerator(pending_polls=2)
    beats: list[object] = []
    acts = make_activities(
        session_factory, artifact_store, gen, tmp_path, heartbeat=lambda *d: beats.append(d)
    )

    submitted = await acts.submit(_submit(ep, sb, images["sb1"]))
    assert submitted.artifact is None and submitted.reservation_id
    result = await acts.await_video(_await(ep, sb, images["sb1"], submitted.reservation_id))
    assert result.reused is False
    assert gen.poll_calls == 3
    assert sum(1 for (d,) in beats if "polls" in d) == 3  # type: ignore[misc]

    artifact = parse_scene_video_artifact(await artifact_store.get_json(result.object_key))
    assert artifact.requested_duration_ms == 8000 and abs(artifact.duration_ms - 8000) <= 300
    assert (artifact.width, artifact.height, artifact.fps_millis) == (720, 1280, 24000)
    assert artifact.has_audio is False and artifact.source_image.artifact_id == images["sb1"]
    assert sha256_hex(await artifact_store.get_bytes(artifact.media.object_key)) == (
        artifact.media.sha256
    )
    assert artifact.generator.generation_profile_id.startswith("fake-video-profile-v1+")

    async with session_factory() as session:
        row = await ProviderReservationRepository(session).get(submitted.reservation_id)
    assert row is not None and row.status is ReservationStatus.SPENT
    assert row.outcome_artifact_id == result.artifact_id

    again = await acts.submit(_submit(ep, sb, images["sb1"]))
    assert again.artifact is not None and again.artifact.artifact_id == result.artifact_id
    assert gen.submit_calls == 1
    assert [j.status for j in await _jobs(session_factory, ep)] == [
        JobStatus.SUCCEEDED,
        JobStatus.SKIPPED,
    ]


class _FailingPrepare(FakeVideoGenerator):
    async def prepare(self, request):
        raise ProviderInvocationError("upload failed")


async def test_upload_failure_leaves_no_reservation(
    session_factory, artifact_store, tmp_path
) -> None:
    ep, sb, images = await seed(session_factory, artifact_store)
    gen = _FailingPrepare()
    acts = make_activities(session_factory, artifact_store, gen, tmp_path)
    with pytest.raises(ProviderInvocationError):
        await acts.submit(_submit(ep, sb, images["sb1"]))
    assert gen.submit_calls == 0
    assert await _reservations(session_factory, ep) == []
    assert (await _jobs(session_factory, ep))[0].status is JobStatus.RETRYABLE_FAILED


async def test_await_resume_never_resubmits(session_factory, artifact_store, tmp_path) -> None:
    ep, sb, images = await seed(session_factory, artifact_store)
    gen = FakeVideoGenerator(pending_polls=3)
    impatient = make_activities(
        session_factory, artifact_store, gen, tmp_path, await_deadline_seconds=0
    )
    submitted = await impatient.submit(_submit(ep, sb, images["sb1"]))
    with pytest.raises(ProviderPollDeadlineError):
        await impatient.await_video(_await(ep, sb, images["sb1"], submitted.reservation_id))
    patient = make_activities(session_factory, artifact_store, gen, tmp_path)
    # submit の再実行も台帳の参照から再開する
    resumed = await patient.submit(_submit(ep, sb, images["sb1"]))
    assert resumed.reservation_id == submitted.reservation_id
    result = await patient.await_video(_await(ep, sb, images["sb1"], submitted.reservation_id))
    assert result.reused is False and gen.submit_calls == 1
    assert (await _jobs(session_factory, ep))[0].status is JobStatus.SUCCEEDED


async def test_image_sha_mismatch_and_scene_mismatch_are_invalid(
    session_factory, artifact_store, tmp_path
) -> None:
    ep, sb, images = await seed(session_factory, artifact_store, scenes=("sb1", "sb2"))
    acts = make_activities(session_factory, artifact_store, FakeVideoGenerator(), tmp_path)
    with pytest.raises(ProductionInputInvalidError):
        await acts.submit(_submit(ep, sb, images["sb2"], scene="sb1"))  # 別シーンの画像
    with pytest.raises(ProductionInputInvalidError):
        bad = dataclasses.replace(_submit(ep, sb, images["sb1"]), requested_duration_ms=3000)
        await acts.submit(bad)

    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).get(images["sb1"])
    assert meta is not None
    image = await artifact_store.get_json(meta.object_key)
    artifact_store._objects[image["media"]["object_key"]] = b"tampered"  # type: ignore[attr-defined]
    gen = FakeVideoGenerator()
    acts = make_activities(session_factory, artifact_store, gen, tmp_path)
    with pytest.raises(ProductionInputInvalidError):
        await acts.submit(_submit(ep, sb, images["sb1"]))
    assert gen.submit_calls == 0


class _AudioProbe(PillowAvMediaProbe):
    def probe_video(self, data: bytes):
        return dataclasses.replace(super().probe_video(data), has_audio=True)


class _WrongDuration(FakeVideoGenerator):
    def _render(self, request):
        return make_mp4(request.duration_ms + 3000, *self.size, self.fps)


@pytest.mark.parametrize(
    ("gen", "probe"), [(FakeVideoGenerator(), _AudioProbe()), (_WrongDuration(), None)]
)
async def test_invalid_media_is_rejected_after_spending(
    session_factory, artifact_store, tmp_path, gen, probe
) -> None:
    ep, sb, images = await seed(session_factory, artifact_store)
    kwargs = {"probe": probe} if probe else {}
    acts = make_activities(session_factory, artifact_store, gen, tmp_path, **kwargs)
    s = await acts.submit(_submit(ep, sb, images["sb1"]))
    with pytest.raises(MediaValidationError):
        await acts.await_video(_await(ep, sb, images["sb1"], s.reservation_id))
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).get(s.reservation_id)
        current = await ArtifactMetadataRepository(session).list_current_by_type(
            ep, ArtifactType.SCENE_VIDEO
        )
    assert row is not None and row.status is ReservationStatus.SPENT and row.raw_output_key
    assert current == []


def test_activity_names_match_contracts(session_factory, artifact_store, tmp_path) -> None:
    from temporalio import activity

    from contracts.production_activities import VIDEO_AWAIT, VIDEO_SUBMIT

    acts = make_activities(session_factory, artifact_store, FakeVideoGenerator(), tmp_path)
    names = {activity._Definition.must_from_callable(fn).name for fn in acts.all_activities()}  # type: ignore[attr-defined]
    assert names == {VIDEO_SUBMIT, VIDEO_AWAIT}
