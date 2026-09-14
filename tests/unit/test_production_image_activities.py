"""Production Image Activity（ADR-0017 Phase 4A）。SQLite + メモリストア + fake 生成器。"""

from __future__ import annotations

import uuid

import pytest
from temporalio.exceptions import ApplicationError

from contracts.artifacts import parse_scene_image_artifact
from contracts.production_activities import ImageAwaitRequest, ImageSubmitRequest
from contracts.states import ArtifactType, JobStatus, JobType, ReservationStatus
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeImageGenerator, sample_storyboard
from workers.production_image.activities import ImageProductionActivities


async def _sleep(_: float) -> None:
    return None


def make_activities(session_factory, store, generator, tmp_path) -> ImageProductionActivities:
    runner = PaidJobRunner(
        session_factory=session_factory,
        store=store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )
    return ImageProductionActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        runner=runner,
        bucket="artifacts",
        poll_interval_seconds=0,
    )


async def seed_storyboard(session_factory, store) -> tuple[str, str]:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    payload = sample_storyboard(episode.id).model_dump(mode="json")
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode.id, "storyboard", digest)
    await store.put_json(key, payload)
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode.id,
            artifact_type=ArtifactType.STORYBOARD,
            schema_version="1.0",
            bucket="artifacts",
            object_key=key,
            sha256=digest,
        )
        await session.commit()
    return episode.id, meta.id


def _submit(episode_id, storyboard_id, scene="sb1", round=1) -> ImageSubmitRequest:
    return ImageSubmitRequest(
        episode_id=episode_id,
        workflow_id="wf",
        run_id="run",
        scene_id=scene,
        storyboard_artifact_id=storyboard_id,
        round=round,
    )


def _await(episode_id, storyboard_id, reservation_id, scene="sb1") -> ImageAwaitRequest:
    return ImageAwaitRequest(
        episode_id=episode_id,
        workflow_id="wf",
        run_id="run",
        scene_id=scene,
        storyboard_artifact_id=storyboard_id,
        reservation_id=reservation_id,
    )


async def _jobs(session_factory, episode_id):
    async with session_factory() as session:
        return [
            j
            for j in await JobRepository(session).list_for_episode(episode_id)
            if j.type is JobType.PRODUCE_SCENE_IMAGE
        ]


async def test_submit_await_produces_a_validated_scene_image(
    session_factory, artifact_store, tmp_path
) -> None:
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator()
    acts = make_activities(session_factory, artifact_store, gen, tmp_path)

    submitted = await acts.submit(_submit(episode_id, sb_id))
    assert submitted.artifact is None and submitted.reservation_id
    result = await acts.await_image(_await(episode_id, sb_id, submitted.reservation_id))
    assert result.reused is False

    artifact = parse_scene_image_artifact(await artifact_store.get_json(result.object_key))
    assert (artifact.width, artifact.height, artifact.scene_id) == (1080, 1920, "sb1")
    assert artifact.source_storyboard.artifact_id == sb_id
    media = await artifact_store.get_bytes(artifact.media.object_key)
    assert sha256_hex(media) == artifact.media.sha256
    assert artifact.generator.generation_profile_id == "fake-image-profile-v1"

    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.SCENE_IMAGE, "sb1"
        )
        row = await ProviderReservationRepository(session).get(submitted.reservation_id)
    assert meta is not None and meta.id == result.artifact_id and meta.scene_id == "sb1"
    assert row is not None and row.status is ReservationStatus.SPENT
    assert row.outcome_artifact_id == meta.id
    jobs = await _jobs(session_factory, episode_id)
    assert [(j.scene_id, j.status) for j in jobs] == [("sb1", JobStatus.SUCCEEDED)]

    # 2回目: 生成器を呼ばず、job は skipped
    again = await acts.submit(_submit(episode_id, sb_id, round=1))
    assert again.artifact is not None and again.artifact.reused
    assert again.artifact.artifact_id == result.artifact_id
    assert gen.submit_calls == 1
    jobs = await _jobs(session_factory, episode_id)
    assert [j.status for j in jobs] == [JobStatus.SUCCEEDED, JobStatus.SKIPPED]


async def test_await_retry_after_artifact_attached_returns_it(
    session_factory, artifact_store, tmp_path
) -> None:
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator()
    acts = make_activities(session_factory, artifact_store, gen, tmp_path)
    submitted = await acts.submit(_submit(episode_id, sb_id))
    first = await acts.await_image(_await(episode_id, sb_id, submitted.reservation_id))
    second = await acts.await_image(_await(episode_id, sb_id, submitted.reservation_id))
    assert second.artifact_id == first.artifact_id and gen.download_calls == 1


