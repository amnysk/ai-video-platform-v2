"""ADR-0035 の連続シナリオ: 通常生成 → 1シーンだけ 422 → 代替案 → そのシーンだけ再生成 →
Render → private Upload。実 PostgreSQL + 実 MinIO + 実 Temporal で、provider・planner・描画・
投稿だけ fake。

本物の ``ProductionWorkflow`` / ``RenderWorkflow`` / ``UploadWorkflow`` を、この試験専用の
task queue に登録した worker で順に動かす。Temporal namespace はテスト専用（``avp-test``。
``tests/support/temporal.py``）なので、本番の worker（namespace ``default``）が拾うことはない。

fake provider の振る舞いは 2026-09-26/27 の実際の拒否に合わせる:

- 拒否は**動画の await（result）**で返り、対象は ``body.image_url``（入力画像）
- 判定は画像そのもので決まる（``ImageRejectingVideoGenerator``: 画像の sha256 で拒否）
- 画像生成器はプロンプトで絵が変わる（代替案で作り直した画像は別の画像になる）

確認すること（回数と台帳の両方で）:

1. 成功済みシーン（sb1〜sb5）の画像・動画の provider submit が増えない
2. 拒否されたシーン（sb6）は代替案で**画像から**作り直され、拒否された画像は再送されない
3. 代替案は1回、人物を主題にしない映像対象、根拠つきで保存される
4. Render と private Upload は1回ずつ。Upload を再実行しても二重投稿しない
5. 日次枠は1行のまま（再消費しない）
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.artifacts import (
    VisualSubject,
    parse_final_video,
    parse_production_manifest,
    parse_scene_video_artifact,
    parse_scene_visual_override_artifact,
)
from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    ProviderCall,
    RejectedInput,
)
from contracts.upload import parse_upload_receipt
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.production.ports import ImageRequest, VideoRequest
from infrastructure.db.models import Base, DailyEpisodeSlotRow, ProviderReservationRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    DailyEpisodeSlotRepository,
    EpisodeRepository,
    ProviderRejectionRepository,
)
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.workdir import WorkDirectory
from tests.integration.test_incident_recovery_e2e import SIX_SCENES, build_six_scene_storyboard
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.fake_render_engine import FakeFinalVideoProbe, FakeRenderEngine
from tests.support.fake_youtube import FakeVideoUploader
from tests.support.production import (
    FakeImageGenerator,
    FakeSceneAlternativePlanner,
    FakeVoiceGenerator,
    ImageRejectingVideoGenerator,
    make_png,
)
from tests.support.render_activity import PassingSourceProbe
from tests.support.storyboard import BUCKET, record_script
from tests.support.upload import CHANNEL_ID, TEST_CHUNK_BYTES
from workers.production.activities import ProductionActivities
from workers.production.scene_recovery_activities import SceneAlternativeActivities
from workers.production.workflows import ProductionWorkflow, ProductionWorkflowInput
from workers.production_image.activities import ImageProductionActivities
from workers.production_video.activities import VideoProductionActivities
from workers.production_voice.activities import VoiceActivities
from workers.render.activities import RenderActivities
from workers.render.run_inspector import TemporalWorkflowRunInspector
from workers.render.workflows import RenderWorkflow, RenderWorkflowInput
from workers.upload.activities import UploadActivities
from workers.upload.workflows import UploadWorkflow, UploadWorkflowInput

TEST_DATABASE_URL = require_test_database_url()
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT") or not TEMPORAL_ADDRESS,
        reason="TEST_DATABASE_URL (*_test), MINIO_ENDPOINT and TEMPORAL_ADDRESS must be set",
    ),
]

RUN_TIMEOUT_SECONDS = 180
REJECTED_SCENE = "sb6"
SLOT_DATE = date(2026, 9, 29)
_SCENE_TAG = re.compile(r"\[(sb\d+)\]")


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = f"scene_recovery_e2e_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def store():
    from tests.support.minio import connect_test_artifact_store

    return await connect_test_artifact_store()


@pytest_asyncio.fixture
async def client() -> Client:
    from tests.support.temporal import connect_test_client

    return await connect_test_client(TEMPORAL_ADDRESS)


def _scene_of(prompt: str) -> str:
    match = _SCENE_TAG.search(prompt)
    assert match, f"prompt does not name a scene: {prompt[:80]}"
    return match.group(1)


class PromptDependentImageGenerator(FakeImageGenerator):
    """プロンプトで絵が変わる画像生成器（代替案で作り直した画像は別の sha256 になる）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.submits_by_scene: dict[str, int] = {}

    async def submit(self, request: ImageRequest) -> Any:
        scene = _scene_of(request.prompt)
        self.submits_by_scene[scene] = self.submits_by_scene.get(scene, 0) + 1
        return await super().submit(request)

    def _render(self, request: ImageRequest) -> bytes:
        digest = hashlib.sha256(request.prompt.encode()).digest()
        return make_png(*self.output_size, color=(digest[0], digest[1], digest[2]))


