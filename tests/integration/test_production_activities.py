"""Production の状態系 Activity を実PostgreSQL + 実MinIO で検査する（ADR-0017）。

共有 DB を壊さないよう一時スキーマに閉じ込める。MinIO のキーは Episode ごとに一意。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.artifacts import (
    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
    STORYBOARD_ARTIFACT_SCHEMA_VERSION,
    build_scene_image_artifact,
    build_scene_video_artifact,
    build_scene_voice_artifact,
)
from contracts.production_activities import (
    ProductionAdmitRequest,
    ProductionAssembleRequest,
    ProductionMarkReadyRequest,
    ProductionPlanRequest,
    ProductionRecordFailureRequest,
)
from contracts.states import ArtifactType, EpisodeStatus, FailureClass, JobStatus, JobType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    ProductionInputInvalidError,
    ProductionInputMissingError,
    TransientError,
)
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from tests.support.production import GENERATOR, media_descriptor, sample_storyboard
from tests.support.storyboard import BUCKET, record_script
from workers.production.activities import ProductionActivities, admission_token

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or "postgresql" not in DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="DATABASE_URL (PostgreSQL) and MINIO_ENDPOINT must be set (docker compose core)",
)

WF, RUN = "episode-x-production", "run-1"
TOKEN = admission_token(WF, RUN)


@pytest_asyncio.fixture
async def factory():
    schema = f"prod_act_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def store():
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    s = MinioArtifactStore.from_settings(Settings())
    await s.ensure_bucket()
    return s


class FakeInspector:
    """run id → 閉じているか。登録の無い run の問い合わせは失敗させる。"""

    def __init__(self) -> None:
        self.closed: dict[tuple[str, str], bool | BaseException] = {}
        self.calls: list[tuple[str, str]] = []

    async def is_closed(self, workflow_id: str, run_id: str) -> bool:
        self.calls.append((workflow_id, run_id))
        value = self.closed[(workflow_id, run_id)]
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
def inspector() -> FakeInspector:
    return FakeInspector()


@pytest.fixture
def acts(factory, store, inspector) -> ProductionActivities:
    return ProductionActivities(
        session_factory=factory, store=store, bucket=BUCKET, run_inspector=inspector
    )


# --------------------------------------------------------------------------- 下ごしらえ


async def _episode_at(factory, events: list[EpisodeEvent]) -> str:
    async with factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="production")
        for event in events:
            await episodes.apply_event(episode.id, event)
        await session.commit()
        return episode.id


TO_STORYBOARD_READY = [
    EpisodeEvent.WORKFLOW_STARTED,
    EpisodeEvent.SCRIPT_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.STORYBOARD_READY,
]


async def _put_record(
    factory,
    store,
    episode_id: str,
    artifact_type: ArtifactType,
    payload: dict[str, Any],
    *,
    scene_id: str | None = None,
    schema_version: str = PRODUCTION_ARTIFACT_SCHEMA_VERSION,
) -> ArtifactMetadata:
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        artifact_object_key(episode_id, artifact_type.value, digest, scene_id), payload
    )
    async with factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            bucket=BUCKET,
            object_key=put.key,
            sha256=digest,
            input_hash=digest,
            scene_id=scene_id,
        )
        await session.commit()
    return meta


class Seeded:
    def __init__(self, episode_id: str, script: ArtifactMetadata, storyboard: ArtifactMetadata):
        self.episode_id = episode_id
        self.script = script
        self.storyboard = storyboard


async def _seed(factory, store, *, script_sha: str | None = None) -> Seeded:
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    script = await record_script(factory, store, episode_id)
    storyboard_model = sample_storyboard(
        episode_id, script_artifact_id=script.id, script_sha256=script_sha or script.sha256
    )
    storyboard = await _put_record(
        factory,
        store,
        episode_id,
        ArtifactType.STORYBOARD,
        storyboard_model.model_dump(mode="json"),
        schema_version=STORYBOARD_ARTIFACT_SCHEMA_VERSION,
    )
    return Seeded(episode_id, script, storyboard)


def _src(meta: ArtifactMetadata) -> dict[str, str]:
    return {"artifact_id": meta.id, "sha256": meta.sha256, "schema_version": "1.0"}


async def _seed_media(
    factory,
    store,
    seeded: Seeded,
    *,
    skip_image: str | None = None,
    storyboard_ref: dict[str, str] | None = None,
    variant: int = 0,
) -> None:
    sb_ref = storyboard_ref or _src(seeded.storyboard)
    sha = lambda i: f"{(variant * 7 + i) % 10}" * 64  # noqa: E731
    voices = {"s1": ["sb1"], "s2": ["sb2", "sb3"], "s3": ["sb4"]}
    for n, scene in enumerate(["sb1", "sb2", "sb3", "sb4"], start=1):
        if scene == skip_image:
            continue
        image = await _put_record(
            factory,
            store,
            seeded.episode_id,
            ArtifactType.SCENE_IMAGE,
            build_scene_image_artifact(
                episode_id=seeded.episode_id,
                source_storyboard=sb_ref,
                scene_id=scene,
                media=media_descriptor("image/png", sha256=sha(n)),
                width=1080,
                height=1920,
                generator=GENERATOR,
            ),
            scene_id=scene,
        )
        await _put_record(
            factory,
            store,
            seeded.episode_id,
            ArtifactType.SCENE_VIDEO,
            build_scene_video_artifact(
                episode_id=seeded.episode_id,
                source_storyboard=sb_ref,
                scene_id=scene,
                source_image={"artifact_id": image.id, "sha256": image.sha256},
                media=media_descriptor("video/mp4", sha256=sha(n + 1)),
                duration_ms=4000,
                requested_duration_ms=4000,
                width=1080,
                height=1920,
                fps_millis=24000,
                generator=GENERATOR,
            ),
            scene_id=scene,
        )
    for sid, scenes in voices.items():
        await _put_record(
            factory,
            store,
            seeded.episode_id,
            ArtifactType.SCENE_VOICE,
            build_scene_voice_artifact(
                episode_id=seeded.episode_id,
                source_storyboard=sb_ref,
                source_script=_src(seeded.script),
                script_scene_id=sid,
                storyboard_scene_ids=scenes,
                language="ja",
                voice_id="v",
                media=media_descriptor("audio/wav", sha256=sha(5)),
                duration_ms=1000,
                sample_rate_hz=22050,
                channels=1,
                generator=GENERATOR,
            ),
            scene_id=sid,
        )


async def _status(factory, episode_id: str) -> EpisodeStatus:
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
        assert episode is not None
        return episode.status


async def _admit(acts, episode_id: str, run: str = RUN):
    return await acts.admit(
        ProductionAdmitRequest(episode_id=episode_id, workflow_id=WF, run_id=run)
    )


# --------------------------------------------------------------------------- admit


async def test_admit_from_storyboard_ready_writes_token(acts, factory, inspector) -> None:
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    result = await _admit(acts, episode_id)
    assert result.admitted and result.status == EpisodeStatus.IN_PROGRESS.value
    async with factory() as session:
        assert await EpisodeRepository(session).get_workflow_id(episode_id) == TOKEN
    # 同じトークンの再実行は通す、走っている別の run は通さない
    inspector.closed[(WF, RUN)] = False
    assert (await _admit(acts, episode_id)).admitted
    assert not (await _admit(acts, episode_id, run="other")).admitted


async def test_admit_from_assets_ready(acts, factory) -> None:
    episode_id = await _episode_at(
        factory, [*TO_STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.ASSETS_READY]
    )
    assert (await _admit(acts, episode_id)).admitted
    assert await _status(factory, episode_id) is EpisodeStatus.IN_PROGRESS


@pytest.mark.parametrize(
    "events",
    [
        [],
        [EpisodeEvent.WORKFLOW_STARTED, EpisodeEvent.SCRIPT_READY],
        [*TO_STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.NEEDS_INPUT_FAILURE],
        [EpisodeEvent.WORKFLOW_STARTED],  # in_progress, 他の工程が所有
    ],
)
async def test_admit_rejects_other_states_without_writes(acts, factory, events) -> None:
    episode_id = await _episode_at(factory, events)
    before = await _status(factory, episode_id)
    result = await _admit(acts, episode_id)
    assert result.admitted is False
    assert await _status(factory, episode_id) is before
    async with factory() as session:
        assert await EpisodeRepository(session).get_workflow_id(episode_id) is None


async def _token(factory, episode_id: str) -> str | None:
    async with factory() as session:
        return await EpisodeRepository(session).get_workflow_id(episode_id)


@pytest.mark.parametrize(
    ("cls", "exhausted", "stopped"),
    [
        (FailureClass.RETRYABLE, False, EpisodeStatus.NEEDS_WORK),  # RETRY_ADMITTED
        (FailureClass.RETRYABLE, True, EpisodeStatus.BLOCKED),  # RESUMED
        (FailureClass.NEEDS_INPUT, False, EpisodeStatus.BLOCKED),
    ],
)
async def test_admit_resumes_after_production_failure(acts, factory, cls, exhausted, stopped):
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    await _admit(acts, episode_id)
    await acts.record_failure(_failure(episode_id, cls, exhausted=exhausted))
    assert await _status(factory, episode_id) is stopped

    result = await _admit(acts, episode_id, run="run-2")

    assert result.admitted and result.status == EpisodeStatus.IN_PROGRESS.value
    assert await _token(factory, episode_id) == admission_token(WF, "run-2")


async def test_admit_does_not_resume_failed_or_other_stage_failures(acts, factory) -> None:
    failed = await _episode_at(factory, TO_STORYBOARD_READY)
    await _admit(acts, failed)
    await acts.record_failure(_failure(failed, FailureClass.PERMANENT))
    assert not (await _admit(acts, failed, run="run-2")).admitted
    assert await _status(factory, failed) is EpisodeStatus.FAILED

    # 他工程（storyboard）の失敗で blocked: production から再開すると上流を飛ばす
    other = await _episode_at(
        factory,
        [*TO_STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.NEEDS_INPUT_FAILURE],
    )
    async with factory() as session:
        await EpisodeRepository(session).set_workflow_id(other, "episode-x-storyboard:run-9")
        await session.commit()
    assert not (await _admit(acts, other)).admitted
    assert await _status(factory, other) is EpisodeStatus.BLOCKED


async def test_admit_takes_over_only_a_closed_run(acts, factory, inspector) -> None:
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    await _admit(acts, episode_id)  # run-1 が in_progress のまま（record_failure 前に死んだ等）

    inspector.closed[(WF, RUN)] = False
    assert not (await _admit(acts, episode_id, run="run-2")).admitted, "走っている run"
    assert await _token(factory, episode_id) == TOKEN

    inspector.closed[(WF, RUN)] = TransientError("describe failed")
    with pytest.raises(TransientError):
        await _admit(acts, episode_id, run="run-2")
    assert await _token(factory, episode_id) == TOKEN

    inspector.closed[(WF, RUN)] = True
    taken = await _admit(acts, episode_id, run="run-2")
    assert taken.admitted and taken.status == EpisodeStatus.IN_PROGRESS.value
    assert await _token(factory, episode_id) == admission_token(WF, "run-2")
    assert await _status(factory, episode_id) is EpisodeStatus.IN_PROGRESS


async def test_admit_never_inspects_another_stages_token(acts, factory, inspector) -> None:
    episode_id = await _episode_at(factory, [EpisodeEvent.WORKFLOW_STARTED])
    async with factory() as session:
        await EpisodeRepository(session).set_workflow_id(episode_id, "episode-x-storyboard:r")
        await session.commit()
    assert not (await _admit(acts, episode_id)).admitted
    assert inspector.calls == []


async def test_admit_unknown_episode(acts) -> None:
    result = await _admit(acts, str(uuid.uuid4()))
    assert result.admitted is False and result.status == ""


# --------------------------------------------------------------------------- plan


async def test_plan_lists_scene_work_in_order(acts, factory, store) -> None:
    seeded = await _seed(factory, store)
    plan = await acts.plan(ProductionPlanRequest(seeded.episode_id, WF, RUN))
    assert plan.storyboard_artifact_id == seeded.storyboard.id
    assert plan.script_sha256 == seeded.script.sha256
    assert [i.scene_id for i in plan.images] == ["sb1", "sb2", "sb3", "sb4"]
    assert [v.requested_duration_ms for v in plan.videos] == [8000, 4000, 5000, 8000]
    assert [(v.script_scene_id, v.storyboard_scene_ids) for v in plan.voices] == [
        ("s1", ["sb1"]),
        ("s2", ["sb2", "sb3"]),
        ("s3", ["sb4"]),
    ]


async def test_plan_rejects_script_sha_mismatch(acts, factory, store) -> None:
    seeded = await _seed(factory, store, script_sha="f" * 64)
    with pytest.raises(ProductionInputInvalidError):
        await acts.plan(ProductionPlanRequest(seeded.episode_id, WF, RUN))


async def test_plan_without_storyboard_is_missing(acts, factory) -> None:
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    with pytest.raises(ProductionInputMissingError):
        await acts.plan(ProductionPlanRequest(episode_id, WF, RUN))


# --------------------------------------------------------------------------- assemble


def _assemble_req(seeded: Seeded) -> ProductionAssembleRequest:
    return ProductionAssembleRequest(
        episode_id=seeded.episode_id,
        workflow_id=WF,
        run_id=RUN,
        storyboard_artifact_id=seeded.storyboard.id,
        script_artifact_id=seeded.script.id,
    )


async def _jobs(factory, episode_id: str):
    async with factory() as session:
        return [j for j in await JobRepository(session).list_for_episode(episode_id)]


async def test_assemble_builds_manifest_and_reuses_identical(acts, factory, store) -> None:
    seeded = await _seed(factory, store)
    await _seed_media(factory, store, seeded)

    first = await acts.assemble_manifest(_assemble_req(seeded))
    assert first.reused is False
    manifest = await store.get_json(first.object_key)
    assert [s["scene_id"] for s in manifest["scenes"]] == ["sb1", "sb2", "sb3", "sb4"]
    assert manifest["source_storyboard"]["sha256"] == seeded.storyboard.sha256

    second = await acts.assemble_manifest(_assemble_req(seeded))
    assert second.reused is True and second.artifact_id == first.artifact_id
    statuses = [
        j.status
        for j in await _jobs(factory, seeded.episode_id)
        if j.type is JobType.ASSEMBLE_PRODUCTION
    ]
    assert statuses == [JobStatus.SUCCEEDED, JobStatus.SKIPPED]


async def test_assemble_with_missing_scene_is_needs_input(acts, factory, store) -> None:
    seeded = await _seed(factory, store)
    await _seed_media(factory, store, seeded, skip_image="sb3")
    with pytest.raises(ProductionInputMissingError):
        await acts.assemble_manifest(_assemble_req(seeded))
    (job,) = await _jobs(factory, seeded.episode_id)
    assert job.status is JobStatus.TERMINAL_FAILED
    assert job.failure_class is FailureClass.NEEDS_INPUT


async def _rerecord_image(factory, store, seeded: Seeded, scene: str, media_sha: str) -> None:
    await _put_record(
        factory,
        store,
        seeded.episode_id,
        ArtifactType.SCENE_IMAGE,
        build_scene_image_artifact(
            episode_id=seeded.episode_id,
            source_storyboard=_src(seeded.storyboard),
            scene_id=scene,
            media=media_descriptor("image/png", sha256=media_sha),
            width=1080,
            height=1920,
            generator={**GENERATOR, "generation_profile_id": "re-recorded-v2"},
        ),
        scene_id=scene,
    )


async def test_assemble_matches_video_source_image_by_media_sha(acts, factory, store) -> None:
    """同じメディアで画像 Artifact が記録し直されても動画は有効（input_hash は media sha）。"""
    seeded = await _seed(factory, store)
    await _seed_media(factory, store, seeded)
    await _rerecord_image(factory, store, seeded, "sb2", "2" * 64)  # sb2 の元メディアと同じ

    manifest = await acts.assemble_manifest(_assemble_req(seeded))
    assert manifest.reused is False


async def test_assemble_rejects_video_made_from_other_image_media(acts, factory, store) -> None:
    seeded = await _seed(factory, store)
    await _seed_media(factory, store, seeded)
    await _rerecord_image(factory, store, seeded, "sb2", "e" * 64)

    with pytest.raises(ProductionInputInvalidError, match="image media"):
        await acts.assemble_manifest(_assemble_req(seeded))


async def test_assemble_rejects_media_from_another_storyboard(acts, factory, store) -> None:
    seeded = await _seed(factory, store)
    other = {"artifact_id": str(uuid.uuid4()), "sha256": "e" * 64, "schema_version": "1.0"}
    await _seed_media(factory, store, seeded, storyboard_ref=other)
    with pytest.raises(ProductionInputInvalidError):
        await acts.assemble_manifest(_assemble_req(seeded))


# ------------------------------------------------------------ mark_ready / record_failure


async def test_mark_ready_requires_the_token(acts, factory) -> None:
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    await _admit(acts, episode_id)

    refused = await acts.mark_ready(ProductionMarkReadyRequest(episode_id, WF, "stale"))
    assert refused.owned is False
    assert await _status(factory, episode_id) is EpisodeStatus.IN_PROGRESS

    ready = await acts.mark_ready(ProductionMarkReadyRequest(episode_id, WF, RUN))
    assert ready.status == EpisodeStatus.ASSETS_READY.value
    again = await acts.mark_ready(ProductionMarkReadyRequest(episode_id, WF, RUN))
    assert again.status == EpisodeStatus.ASSETS_READY.value and again.owned


def _failure(episode_id: str, cls: FailureClass, *, run: str = RUN, exhausted: bool = False):
    return ProductionRecordFailureRequest(
        episode_id=episode_id,
        workflow_id=WF,
        run_id=run,
        failure_class=cls.value,
        error_summary="boom",
        retry_exhausted=exhausted,
    )


async def test_record_failure_requires_the_token(acts, factory) -> None:
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    await _admit(acts, episode_id)
    outcome = await acts.record_failure(_failure(episode_id, FailureClass.NEEDS_INPUT, run="x"))
    assert outcome.owned is False
    assert await _status(factory, episode_id) is EpisodeStatus.IN_PROGRESS


@pytest.mark.parametrize(
    ("cls", "exhausted", "expected"),
    [
        (FailureClass.NEEDS_INPUT, False, EpisodeStatus.BLOCKED),
        (FailureClass.PERMANENT, False, EpisodeStatus.FAILED),
        (FailureClass.RETRYABLE, False, EpisodeStatus.NEEDS_WORK),
        (FailureClass.RETRYABLE, True, EpisodeStatus.BLOCKED),
    ],
)
async def test_record_failure_maps_class_to_episode_state(acts, factory, cls, exhausted, expected):
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    await _admit(acts, episode_id)
    outcome = await acts.record_failure(_failure(episode_id, cls, exhausted=exhausted))
    assert outcome.episode_status == expected.value
    assert await _status(factory, episode_id) is expected
    # 再実行で二重遷移しない
    again = await acts.record_failure(_failure(episode_id, cls, exhausted=exhausted))
    assert again.episode_status == expected.value


@pytest.mark.parametrize("cls", [FailureClass.NEEDS_INPUT, FailureClass.PERMANENT])
async def test_record_failure_touches_only_workflow_owned_jobs(acts, factory, cls) -> None:
    """PRODUCE_SCENE_* はメディア worker の所有物。cancel 中も worker が書くので触らない。"""
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)
    await _admit(acts, episode_id)
    async with factory() as session:
        jobs = JobRepository(session)
        image = await jobs.create(
            episode_id=episode_id, type=JobType.PRODUCE_SCENE_IMAGE, scene_id="sb1"
        )
        await jobs.start(image.id)
        video = await jobs.create(
            episode_id=episode_id, type=JobType.PRODUCE_SCENE_VIDEO, scene_id="sb1"
        )
        assemble = await jobs.create(episode_id=episode_id, type=JobType.ASSEMBLE_PRODUCTION)
        await jobs.start(assemble.id)
        await session.commit()

    await acts.record_failure(_failure(episode_id, cls))

    by_id = {j.id: j for j in await _jobs(factory, episode_id)}
    assert by_id[image.id].status is JobStatus.RUNNING
    assert by_id[video.id].status is JobStatus.QUEUED
    expected = (
        JobStatus.TERMINAL_FAILED if cls is FailureClass.PERMANENT else JobStatus.RETRYABLE_FAILED
    )
    assert by_id[assemble.id].status is expected
    if expected is JobStatus.RETRYABLE_FAILED:
        async with factory() as session:  # 次の実行で start できる
            await JobRepository(session).start(assemble.id)
            await session.commit()
