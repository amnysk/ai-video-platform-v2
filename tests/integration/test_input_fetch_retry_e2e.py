"""ADR-0035 (8) の fault injection: 入力の取得失敗・分類不能・代替案の上限（連続シナリオ）。

実 PostgreSQL（一時スキーマ）+ 実 MinIO（``artifacts-test``）+ 実 Temporal（namespace
``avp-test``）。provider・planner・描画・投稿は fake。本物の ``ProductionWorkflow`` /
``RenderWorkflow`` / ``UploadWorkflow`` を試験専用 queue で動かす
（``test_scene_rejection_recovery_e2e.py`` と同じ組み方）。

- 一時的な取得失敗（file_download_error）→ 上げ直して再試行: そのシーンの動画だけ2回目で
  成功、新しい URL、他シーン・画像は不変、planner 無し、Render・private Upload 1回、日次枠1行
- 取得失敗 → 再試行も取得失敗: そのシーンの submit は2回で止まる（構造化した停止理由）、
  planner 無し、resume しても追加 submit 無し
- 分類不能の 422（型なし）: planner を呼ばずに止まる
- 代替案の上限（設定値を小さく）: 上限で止まり、それ以上 planner も生成もしない

内容方針の拒否 → 代替案 → 完成は ``test_scene_rejection_recovery_e2e.py`` が担う。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    JobStatus,
    ProviderCall,
    RejectionCategory,
)
from contracts.upload import parse_upload_receipt
from domain.artifact.hashing import sha256_hex
from domain.errors import ProviderRejection
from domain.production.ports import JobFailed, JobSucceeded, ProviderJobRef, VideoRequest
from domain.production.ports import JobStatus as ProviderJobStatus
from infrastructure.db.models import Base, DailyEpisodeSlotRow, JobRow
from infrastructure.db.repositories import ProviderRejectionRepository
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.workdir import WorkDirectory
from tests.integration.test_incident_recovery_e2e import SIX_SCENES
from tests.integration.test_scene_rejection_recovery_e2e import (
    REJECTED_SCENE,
    PromptDependentImageGenerator,
    _count,
    _current,
    _fal_video_reservations_by_scene,
    _run,
    _scene_of,
    _seed_daily_episode,
    _status,
)
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.fake_render_engine import FakeFinalVideoProbe, FakeRenderEngine
from tests.support.fake_youtube import FakeVideoUploader
from tests.support.production import (
    LIKENESS_REJECTION,
    FakeSceneAlternativePlanner,
    FakeVideoGenerator,
    FakeVoiceGenerator,
)
from tests.support.render_activity import PassingSourceProbe
from tests.support.storyboard import BUCKET
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

UNREACHABLE = ProviderRejection(
    types=("file_download_error",),
    locs=("body.image_url",),
    message="Failed to download the file. Please check if the URL is accessible and try again.",
    http_status=422,
    category=RejectionCategory.INPUT_UNREACHABLE,
)
UNCLASSIFIED = ProviderRejection(types=(), locs=("body.image_url",), message="m", http_status=422)


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = f"fetch_retry_e2e_{uuid.uuid4().hex[:12]}"
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


# --------------------------------------------------------------------------- fakes


class ScriptedVideo(FakeVideoGenerator):
    """シーンごとに submit の結果を台本どおり返す動画生成器。

    ``outcomes[scene]`` を submit のたびに先頭から消費する: ``"fetch"`` = 入力を取得できない
    （file_download_error）、``"policy"`` = 内容方針の拒否、``"unknown"`` = 分類不能の 422、
    それ以外（尽きた後も）= 成功。``prepare`` は本物と同じく毎回アップロードし直し、新しい
    URL を作る（同じ壊れた URL を使い回していないかを ``urls_by_scene`` で見る）。
    """

    def __init__(self, outcomes: dict[str, list[str]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.outcomes = {scene: list(items) for scene, items in outcomes.items()}
        self.submits_by_scene: dict[str, int] = {}
        self.uploads_by_scene: dict[str, int] = {}
        self.urls_by_scene: dict[str, list[str]] = {}
        self._pending_url: dict[str, str] = {}
        self._fail: dict[str, str] = {}

    async def prepare(self, request: VideoRequest) -> VideoRequest:
        scene = _scene_of(request.prompt)
        n = self.uploads_by_scene.get(scene, 0) + 1
        self.uploads_by_scene[scene] = n
        self._pending_url[scene] = f"https://cdn.test/{scene}/{uuid.uuid4().hex}.png"
        return await super().prepare(request)

    async def submit(self, request: VideoRequest) -> ProviderJobRef:
        scene = _scene_of(request.prompt)
        self.submits_by_scene[scene] = self.submits_by_scene.get(scene, 0) + 1
        self.urls_by_scene.setdefault(scene, []).append(self._pending_url[scene])
        ref = await super().submit(request)
        queue = self.outcomes.get(scene) or []
        outcome = queue.pop(0) if queue else "ok"
        if outcome != "ok":
            self._fail[ref] = outcome
        return ref

    async def poll(self, ref: ProviderJobRef) -> ProviderJobStatus:
        status = await super().poll(ref)
        outcome = self._fail.get(ref)
        if outcome is None or not isinstance(status, JobSucceeded):
            return status
        if outcome == "fetch":
            return JobFailed(
                message="fal job failed: HTTP 422 types=['file_download_error']",
                input_unreachable=True,
                rejection=UNREACHABLE,
            )
        if outcome == "policy":
            return JobFailed(
                message="fal job failed: HTTP 422 types=['content_policy_violation']",
                rejected=True,
                rejection=LIKENESS_REJECTION,
            )
        return JobFailed(
            message="fal job failed: HTTP 422 types=[]", rejected=True, rejection=UNCLASSIFIED
        )


# --------------------------------------------------------------------------- stack


async def _produce(
    client: Client,
    factory: Any,
    store: Any,
    tmp_path: Any,
    episode_id: str,
    *,
    image: PromptDependentImageGenerator,
    video: ScriptedVideo,
    planner: FakeSceneAlternativePlanner,
    **limits: Any,
):
    suffix = uuid.uuid4().hex[:10]
    q = {k: f"fetch-e2e-{k}-{suffix}" for k in ("prod", "image", "video", "voice", "alt")}
    runner = PaidJobRunner(
        session_factory=factory, store=store, workdir=WorkDirectory(tmp_path / "w", forbidden=())
    )
    probe = PillowAvMediaProbe()
    workers = [
        Worker(
            client,
            task_queue=q["prod"],
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
            task_queue=q["alt"],
            activities=SceneAlternativeActivities(
                session_factory=factory, store=store, bucket=BUCKET, planner=planner, **limits
            ).all_activities(),
        ),
    ]
    return await _run(
        client,
        ProductionWorkflow.run,
        ProductionWorkflowInput(
            episode_id=episode_id,
            image_concurrency=1,
            image_task_queue=q["image"],
            video_task_queue=q["video"],
            voice_task_queue=q["voice"],
            scene_alternative_task_queue=q["alt"],
        ),
        # 入場（admission）は Episode の production workflow id に結び付く。resume も同じ id
        workflow_id=f"episode-{episode_id}-production",
        queue=q["prod"],
        workers=workers,
    )


async def _render_and_upload(client: Client, factory: Any, store: Any, tmp_path: Any, episode_id):
    suffix = uuid.uuid4().hex[:10]
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
    rq = f"fetch-e2e-render-{suffix}"
    rendered = await _run(
        client,
        RenderWorkflow.run,
        RenderWorkflowInput(
            episode_id=episode_id,
            render_profile_id="shorts_vertical",
            render_task_queue=f"{rq}-media",
        ),
        workflow_id=f"episode-{episode_id}-render",
        queue=rq,
        workers=[
            Worker(
                client,
                task_queue=rq,
                workflows=[RenderWorkflow],
                activities=render.state_activities(),
            ),
            Worker(
                client,
                task_queue=f"{rq}-media",
                activities=render.media_activities(),
                max_concurrent_activities=1,
            ),
        ],  # fmt: skip
    )
    uploader = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
    upload = UploadActivities(
        session_factory=factory, store=store, bucket=BUCKET,
        workdir=WorkDirectory(tmp_path / "upload", forbidden=()), uploader=uploader,
        channel_id=CHANNEL_ID, chunk_bytes=TEST_CHUNK_BYTES, marker_lookup_attempts=2,
        marker_lookup_delay_seconds=0.0, transient_backoff_seconds=0.0,
        expiry_confirm_delay_seconds=0.0,
    )  # fmt: skip
    uq = f"fetch-e2e-upload-{suffix}"

    async def run_upload(tag: str):
        return await _run(
            client,
            UploadWorkflow.run,
            UploadWorkflowInput(episode_id=episode_id, upload_task_queue=f"{uq}-media"),
            workflow_id=f"episode-{episode_id}-upload-{tag}",
            queue=uq,
            workers=[
                Worker(
                    client,
                    task_queue=uq,
                    workflows=[UploadWorkflow],
                    activities=upload.state_activities(),
                ),
                Worker(
                    client,
                    task_queue=f"{uq}-media",
                    activities=upload.media_activities(),
                    max_concurrent_activities=1,
                ),
            ],  # fmt: skip
        )

    uploaded = await run_upload("1")
    again = await run_upload("2")
    return rendered, engine, uploaded, again, uploader


async def _slots(factory) -> list[str]:
    async with factory() as session:
        return [str(s.episode_id) for s in (await session.scalars(select(DailyEpisodeSlotRow)))]


async def _failed_jobs(factory, episode_id: str) -> list[JobRow]:
    """終わった失敗 job（停止理由 ``error_summary`` は行にだけある）。作成順。"""
    async with factory() as session:
        rows = await session.scalars(
            select(JobRow)
            .where(
                JobRow.episode_id == uuid.UUID(episode_id),
                JobRow.status == JobStatus.TERMINAL_FAILED.value,
            )
            .order_by(JobRow.created_at)
        )
        return list(rows)


def _gens(outcomes: dict[str, list[str]]):
    return (
        PromptDependentImageGenerator(pending_polls=1, cost_usd=0.04),
        ScriptedVideo(outcomes, pending_polls=1, cost_usd=0.97),
        FakeSceneAlternativePlanner(),
    )


# ==================================================================== シナリオ


async def test_transient_input_fetch_failure_is_retried_once_with_a_new_url_through_upload(
    client, factory, store, tmp_path
) -> None:
    """2026-09-29（Episode 54392404 sb5）型: 取得失敗 → 上げ直して1回だけ再試行 → 完成。"""
    episode_id = await _seed_daily_episode(factory, store)
    image, video, planner = _gens({REJECTED_SCENE: ["fetch"]})

    produced = await _produce(
        client, factory, store, tmp_path, episode_id, image=image, video=video, planner=planner
    )

    assert produced.status == EpisodeStatus.ASSETS_READY.value, produced
    untouched = [s for s in SIX_SCENES if s != REJECTED_SCENE]
    # 他シーンは画像・動画とも1回、失敗シーンも画像は作り直さない（内容の問題ではない）
    assert image.submits_by_scene == dict.fromkeys(SIX_SCENES, 1)
    assert {s: video.submits_by_scene[s] for s in untouched} == dict.fromkeys(untouched, 1)
    assert video.submits_by_scene[REJECTED_SCENE] == 2
    first_url, retry_url = video.urls_by_scene[REJECTED_SCENE]
    assert first_url != retry_url  # 同じ壊れた URL を使わない（上げ直した）
    assert video.uploads_by_scene[REJECTED_SCENE] == 2
    assert planner.contexts == []  # 取得失敗では代替案を頼まない
    assert await _count(factory, ProviderCall.CODEX_SCENE_ALTERNATIVE, episode_id) == 0

    reservations = await _fal_video_reservations_by_scene(factory, episode_id)
    sb6 = sorted(reservations[REJECTED_SCENE], key=lambda r: r.round)
    assert [r.round for r in sb6] == [1, 2]
    assert [r.input_rejected_by_provider for r in sb6] == [False, False]
    assert all(r.estimated_cost_usd is not None for r in sb6)  # 再試行の追加費用も台帳に残る
    async with factory() as session:
        (row,) = await ProviderRejectionRepository(session).list_for_episode(episode_id)
    assert row.category is RejectionCategory.INPUT_UNREACHABLE
    assert row.scene_id == REJECTED_SCENE

    rendered, engine, uploaded, again, uploader = await _render_and_upload(
        client, factory, store, tmp_path, episode_id
    )
    assert rendered.status == EpisodeStatus.RENDER_READY.value and engine.completed == 1
    assert uploaded.status == EpisodeStatus.UPLOADED.value
    assert uploader.videos_created == 1  # 2回目の Upload 実行で二重投稿しない
    (receipt_meta,) = await _current(factory, episode_id, ArtifactType.UPLOAD_RECEIPT)
    receipt = parse_upload_receipt(await store.get_json(receipt_meta.object_key))
    assert receipt.privacy_status == "private"
    assert await _count(factory, ProviderCall.YOUTUBE_UPLOAD, episode_id) == 1
    assert await _slots(factory) == [episode_id]


async def test_second_input_fetch_failure_stops_and_resume_does_not_submit_again(
    client, factory, store, tmp_path
) -> None:
    """取得失敗 → 再試行も取得失敗 → 構造化した理由で停止。resume でも追加の有料 submit は無い。"""
    episode_id = await _seed_daily_episode(factory, store)
    image, video, planner = _gens({REJECTED_SCENE: ["fetch", "fetch"]})

    produced = await _produce(
        client, factory, store, tmp_path, episode_id, image=image, video=video, planner=planner
    )

    assert produced.status == EpisodeStatus.BLOCKED.value, produced
    assert produced.failure_class == "needs_input"
    assert video.submits_by_scene[REJECTED_SCENE] == 2
    assert planner.contexts == []
    async with factory() as session:
        rows = await ProviderRejectionRepository(session).list_for_episode(episode_id)
    assert [r.category for r in rows] == [RejectionCategory.INPUT_UNREACHABLE] * 2
    failed = [j for j in await _failed_jobs(factory, episode_id) if j.scene_id == REJECTED_SCENE]
    assert failed and "ProviderInputFetchError" in (failed[-1].error_summary or "")

    # resume（同じ Episode の Production を再実行）: 台帳が上限を覚えているので送らない
    resumed = await _produce(
        client, factory, store, tmp_path, episode_id, image=image, video=video, planner=planner
    )
    assert resumed.admitted, "resume が入場できず何もしていない（空振りの合格を防ぐ）"
    assert resumed.status == EpisodeStatus.BLOCKED.value
    assert video.submits_by_scene[REJECTED_SCENE] == 2
    assert image.submits_by_scene == dict.fromkeys(SIX_SCENES, 1)
    failed = [j for j in await _failed_jobs(factory, episode_id) if j.scene_id == REJECTED_SCENE]
    assert "ProviderInputFetchRetryExhaustedError" in (failed[-1].error_summary or "")
    assert await _status(factory, episode_id) is EpisodeStatus.BLOCKED


async def test_unclassified_422_stops_without_asking_the_planner(
    client, factory, store, tmp_path
) -> None:
    """型の無い 422 は内容方針の拒否と決めつけない: 代替案を頼まず止まる。"""
    episode_id = await _seed_daily_episode(factory, store)
    image, video, planner = _gens({REJECTED_SCENE: ["unknown"]})

    produced = await _produce(
        client, factory, store, tmp_path, episode_id, image=image, video=video, planner=planner
    )

    assert produced.status == EpisodeStatus.BLOCKED.value, produced
    assert planner.contexts == []
    assert await _count(factory, ProviderCall.CODEX_SCENE_ALTERNATIVE, episode_id) == 0
    assert video.submits_by_scene[REJECTED_SCENE] == 1
    assert image.submits_by_scene == dict.fromkeys(SIX_SCENES, 1)
    async with factory() as session:
        (row,) = await ProviderRejectionRepository(session).list_for_episode(episode_id)
    assert row.category is RejectionCategory.UNKNOWN


async def test_alternative_limit_from_settings_stops_further_generation(
    client, factory, store, tmp_path
) -> None:
    """設定値の上限（1シーン1回）に達したら、それ以上の計画も生成もしない。"""
    episode_id = await _seed_daily_episode(factory, store)
    # 元の画像も、代替案で作り直した画像も内容方針で拒否される
    image, video, planner = _gens({REJECTED_SCENE: ["policy", "policy", "policy"]})

    produced = await _produce(
        client,
        factory,
        store,
        tmp_path,
        episode_id,
        image=image,
        video=video,
        planner=planner,
        max_alternatives_per_scene=1,
    )

    assert produced.status == EpisodeStatus.BLOCKED.value, produced
    assert len(planner.contexts) == 1
    (override,) = await _current(factory, episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE)
    assert override.scene_id == REJECTED_SCENE
    # 元の画像 + 代替案の画像 = 2回、動画も2回で止まる（3回目は作らない）
    assert image.submits_by_scene[REJECTED_SCENE] == 2
    assert video.submits_by_scene[REJECTED_SCENE] == 2
    untouched = [s for s in SIX_SCENES if s != REJECTED_SCENE]
    assert {s: image.submits_by_scene[s] for s in untouched} == dict.fromkeys(untouched, 1)
    assert {s: video.submits_by_scene[s] for s in untouched} == dict.fromkeys(untouched, 1)
    assert await _status(factory, episode_id) is EpisodeStatus.BLOCKED