class FirstImageOfSceneRejected(ImageRejectingVideoGenerator):
    """``scene`` の**最初に**渡された入力画像を provider が拒否する（肖像判定を模す）。"""

    def __init__(self, scene: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.scene = scene
        self.submits_by_scene: dict[str, int] = {}
        self.images_by_scene: dict[str, list[str]] = {}

    async def submit(self, request: VideoRequest) -> Any:
        scene = _scene_of(request.prompt)
        sha = hashlib.sha256(request.source_image).hexdigest()
        self.submits_by_scene[scene] = self.submits_by_scene.get(scene, 0) + 1
        self.images_by_scene.setdefault(scene, []).append(sha)
        if scene == self.scene and len(self.images_by_scene[scene]) == 1:
            self.reject_images.add(sha)
        return await super().submit(request)


async def _seed_daily_episode(factory, store) -> str:
    """日次枠を claim して作った Episode を storyboard_ready まで進める（本番の入口と同じ枠）。"""
    async with factory() as session:
        claim = await DailyEpisodeSlotRepository(session).claim(
            slot_date=SLOT_DATE,
            trigger_id=f"e2e-{uuid.uuid4().hex[:8]}",
            daily_limit=1,
            topic="Sekigahara",
        )
        await session.commit()
    assert claim.episode_id is not None
    episode_id = claim.episode_id
    async with factory() as session:
        episodes = EpisodeRepository(session)
        for event in (
            EpisodeEvent.WORKFLOW_STARTED,
            EpisodeEvent.SCRIPT_READY,
            EpisodeEvent.STAGE_ADMITTED,
            EpisodeEvent.STORYBOARD_READY,
        ):
            await episodes.apply_event(episode_id, event)
        await session.commit()
    script = await record_script(factory, store, episode_id)
    payload = build_six_scene_storyboard(
        episode_id, script_artifact_id=script.id, script_sha256=script.sha256
    )
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        artifact_object_key(episode_id, ArtifactType.STORYBOARD.value, digest), payload
    )
    async with factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.STORYBOARD,
            schema_version="1.0",
            bucket=BUCKET,
            object_key=put.key,
            sha256=digest,
            size_bytes=put.size,
            input_hash=digest,
        )
        await session.commit()
    return episode_id


async def _run(client, workflow_run, arg, *, workflow_id: str, queue: str, workers: list[Worker]):
    for worker in workers:
        await worker.__aenter__()
    try:
        handle = await client.start_workflow(workflow_run, arg, id=workflow_id, task_queue=queue)
        return await asyncio.wait_for(handle.result(), timeout=RUN_TIMEOUT_SECONDS)
    finally:
        for worker in reversed(workers):
            await worker.__aexit__(None, None, None)