async def test_scenes_are_independent(session_factory, artifact_store, tmp_path) -> None:
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    acts = make_activities(session_factory, artifact_store, FakeImageGenerator(), tmp_path)
    for scene in ("sb1", "sb2"):
        s = await acts.submit(_submit(episode_id, sb_id, scene))
        await acts.await_image(_await(episode_id, sb_id, s.reservation_id, scene))
    async with session_factory() as session:
        current = await ArtifactMetadataRepository(session).list_current_by_type(
            episode_id, ArtifactType.SCENE_IMAGE
        )
    assert sorted(m.scene_id for m in current) == ["sb1", "sb2"]  # type: ignore[type-var]


async def test_invalid_media_fails_job_after_spending(
    session_factory, artifact_store, tmp_path
) -> None:
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator(output_size=(1920, 1080))  # 横長: 正規化で拒否
    acts = make_activities(session_factory, artifact_store, gen, tmp_path)
    s = await acts.submit(_submit(episode_id, sb_id))
    with pytest.raises(ApplicationError, match="^MediaValidationError"):
        await acts.await_image(_await(episode_id, sb_id, s.reservation_id))
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).get(s.reservation_id)
    assert row is not None and row.status is ReservationStatus.SPENT and row.raw_output_key
    jobs = await _jobs(session_factory, episode_id)
    assert jobs[0].status is JobStatus.RETRYABLE_FAILED


async def test_missing_and_mismatched_inputs(session_factory, artifact_store, tmp_path) -> None:
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    acts = make_activities(session_factory, artifact_store, FakeImageGenerator(), tmp_path)
    with pytest.raises(ApplicationError, match="^ProductionInputMissingError"):
        await acts.submit(_submit(episode_id, str(uuid.uuid4())))
    with pytest.raises(ApplicationError, match="^ProductionInputInvalidError"):
        await acts.submit(_submit(episode_id, sb_id, scene="sb9"))
    s = await acts.submit(_submit(episode_id, sb_id, scene="sb1"))
    with pytest.raises(ApplicationError, match="^ProductionInputInvalidError"):
        await acts.await_image(_await(episode_id, sb_id, s.reservation_id, scene="sb2"))


async def test_storyboard_sha_mismatch_is_invalid(
    session_factory, artifact_store, tmp_path
) -> None:
    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).get(sb_id)
    assert meta is not None
    artifact_store._objects[meta.object_key] = b'{"tampered": true}'  # type: ignore[attr-defined]
    acts = make_activities(session_factory, artifact_store, FakeImageGenerator(), tmp_path)
    with pytest.raises(ApplicationError, match="^ProductionInputInvalidError"):
        await acts.submit(_submit(episode_id, sb_id))


def test_activity_names_match_contracts(session_factory, artifact_store, tmp_path) -> None:
    from temporalio import activity

    from contracts.production_activities import IMAGE_AWAIT, IMAGE_SUBMIT

    acts = make_activities(session_factory, artifact_store, FakeImageGenerator(), tmp_path)
    names = {activity._Definition.must_from_callable(fn).name for fn in acts.all_activities()}  # type: ignore[attr-defined]
    assert names == {IMAGE_SUBMIT, IMAGE_AWAIT}


async def test_round_consumed_await_is_final_for_the_activity(
    session_factory, artifact_store, tmp_path
) -> None:
    from contracts.states import FailureClass
    from domain.errors import failure_class_from_type_name
    from domain.production.ports import JobFailed

    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)
    gen = FakeImageGenerator(pending_polls=0, fail_with=JobFailed("runner crashed"))
    acts = make_activities(session_factory, artifact_store, gen, tmp_path)
    submitted = await acts.submit(_submit(episode_id, sb_id))
    for _ in range(2):  # Temporal が retry しても同じ（ラウンドは確定済み）
        with pytest.raises(ApplicationError) as info:
            await acts.await_image(_await(episode_id, sb_id, submitted.reservation_id))
        assert info.value.type == "ProviderJobFailedError"
        assert info.value.non_retryable is True
        assert failure_class_from_type_name(info.value.type) is FailureClass.RETRYABLE
    assert gen.submit_calls == 1


async def test_store_outage_while_reading_inputs_is_transient(
    session_factory, artifact_store, tmp_path
) -> None:
    from urllib3.exceptions import ProtocolError

    episode_id, sb_id = await seed_storyboard(session_factory, artifact_store)

    async def broken_get_json(key):
        raise ProtocolError("Connection aborted.")

    artifact_store.get_json = broken_get_json
    acts = make_activities(session_factory, artifact_store, FakeImageGenerator(), tmp_path)
    with pytest.raises(ApplicationError) as info:
        await acts.submit(_submit(episode_id, sb_id))
    assert info.value.type == "TransientError" and info.value.non_retryable is False
