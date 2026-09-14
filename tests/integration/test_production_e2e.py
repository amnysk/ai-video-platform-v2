"""Phase 4 の縦切り E2E（ADR-0017）: 本物の PostgreSQL + MinIO + Temporal。

ProductionWorkflow を**本物の Activity クラス**（状態系 / 画像 / 音声 / 動画）で動かす。
生成器だけ fake（INV-18: 有料 provider は呼ばない）。PaidJobRunner / ArtifactStore /
WorkDirectory は本物。DB は一時スキーマ、task queue はテストごとに一意
（本物の worker と取り合わない）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.artifacts import (
    STORYBOARD_ARTIFACT_SCHEMA_VERSION,
    parse_production_manifest,
    parse_scene_image_artifact,
    parse_scene_video_artifact,
    parse_scene_voice_artifact,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType, ReservationStatus
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import ProviderSubmitAmbiguousError
from domain.production.manifest import check_manifest_coverage
from domain.production.ports import JobStatus as ProviderJobStatus
from domain.production.ports import ProviderJobRef
from infrastructure.db.models import Base, ProviderReservationRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.storage.artifact_store import readback_sha256
from infrastructure.workdir import WorkDirectory
from tests.support.production import (
    FakeImageGenerator,
    FakeVideoGenerator,
    FakeVoiceGenerator,
    sample_storyboard,
)
from tests.support.storyboard import BUCKET, record_script
from workers.production.activities import ProductionActivities
from workers.production.workflows import (
    ProductionWorkflow,
    ProductionWorkflowInput,
    ProductionWorkflowResult,
)
from workers.production_image.activities import ImageProductionActivities
from workers.production_video.activities import VideoProductionActivities
from workers.production_voice.activities import VoiceActivities

DATABASE_URL = os.environ.get("DATABASE_URL")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL
        or "postgresql" not in DATABASE_URL
        or not os.environ.get("MINIO_ENDPOINT")
        or not TEMPORAL_ADDRESS,
        reason="DATABASE_URL (PostgreSQL), MINIO_ENDPOINT and TEMPORAL_ADDRESS must be set",
    ),
]

RUN_TIMEOUT_SECONDS = 180

#: sample_storyboard の visual_description（画像/動画プロンプトに入る）→ scene_id
SCENE_BY_DESCRIPTION = {
    "土器のクローズアップ": "sb1",
    "炉に火を入れる": "sb2",
    "煮炊きする再現": "sb3",
    "集落の俯瞰図": "sb4",
}
STORYBOARD_SCENES = ["sb1", "sb2", "sb3", "sb4"]
SCRIPT_SCENES = ["s1", "s2", "s3"]


def _scene_of(prompt: str) -> str:
    for description, scene in SCENE_BY_DESCRIPTION.items():
        if description in prompt:
            return scene
    raise AssertionError(f"prompt does not name a known scene: {prompt[:80]}")


class _SceneAware:
    """fake 生成器にシーン単位の故障注入と submit 回数の記録を足す mixin。"""

    ambiguous_scenes: set[str]
    crash_first_poll_scenes: set[str]
    submits_by_scene: dict[str, int]
    _jobs: dict[str, Any]

    def _init_scene_aware(
        self, *, ambiguous: set[str] | None = None, crash_first_poll: set[str] | None = None
    ) -> None:
        self.ambiguous_scenes = set(ambiguous or ())
        self.crash_first_poll_scenes = set(crash_first_poll or ())
        self.submits_by_scene = {}
        self._crashed: set[str] = set()

    def _before_submit(self, request: Any) -> None:
        scene = _scene_of(request.prompt)
        self.submits_by_scene[scene] = self.submits_by_scene.get(scene, 0) + 1
        if scene in self.ambiguous_scenes:
            raise ProviderSubmitAmbiguousError(f"fake: submit outcome unknown for {scene}")

    def _before_poll(self, ref: ProviderJobRef) -> None:
        scene = _scene_of(self._jobs[ref].request.prompt)
        if scene in self.crash_first_poll_scenes and scene not in self._crashed:
            self._crashed.add(scene)
            raise RuntimeError(f"fake: worker crashed while polling {scene}")


class SceneImageGenerator(_SceneAware, FakeImageGenerator):
    def __init__(self, **kwargs: Any) -> None:
        ambiguous = kwargs.pop("ambiguous", None)
        crash = kwargs.pop("crash_first_poll", None)
        super().__init__(**kwargs)
        self._init_scene_aware(ambiguous=ambiguous, crash_first_poll=crash)

    async def submit(self, request: Any) -> ProviderJobRef:
        self._before_submit(request)
        return await super().submit(request)

    async def poll(self, ref: ProviderJobRef) -> ProviderJobStatus:
        self._before_poll(ref)
        return await super().poll(ref)


class SceneVideoGenerator(_SceneAware, FakeVideoGenerator):
    def __init__(self, **kwargs: Any) -> None:
        ambiguous = kwargs.pop("ambiguous", None)
        crash = kwargs.pop("crash_first_poll", None)
        super().__init__(**kwargs)
        self._init_scene_aware(ambiguous=ambiguous, crash_first_poll=crash)

    async def submit(self, request: Any) -> ProviderJobRef:
        self._before_submit(request)
        return await super().submit(request)

    async def poll(self, ref: ProviderJobRef) -> ProviderJobStatus:
        self._before_poll(ref)
        return await super().poll(ref)


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = f"prod_e2e_{uuid.uuid4().hex[:12]}"
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


@pytest_asyncio.fixture
async def client() -> Client:
    return await Client.connect(TEMPORAL_ADDRESS or "", namespace="default")


async def _seed(factory, store) -> str:
    """script + storyboard Artifact を持つ ``storyboard_ready`` の Episode。"""
    async with factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="production e2e")
        for event in (
            EpisodeEvent.WORKFLOW_STARTED,
            EpisodeEvent.SCRIPT_READY,
            EpisodeEvent.STAGE_ADMITTED,
            EpisodeEvent.STORYBOARD_READY,
        ):
            await episodes.apply_event(episode.id, event)
        await session.commit()
    script = await record_script(factory, store, episode.id)
    payload = sample_storyboard(
        episode.id, script_artifact_id=script.id, script_sha256=script.sha256
    ).model_dump(mode="json")
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        artifact_object_key(episode.id, ArtifactType.STORYBOARD.value, digest), payload
    )
    async with factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=episode.id,
            artifact_type=ArtifactType.STORYBOARD,
            schema_version=STORYBOARD_ARTIFACT_SCHEMA_VERSION,
            bucket=BUCKET,
            object_key=put.key,
            sha256=digest,
            size_bytes=put.size,
            input_hash=digest,
        )
        await session.commit()
    return episode.id


@dataclass
class Stack:
    client: Client
    factory: Any
    store: Any
    tmp_path: Path
    image: SceneImageGenerator
    video: SceneVideoGenerator
    voice: FakeVoiceGenerator

    async def run(self, episode_id: str) -> ProductionWorkflowResult:
        """API の POST と同じ workflow id で起動し、完了を待つ（本物の Activity を4 queue で）。"""
        suffix = uuid.uuid4().hex[:10]
        queues = {k: f"p4e2e-{k}-{suffix}" for k in ("production", "image", "video", "voice")}
        f, s = self.factory, self.store
        runner = PaidJobRunner(
            session_factory=f, store=s, workdir=WorkDirectory(self.tmp_path / "work", forbidden=())
        )
        probe = PillowAvMediaProbe()
        state = ProductionActivities(session_factory=f, store=s, bucket=BUCKET)
        image = ImageProductionActivities(
            session_factory=f,
            store=s,
            generator=self.image,
            probe=probe,
            runner=runner,
            bucket=BUCKET,
            poll_interval_seconds=0,
        )
        video = VideoProductionActivities(
            session_factory=f,
            store=s,
            generator=self.video,
            probe=probe,
            runner=runner,
            bucket=BUCKET,
            poll_interval_seconds=0,
        )
        voice = VoiceActivities(
            session_factory=f,
            store=s,
            generator=self.voice,
            probe=probe,
            workdir=WorkDirectory(self.tmp_path / "voice-work", forbidden=()),
            bucket=BUCKET,
        )
        workers = [
            Worker(
                self.client,
                task_queue=queues["production"],
                workflows=[ProductionWorkflow],
                activities=state.all_activities(),
            ),
            Worker(self.client, task_queue=queues["image"], activities=image.all_activities()),
            Worker(self.client, task_queue=queues["video"], activities=video.all_activities()),
            Worker(self.client, task_queue=queues["voice"], activities=voice.all_activities()),
        ]
        for w in workers:
            await w.__aenter__()
        try:
            handle = await self.client.start_workflow(
                ProductionWorkflow.run,
                ProductionWorkflowInput(
                    episode_id=episode_id,
                    image_concurrency=2,
                    image_task_queue=queues["image"],
                    video_task_queue=queues["video"],
                    voice_task_queue=queues["voice"],
                ),
                # API と同じ id（完了済みの id は再利用できる）。スキーマごとに Episode id は一意。
                id=f"episode-{episode_id}-production",
                task_queue=queues["production"],
            )
            try:
                return await asyncio.wait_for(handle.result(), timeout=RUN_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                desc = await handle.describe()
                pending = [
                    (p.activity_type.name, p.attempt, p.last_failure.message)
                    for p in desc.raw_description.pending_activities
                ]
                raise AssertionError(f"workflow did not finish: pending={pending}") from exc
        finally:
            for w in reversed(workers):
                await w.__aexit__(None, None, None)


def _stack(client, factory, store, tmp_path, **image_video_faults: Any) -> Stack:
    return Stack(
        client=client,
        factory=factory,
        store=store,
        tmp_path=tmp_path,
        image=SceneImageGenerator(
            pending_polls=1,
            cost_usd=0.04,
            ambiguous=image_video_faults.get("image_ambiguous"),
            crash_first_poll=image_video_faults.get("image_crash_first_poll"),
        ),
        video=SceneVideoGenerator(
            pending_polls=1,
            cost_usd=0.2419,
            ambiguous=image_video_faults.get("video_ambiguous"),
            crash_first_poll=image_video_faults.get("video_crash_first_poll"),
        ),
        voice=FakeVoiceGenerator(),
    )


async def _status(factory, episode_id: str) -> EpisodeStatus:
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    assert episode is not None
    return episode.status


async def _reservations(factory, episode_id: str) -> list[ProviderReservationRow]:
    async with factory() as session:
        stmt = select(ProviderReservationRow).where(
            ProviderReservationRow.episode_id == uuid.UUID(episode_id)
        )
        return list((await session.scalars(stmt)).all())


async def _current(factory, episode_id: str, artifact_type: ArtifactType):
    async with factory() as session:
        return await ArtifactMetadataRepository(session).list_current_by_type(
            episode_id, artifact_type
        )


# --------------------------------------------------------------------------- scenarios


async def test_production_end_to_end_then_rerun_reuses_everything(
    client, factory, store, tmp_path
) -> None:
    episode_id = await _seed(factory, store)
    stack = _stack(client, factory, store, tmp_path)

    result = await stack.run(episode_id)

    assert result.status == EpisodeStatus.ASSETS_READY.value, result
    assert await _status(factory, episode_id) is EpisodeStatus.ASSETS_READY

    images = {
        m.scene_id or "": m for m in await _current(factory, episode_id, ArtifactType.SCENE_IMAGE)
    }
    videos = {
        m.scene_id or "": m for m in await _current(factory, episode_id, ArtifactType.SCENE_VIDEO)
    }
    voices = {
        m.scene_id or "": m for m in await _current(factory, episode_id, ArtifactType.SCENE_VOICE)
    }
    assert sorted(images) == STORYBOARD_SCENES
    assert sorted(videos) == STORYBOARD_SCENES
    assert sorted(voices) == SCRIPT_SCENES

    # メディア: Artifact JSON の sha と、メディア本体を MinIO から読み戻した sha
    for meta, parse in (
        *((m, parse_scene_image_artifact) for m in images.values()),
        *((m, parse_scene_video_artifact) for m in videos.values()),
        *((m, parse_scene_voice_artifact) for m in voices.values()),
    ):
        payload = await store.get_json(meta.object_key)
        assert sha256_hex(canonical_json_bytes(payload)) == meta.sha256
        artifact = parse(payload)
        assert await readback_sha256(store, artifact.media.object_key) == artifact.media.sha256
    for scene, meta in videos.items():
        video_artifact = parse_scene_video_artifact(await store.get_json(meta.object_key))
        assert video_artifact.source_image.artifact_id == images[scene].id

    # マニフェスト: 現行1件、カバレッジが通る
    (manifest_meta,) = await _current(factory, episode_id, ArtifactType.PRODUCTION_MANIFEST)
    assert result.manifest is not None and result.manifest.artifact_id == manifest_meta.id
    manifest_payload = await store.get_json(manifest_meta.object_key)
    assert sha256_hex(canonical_json_bytes(manifest_payload)) == manifest_meta.sha256
    manifest = parse_production_manifest(manifest_payload)
    (sb_meta,) = await _current(factory, episode_id, ArtifactType.STORYBOARD)
    (script_meta,) = await _current(factory, episode_id, ArtifactType.SCRIPT)
    check_manifest_coverage(
        manifest,
        parse_storyboard_artifact(await store.get_json(sb_meta.object_key)),
        parse_script_artifact(await store.get_json(script_meta.object_key)),
        storyboard_sha256=sb_meta.sha256,
        script_sha256=script_meta.sha256,
    )
    assert {s.scene_id: s.video.artifact_id for s in manifest.scenes} == {
        k: v.id for k, v in videos.items()
    }

    # 台帳: 画像/動画のシーンごとに spent の予約1件（参照・見積もり付き）。音声は載らない
    rows = await _reservations(factory, episode_id)
    by_provider: dict[str, list[ProviderReservationRow]] = {}
    for row in rows:
        by_provider.setdefault(row.provider, []).append(row)
    assert sorted(by_provider) == ["fal_image", "fal_video"]
    for provider, cost in (("fal_image", Decimal("0.0400")), ("fal_video", Decimal("0.2419"))):
        provider_rows = by_provider[provider]
        assert sorted(r.scene_id for r in provider_rows) == STORYBOARD_SCENES  # type: ignore[type-var]
        for row in provider_rows:
            assert row.status == ReservationStatus.SPENT.value
            assert row.provider_job_ref and row.estimated_cost_usd == cost
            assert row.outcome_artifact_id is not None
    assert stack.image.submit_calls == len(STORYBOARD_SCENES)
    assert stack.video.submit_calls == len(STORYBOARD_SCENES)
    assert stack.voice.calls == len(SCRIPT_SCENES)

    # ---- 2回目（assets_ready から POST 相当）: 新しい submit / 合成なし、job は skipped
    again = await stack.run(episode_id)

    assert again.status == EpisodeStatus.ASSETS_READY.value, again
    assert stack.image.submit_calls == len(STORYBOARD_SCENES)
    assert stack.video.submit_calls == len(STORYBOARD_SCENES)
    assert stack.voice.calls == len(SCRIPT_SCENES)
    assert again.manifest is not None and again.manifest.artifact_id == manifest_meta.id
    (manifest_again,) = await _current(factory, episode_id, ArtifactType.PRODUCTION_MANIFEST)
    assert manifest_again.id == manifest_meta.id
    assert len(await _reservations(factory, episode_id)) == len(rows)
    async with factory() as session:
        jobs = await JobRepository(session).list_for_episode(episode_id)
    for job_type, count in (
        (JobType.PRODUCE_SCENE_IMAGE, 4),
        (JobType.PRODUCE_SCENE_VIDEO, 4),
        (JobType.PRODUCE_SCENE_VOICE, 3),
    ):
        statuses = sorted(j.status.value for j in jobs if j.type is job_type)
        assert statuses == sorted(
            [JobStatus.SUCCEEDED.value] * count + [JobStatus.SKIPPED.value] * count
        ), (job_type, statuses)


async def test_crash_on_first_poll_resumes_await_without_resubmitting(
    client, factory, store, tmp_path
) -> None:
    """await Activity が poll 中に落ちても Temporal の retry が同じ参照を待ち直す（INV-15）。"""
    episode_id = await _seed(factory, store)
    stack = _stack(
        client, factory, store, tmp_path, image_crash_first_poll={"sb2"},
        video_crash_first_poll={"sb3"},
    )  # fmt: skip

    result = await stack.run(episode_id)

    assert result.status == EpisodeStatus.ASSETS_READY.value, result
    assert stack.image.submits_by_scene == dict.fromkeys(STORYBOARD_SCENES, 1)
    assert stack.video.submits_by_scene == dict.fromkeys(STORYBOARD_SCENES, 1)
    rows = await _reservations(factory, episode_id)
    assert len(rows) == 2 * len(STORYBOARD_SCENES)
    assert all(r.status == ReservationStatus.SPENT.value for r in rows)


async def _ambiguous_run(
    client, factory, store, tmp_path
) -> tuple[str, Stack, ProductionWorkflowResult]:
    episode_id = await _seed(factory, store)
    stack = _stack(client, factory, store, tmp_path, image_ambiguous={"sb4"})
    result = await stack.run(episode_id)
    return episode_id, stack, result


async def test_ambiguous_submit_blocks_without_duplicate_submit(
    client, factory, store, tmp_path
) -> None:
    """曖昧な submit は needs_input → blocked。予約は参照なしの reserved で残り、再 submit なし。"""
    episode_id, stack, result = await _ambiguous_run(client, factory, store, tmp_path)

    assert result.status == EpisodeStatus.BLOCKED.value, result
    assert result.failure_class == "needs_input"
    assert await _status(factory, episode_id) is EpisodeStatus.BLOCKED
    assert stack.image.submits_by_scene.get("sb4") == 1
    assert "sb4" not in stack.video.submits_by_scene
    sb4 = [
        r for r in await _reservations(factory, episode_id)
        if r.scene_id == "sb4" and r.provider == "fal_image"
    ]  # fmt: skip
    assert len(sb4) == 1
    assert sb4[0].status == ReservationStatus.RESERVED.value
    assert sb4[0].dispatched_at is not None and sb4[0].provider_job_ref is None
    assert await _current(factory, episode_id, ArtifactType.PRODUCTION_MANIFEST) == []
    async with factory() as session:
        jobs = await JobRepository(session).list_for_episode(episode_id)
    # ADR-0017 §8: record_failure は workflow 所有の job だけを閉じる。シーン job は media worker の
    # 所有で、非終端のまま残り次回の実行で find_open により再利用される（再実行テストで確認）。
    assert not any(
        j.type is JobType.ASSEMBLE_PRODUCTION and j.status in {JobStatus.QUEUED, JobStatus.RUNNING}
        for j in jobs
    ), [(j.type, j.scene_id, j.status) for j in jobs]


async def test_rerun_from_blocked_is_admitted_and_does_not_resubmit_ambiguous_scene(
    client, factory, store, tmp_path
) -> None:
    """blocked からの再実行: admit され、完了済みシーンは再利用、曖昧な予約は再 submit しない。"""
    episode_id, stack, _ = await _ambiguous_run(client, factory, store, tmp_path)
    submits_before = dict(stack.image.submits_by_scene)

    stack.image.ambiguous_scenes.clear()
    again = await stack.run(episode_id)

    assert again.admitted is True, again
    assert stack.image.submits_by_scene.get("sb4") == submits_before.get("sb4") == 1
    for scene, count in submits_before.items():
        assert stack.image.submits_by_scene[scene] == count