async def _current(factory, episode_id: str, artifact_type: ArtifactType):
    async with factory() as session:
        return await ArtifactMetadataRepository(session).list_current_by_type(
            episode_id, artifact_type
        )


async def _status(factory, episode_id: str) -> EpisodeStatus:
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    assert episode is not None
    return episode.status


async def _fal_video_reservations_by_scene(factory, episode_id: str) -> dict[str, list[Any]]:
    async with factory() as session:
        rows = (
            await session.scalars(
                select(ProviderReservationRow).where(
                    ProviderReservationRow.episode_id == uuid.UUID(episode_id),
                    ProviderReservationRow.provider == ProviderCall.FAL_VIDEO.value,
                )
            )
        ).all()
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.scene_id or "", []).append(row)
    return grouped


async def _count(factory, provider: ProviderCall, episode_id: str) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ProviderReservationRow)
                .where(
                    ProviderReservationRow.episode_id == uuid.UUID(episode_id),
                    ProviderReservationRow.provider == provider.value,
                )
            )
            or 0
        )


async def test_only_the_rejected_scene_is_replanned_and_regenerated_through_private_upload(
    client,
    factory,
    store,
    tmp_path,
) -> None:
    episode_id = await _seed_daily_episode(factory, store)
    suffix = uuid.uuid4().hex[:10]
    q = {
        k: f"scene-recovery-e2e-{k}-{suffix}"
        for k in ("production", "image", "video", "voice", "alternative", "render", "upload")
    }
    image = PromptDependentImageGenerator(pending_polls=1, cost_usd=0.04)
    video = FirstImageOfSceneRejected(REJECTED_SCENE, pending_polls=1, cost_usd=0.97)
    planner = FakeSceneAlternativePlanner()
    runner = PaidJobRunner(
        session_factory=factory, store=store, workdir=WorkDirectory(tmp_path / "w", forbidden=())
    )
    probe = PillowAvMediaProbe()

    # ------------------------------------------------------------------ Production
    production_workers = [
        Worker(
            client,
            task_queue=q["production"],
            workflows=[ProductionWorkflow],
            activities=ProductionActivities(
                session_factory=factory, store=store, bucket=BUCKET
            ).all_activities(),
        ),
        Worker(
            client,
            task_queue=q["image"],
            activities=ImageProductionActivities(
                session_factory=factory,
                store=store,
                generator=image,
                probe=probe,
                runner=runner,
                bucket=BUCKET,
                poll_interval_seconds=0,
            ).all_activities(),  # fmt: skip
        ),
        Worker(
            client,
            task_queue=q["video"],
            activities=VideoProductionActivities(
                session_factory=factory,
                store=store,
                generator=video,
                probe=probe,
                runner=runner,
                bucket=BUCKET,
                poll_interval_seconds=0,
            ).all_activities(),  # fmt: skip
        ),
        Worker(
            client,
            task_queue=q["voice"],
            activities=VoiceActivities(
                session_factory=factory,
                store=store,
                generator=FakeVoiceGenerator(),
                probe=probe,
                workdir=WorkDirectory(tmp_path / "v", forbidden=()),
                bucket=BUCKET,
            ).all_activities(),  # fmt: skip
        ),
        Worker(
            client,
            task_queue=q["alternative"],
            activities=SceneAlternativeActivities(
                session_factory=factory, store=store, bucket=BUCKET, planner=planner
            ).all_activities(),
        ),
    ]
    produced = await _run(
        client,
        ProductionWorkflow.run,
        ProductionWorkflowInput(
            episode_id=episode_id,
            image_concurrency=1,
            image_task_queue=q["image"],
            video_task_queue=q["video"],
            voice_task_queue=q["voice"],
            scene_alternative_task_queue=q["alternative"],
        ),
        workflow_id=f"episode-{episode_id}-production",
        queue=q["production"],
        workers=production_workers,
    )

    assert produced.status == EpisodeStatus.ASSETS_READY.value, produced
    # (1) 成功済みシーンは画像・動画とも provider submit 1回ずつ（再生成・再課金なし）
    untouched = [s for s in SIX_SCENES if s != REJECTED_SCENE]
    assert {s: image.submits_by_scene[s] for s in untouched} == dict.fromkeys(untouched, 1)
    assert {s: video.submits_by_scene[s] for s in untouched} == dict.fromkeys(untouched, 1)
    # (2) 拒否されたシーンだけ、画像から作り直した（画像2回・動画2回。2回目は別の画像）
    assert image.submits_by_scene[REJECTED_SCENE] == 2
    assert video.submits_by_scene[REJECTED_SCENE] == 2
    rejected_image, replacement_image = video.images_by_scene[REJECTED_SCENE]
    assert rejected_image != replacement_image
    assert video.submitted_images.count(rejected_image) == 1  # 拒否された画像は再送しない

    reservations = await _fal_video_reservations_by_scene(factory, episode_id)
    assert {s: len(reservations[s]) for s in untouched} == dict.fromkeys(untouched, 1)
    assert len(reservations[REJECTED_SCENE]) == 2
    assert sorted(r.input_rejected_by_provider for r in reservations[REJECTED_SCENE]) == [
        False,
        True,
    ]
    async with factory() as session:
        (rejection,) = await ProviderRejectionRepository(session).list_for_episode(episode_id)
    assert rejection.scene_id == REJECTED_SCENE
    assert rejection.rejected_input is RejectedInput.IMAGE
    assert rejection.source_media_sha256 == rejected_image
    assert rejection.reason == "partner_validation_failed"

    # (3) 代替案は1回だけ計画され、人物を主題にしない対象・根拠つきで保存された
    assert len(planner.contexts) == 1
    assert await _count(factory, ProviderCall.CODEX_SCENE_ALTERNATIVE, episode_id) == 1
    (override_meta,) = await _current(factory, episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE)
    override = parse_scene_visual_override_artifact(await store.get_json(override_meta.object_key))
    assert override.scene_id == REJECTED_SCENE and override.revision == 1
    assert override.visual_subject not in {
        VisualSubject.NAMED_PERSON,
        VisualSubject.FIGURE_ANONYMOUS,
    }
    assert override.rationale and override.rejection_ids == [rejection.id]

    # manifest は作り直した sb6 の動画を指し、他シーンは最初の動画のまま
    videos = {m.scene_id: m for m in await _current(factory, episode_id, ArtifactType.SCENE_VIDEO)}
    assert set(videos) == set(SIX_SCENES)
    sb6_video = parse_scene_video_artifact(await store.get_json(videos[REJECTED_SCENE].object_key))
    (sb6_image,) = [
        m
        for m in await _current(factory, episode_id, ArtifactType.SCENE_IMAGE)
        if m.scene_id == REJECTED_SCENE
    ]
    assert sb6_video.source_image.artifact_id == sb6_image.id  # 作り直した画像から作った動画
    (manifest_meta,) = await _current(factory, episode_id, ArtifactType.PRODUCTION_MANIFEST)
    manifest = parse_production_manifest(await store.get_json(manifest_meta.object_key))
    assert {s.scene_id: s.video.artifact_id for s in manifest.scenes} == {
        s: videos[s].id for s in SIX_SCENES
    }

    # ------------------------------------------------------------------ Render
    font = tmp_path / "font.ttc"
    font.write_bytes(b"integration font")
    final_probe = FakeFinalVideoProbe()
    engine = FakeRenderEngine(on_render=lambda r: setattr(final_probe, "plan", r.plan))
    render = RenderActivities(
        session_factory=factory, store=store, bucket=BUCKET,
        workdir=WorkDirectory(tmp_path / "render", forbidden=()), engine=engine,
        probe=final_probe, source_probe=PassingSourceProbe(), font_path=font,
        font_sha256=sha256_hex(b"integration font"), render_timeout_seconds=60,
        min_free_bytes=0, run_inspector=TemporalWorkflowRunInspector(client),
    )  # fmt: skip
    rendered = await _run(
        client,
        RenderWorkflow.run,
        RenderWorkflowInput(
            episode_id=episode_id,
            render_profile_id="shorts_vertical",
            render_task_queue=f"{q['render']}-media",
        ),
        workflow_id=f"episode-{episode_id}-render",
        queue=q["render"],
        workers=[
            Worker(
                client,
                task_queue=q["render"],
                workflows=[RenderWorkflow],
                activities=render.state_activities(),
            ),
            Worker(
                client,
                task_queue=f"{q['render']}-media",
                activities=render.media_activities(),
                max_concurrent_activities=1,
            ),
        ],
    )
    assert rendered.status == EpisodeStatus.RENDER_READY.value, rendered
    assert engine.completed == 1
    (final_meta,) = await _current(factory, episode_id, ArtifactType.FINAL_VIDEO)
    final = parse_final_video(await store.get_json(final_meta.object_key))
    assert final.source_production_manifest.artifact_id == manifest_meta.id  # type: ignore[union-attr]

    # ------------------------------------------------------------------ private Upload
    uploader = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
    upload = UploadActivities(
        session_factory=factory, store=store, bucket=BUCKET,
        workdir=WorkDirectory(tmp_path / "upload", forbidden=()), uploader=uploader,
        channel_id=CHANNEL_ID, chunk_bytes=TEST_CHUNK_BYTES, marker_lookup_attempts=2,
        marker_lookup_delay_seconds=0.0, transient_backoff_seconds=0.0,
        expiry_confirm_delay_seconds=0.0,
    )  # fmt: skip

    def upload_workers() -> list[Worker]:
        return [
            Worker(
                client,
                task_queue=q["upload"],
                workflows=[UploadWorkflow],
                activities=upload.state_activities(),
            ),
            Worker(
                client,
                task_queue=f"{q['upload']}-media",
                activities=upload.media_activities(),
                max_concurrent_activities=1,
            ),
        ]

    uploaded = await _run(
        client,
        UploadWorkflow.run,
        UploadWorkflowInput(episode_id=episode_id, upload_task_queue=f"{q['upload']}-media"),
        workflow_id=f"episode-{episode_id}-upload",
        queue=q["upload"],
        workers=upload_workers(),
    )
    assert uploaded.status == EpisodeStatus.UPLOADED.value, uploaded
    assert await _status(factory, episode_id) is EpisodeStatus.UPLOADED
    assert uploader.videos_created == 1
    (receipt_meta,) = await _current(factory, episode_id, ArtifactType.UPLOAD_RECEIPT)
    receipt = parse_upload_receipt(await store.get_json(receipt_meta.object_key))
    assert receipt.privacy_status == "private"
    assert receipt.source_final_video.artifact_id == final_meta.id

    # (4) Upload をもう一度走らせても二重投稿しない
    await _run(
        client,
        UploadWorkflow.run,
        UploadWorkflowInput(episode_id=episode_id, upload_task_queue=f"{q['upload']}-media"),
        workflow_id=f"episode-{episode_id}-upload-again",
        queue=q["upload"],
        workers=upload_workers(),
    )
    assert uploader.videos_created == 1
    assert await _count(factory, ProviderCall.YOUTUBE_UPLOAD, episode_id) == 1

    # (5) 日次枠は1行のまま（復旧・Render・Upload で再消費しない）
    async with factory() as session:
        slots = (await session.scalars(select(DailyEpisodeSlotRow))).all()
    assert [str(s.episode_id) for s in slots] == [episode_id]
    # 費用: 成功済みシーンの追加 submit はゼロ。追加分は sb6 の画像1回・動画1回だけ
    assert sum(image.submits_by_scene.values()) == len(SIX_SCENES) + 1
    assert sum(video.submits_by_scene.values()) == len(SIX_SCENES) + 1
