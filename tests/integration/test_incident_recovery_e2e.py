"""2026-09-22 型の事故を1本のシナリオとして再現する（ADR-0030/0031/0032/0033）。

実 PostgreSQL + MinIO + Temporal。生成器・アップローダは fake（INV-18）。

**安全上の設計判断（実装時に確定）**: 本物の ``ProductionWorkflow`` を
``EpisodePipelineWorkflow`` 経由で走らせる統合試験は、`test_episode_resume.py` が既に
文書化した理由（``PipelineOptions`` は各工程の state workflow の名前・queue しか
差し替えられず、内部のメディア Activity queue は固定のため、本物の docker-compose
worker と衝突しうる）により安全でない。したがって本ファイルは2つの独立したテスト群に
分ける。両方とも「1つの物語」の異なる区間を、それぞれ既にこのrepoで安全性が確立している
パターンで検査する（新しい危険なハイブリッドは作らない）。

- **区間A（本物の ProductionWorkflow・専用queue、``test_production_e2e.py`` と同じ型）**:
  fal の 403 分類・診断（ADR-0030）、共有障害の抑止ゲート、Artifact 完全性検証
  （ADR-0033）を本物の Activity で検査する。
- **区間B（``test_episode_resume.py`` の Stack を再利用・fake工程）**: 統一再開
  （ADR-0032）と watchdog（ADR-0031）の「Temporal completed ≠ 成功」判定を、本物の
  Temporal + 実DBに対して検査する。

以下は既存の（本ファイルが変更しない）テストが既に検査済みで、ここでは重複させない:

- 未照合予約（曖昧な submit）は rerun で再送されない:
  ``test_production_e2e.py::test_ambiguous_submit_blocks_without_duplicate_submit`` /
  ``::test_rerun_from_blocked_is_admitted_and_does_not_resubmit_ambiguous_scene``
- 日次枠を消費しない・同時2回のresumeが1本しか起動しない・UPLOADS_PAUSEDを迂回しない:
  ``test_episode_resume.py`` の (a)(b)(d)(c)(e)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.pipeline import EPISODE_PIPELINE_WORKFLOW
from contracts.production_activities import (
    AUTH_INCIDENT_SUPPRESSION_THRESHOLD,
)
from contracts.schedule_guard import AnomalyKind, WatchdogCheckRequest
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import ProviderUnavailableError
from domain.production.ports import ImageRequest, VideoRequest
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    OperationalAnomalyRepository,
    ProviderAuthIncidentRepository,
)
from infrastructure.production.paid_job import PaidJobRunner, Submitted
from infrastructure.temporal.watchdog import TemporalPipelineOutcomeChecker, run_daily_watchdog
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.production import FakeImageGenerator, FakeVideoGenerator
from tests.support.schedule_control import FakeScheduleControl
from tests.support.storyboard import BUCKET, record_script
from workers.production.activities import ProductionActivities
from workers.production.workflows import ProductionWorkflow, ProductionWorkflowInput
from workers.production_image.activities import ImageProductionActivities
from workers.production_video.activities import VideoProductionActivities
from workers.production_voice.activities import VoiceActivities

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
SIX_SCENES = ["sb1", "sb2", "sb3", "sb4", "sb5", "sb6"]


def _scene_of(prompt: str) -> str:
    for scene in SIX_SCENES:
        if prompt.startswith(f"[{scene}]"):
            return scene
    raise AssertionError(f"prompt does not name a known scene: {prompt[:80]}")


def build_six_scene_storyboard(
    episode_id: str, *, script_artifact_id: str, script_sha256: str
) -> dict[str, Any]:
    """sb1〜sb6 の storyboard。2026-09-22 型の事故と同じ形（scene 6 だけが動画で落ちる）。"""
    from contracts.artifacts import build_storyboard_artifact, parse_storyboard_artifact

    scenes = [
        {
            "scene_id": scene,
            "order": i,
            "script_scene_id": "s1" if i <= 3 else ("s2" if i <= 5 else "s3"),
            "start_ms": (i - 1) * 4000,
            "duration_ms": 4000,
            "visual_kind": "broll",
            "visual_description": f"[{scene}] 縄文の情景 {i}",
            "camera_movement": None,
        }
        for i, scene in enumerate(SIX_SCENES, start=1)
    ]
    return parse_storyboard_artifact(
        build_storyboard_artifact(
            episode_id=episode_id,
            source_script={
                "artifact_id": script_artifact_id,
                "sha256": script_sha256,
                "schema_version": "1.0",
            },
            scenes=scenes,
            total_duration_ms=4000 * len(SIX_SCENES),
            metadata={
                "generator": "fake",
                "generator_model": "fake",
                "generation_spec_id": "spec-6scene",
            },
        )
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- 故障注入 fake


class FaultyVideoGenerator(FakeVideoGenerator):
    """特定シーンの ``prepare()``（fal storage token 取得に相当。予約の前）で 403 を注入できる。

    ADR-0030: 実際の ``ProviderUnavailableError(http_status=403)`` と同じ型・同じ形の例外を
    投げる（``fal_storage.py`` の分類コードを再実装しない。ここは Activity の外側の fake なので、
    実際に分類コードを通すのは ``PaidJobRunner.submit`` が呼ぶ本物の
    ``infrastructure.production.activity_errors`` 側 ── その入口に本物と同じ例外型を渡す）。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_scenes: set[str] = set()
        self.submits_by_scene: dict[str, int] = {}
        self.prepares_by_scene: dict[str, int] = {}

    async def prepare(self, request: VideoRequest) -> VideoRequest:
        scene = _scene_of(request.prompt)
        self.prepares_by_scene[scene] = self.prepares_by_scene.get(scene, 0) + 1
        if scene in self.fail_scenes:
            raise ProviderUnavailableError(
                f"fal storage token refused: HTTP 403 for {scene}", http_status=403
            )
        return await super().prepare(request)

    async def submit(self, request: VideoRequest) -> Any:
        scene = _scene_of(request.prompt)
        self.submits_by_scene[scene] = self.submits_by_scene.get(scene, 0) + 1
        return await super().submit(request)


class SceneAwareImageGenerator(FakeImageGenerator):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.submits_by_scene: dict[str, int] = {}

    async def submit(self, request: ImageRequest) -> Any:
        scene = _scene_of(request.prompt)
        self.submits_by_scene[scene] = self.submits_by_scene.get(scene, 0) + 1
        return await super().submit(request)


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = f"incident_e2e_{uuid.uuid4().hex[:12]}"
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
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    s = MinioArtifactStore.from_settings(Settings())
    await s.ensure_bucket()
    return s


@pytest_asyncio.fixture
async def client() -> Client:
    return await Client.connect(TEMPORAL_ADDRESS or "", namespace="default")


async def _seed(factory, store) -> str:
    async with factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="incident recovery e2e")
        for event in (
            EpisodeEvent.WORKFLOW_STARTED,
            EpisodeEvent.SCRIPT_READY,
            EpisodeEvent.STAGE_ADMITTED,
            EpisodeEvent.STORYBOARD_READY,
        ):
            await episodes.apply_event(episode.id, event)
        await session.commit()
    script = await record_script(factory, store, episode.id)
    payload = build_six_scene_storyboard(
        episode.id, script_artifact_id=script.id, script_sha256=script.sha256
    )
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        artifact_object_key(episode.id, ArtifactType.STORYBOARD.value, digest), payload
    )
    async with factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=episode.id,
            artifact_type=ArtifactType.STORYBOARD,
            schema_version="1.0",
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
    image: SceneAwareImageGenerator
    video: FaultyVideoGenerator

    async def run(self, episode_id: str):
        from infrastructure.media.probe import PillowAvMediaProbe
        from tests.support.production import FakeVoiceGenerator

        suffix = uuid.uuid4().hex[:10]
        queues = {
            k: f"incident-e2e-{k}-{suffix}" for k in ("production", "image", "video", "voice")
        }
        f, s = self.factory, self.store
        runner = PaidJobRunner(session_factory=f, store=s, workdir=_workdir(self.tmp_path / "work"))
        probe = PillowAvMediaProbe()
        state = ProductionActivities(session_factory=f, store=s, bucket=BUCKET)
        image = ImageProductionActivities(
            session_factory=f, store=s, generator=self.image, probe=probe, runner=runner,
            bucket=BUCKET, poll_interval_seconds=0,
        )  # fmt: skip
        video = VideoProductionActivities(
            session_factory=f, store=s, generator=self.video, probe=probe, runner=runner,
            bucket=BUCKET, poll_interval_seconds=0,
        )  # fmt: skip
        voice = VoiceActivities(
            session_factory=f, store=s, generator=FakeVoiceGenerator(), probe=probe,
            workdir=_workdir(self.tmp_path / "voice-work"), bucket=BUCKET,
        )  # fmt: skip
        workers = [
            Worker(
                self.client,
                task_queue=queues["production"],
                workflows=[ProductionWorkflow],
                activities=state.all_activities(),
            ),  # fmt: skip
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
                    # concurrency=1（既定の動画と揃える）: シーン完了順を決定的にする。並行度が
                    # 違うと画像の完了順が入れ替わり得て、動画 submit の順序（延いてはどのシーン
                    # が sb6 の失敗より前に submit されるか）が試行ごとに変わってしまう
                    image_concurrency=1,
                    image_task_queue=queues["image"],
                    video_task_queue=queues["video"],
                    voice_task_queue=queues["voice"],
                ),
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


def _workdir(path: Path):
    from infrastructure.workdir import WorkDirectory

    return WorkDirectory(path, forbidden=())


async def _status(factory, episode_id: str) -> EpisodeStatus:
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    assert episode is not None
    return episode.status


async def _current(factory, episode_id: str, artifact_type: ArtifactType):
    async with factory() as session:
        return await ArtifactMetadataRepository(session).list_current_by_type(
            episode_id, artifact_type
        )


# ==================================================================== 区間A: 本物の Production


async def test_sb6_403_blocks_then_recovery_resumes_only_sb6_without_recharging(
    client, factory, store, tmp_path
) -> None:
    """要件1・7: sb1〜sb5成功、sb6のprepare()で403 → blocked。復旧後はsb6だけ再生成する。"""
    episode_id = await _seed(factory, store)
    image = SceneAwareImageGenerator(pending_polls=1, cost_usd=0.04)
    video = FaultyVideoGenerator(pending_polls=1, cost_usd=0.24)
    video.fail_scenes = {"sb6"}
    stack = Stack(
        client=client, factory=factory, store=store, tmp_path=tmp_path, image=image, video=video
    )

    # このシナリオの fake generator は fal_storage.py を経由しない（そこの診断ログの検証は
    # ADR-0030 側の単体テスト test_fal_storage.py が既に担っている）

    result = await stack.run(episode_id)

    assert result.status == EpisodeStatus.BLOCKED.value, result
    assert result.failure_class == "needs_input"
    assert await _status(factory, episode_id) is EpisodeStatus.BLOCKED
    # 画像は6シーンとも成功（動画だけが落ちた）。動画は sb1〜sb5 成功、sb6 は prepare で失敗
    assert image.submits_by_scene == dict.fromkeys(SIX_SCENES, 1)
    assert video.submits_by_scene == dict.fromkeys(["sb1", "sb2", "sb3", "sb4", "sb5"], 1)
    assert "sb6" not in video.submits_by_scene  # prepare で落ちたので submit まで行かない
    assert video.prepares_by_scene.get("sb6", 0) >= 1
    videos_so_far = {
        m.scene_id for m in await _current(factory, episode_id, ArtifactType.SCENE_VIDEO)
    }
    assert videos_so_far == {"sb1", "sb2", "sb3", "sb4", "sb5"}

    # ---- 復旧を fake で表現: 同じ生成器の fail_scenes を空にする
    video.fail_scenes = set()
    submits_before = dict(video.submits_by_scene)
    image_submits_before = dict(image.submits_by_scene)

    again = await stack.run(episode_id)

    assert again.status == EpisodeStatus.ASSETS_READY.value, again
    assert await _status(factory, episode_id) is EpisodeStatus.ASSETS_READY
    # sb1〜sb5 は再課金されない（submit回数が増えない）。sb6だけ新規 submit
    for scene, count in submits_before.items():
        assert video.submits_by_scene[scene] == count, f"{scene} was resubmitted"
    assert video.submits_by_scene.get("sb6") == 1
    for scene, count in image_submits_before.items():
        assert image.submits_by_scene[scene] == count, f"{scene} image was resubmitted"
    videos_final = {
        m.scene_id for m in await _current(factory, episode_id, ArtifactType.SCENE_VIDEO)
    }
    assert videos_final == set(SIX_SCENES)


async def test_corrupt_artifact_is_never_silently_reused_or_silently_overwritten(
    client, factory, store, tmp_path
) -> None:
    """要件8: 成功済みシーンの実体が壊れていても「成功済み」として再利用しない（ADR-0033）。

    壊す対象は動画（sb2）: 画像を壊すと、その画像を入力に使う同シーンの動画の
    ``_load_inputs``（既存の別の検査、ADR-0033 の対象外）が先に食い違いを検出して
    workflow 全体を blocked にする（画像は他の Activity の入力になるため）。動画は
    Production 内の他工程の入力にならないので、``find_and_verify_current`` /
    ``await_output`` 単体の検証を汚染なく検査できる。

    **実装時に判明した正しい挙動**（当初は「そのシーンだけ黙って再生成される」を期待したが、
    実装を追ったところ、より安全な挙動であることが分かった）: 破損検出後、生の取得物
    （evidence）は無傷なのでそこから検証をやり直そうとするが、書き込み先は sha256 由来の
    content-addressed キーで、そこには**既に（破損した）別内容が存在する**。immutability
    （INV-11）はそれを黙って上書きすることを禁止するので、``ArtifactConflictError``
    （needs_input）で安全に止まる。これは「壊れたら黙って直す」よりも安全:
    破損の自動修復は「検出しても自動で削除・変更しない」という方針（ADR-0033 §Decision(4)）と
    衝突するため、正しい着地点は「needs_input で人に見せる」である。
    """
    episode_id = await _seed(factory, store)
    image = SceneAwareImageGenerator(pending_polls=1, cost_usd=0.04)
    video = FaultyVideoGenerator(pending_polls=1, cost_usd=0.24)
    stack = Stack(
        client=client, factory=factory, store=store, tmp_path=tmp_path, image=image, video=video
    )

    first = await stack.run(episode_id)
    assert first.status == EpisodeStatus.ASSETS_READY.value, first

    # sb2 の動画本体を直接壊す（DB行は正常なまま。ADR-0033 が検出する対象そのもの）
    (sb2_video,) = [
        m
        for m in await _current(factory, episode_id, ArtifactType.SCENE_VIDEO)
        if m.scene_id == "sb2"
    ]
    payload = await store.get_json(sb2_video.object_key)
    media_key = payload["media"]["object_key"]
    # 実体を直接壊す（アプリの書き込み経路の外からの破損を模す。immutability ガードを迂回するのが
    # 目的なので、低レベルの minio クライアントを直接叩く）
    import io

    corrupted = b"bit rot"
    store._client.put_object(  # type: ignore[attr-defined]
        store._bucket, media_key, io.BytesIO(corrupted), length=len(corrupted)
    )

    image_submits_before = dict(image.submits_by_scene)
    video_submits_before = dict(video.submits_by_scene)

    # ProductionWorkflow を再実行させるため、admit できる新しい run を発行する必要はない:
    # assets_ready からの再実行は既存の admit（トークン一致）に任せる（run() が同じ episode を渡す）
    second = await stack.run(episode_id)

    # 破損は「成功」として素通りしない。needs_input で安全に止まる（terminal failedにはしない）
    assert second.status == EpisodeStatus.BLOCKED.value, second
    assert second.failure_class == "needs_input"
    assert await _status(factory, episode_id) is EpisodeStatus.BLOCKED
    # 誰も再課金されない: 破損の検出・拒否そのものは submit を一切起こさない
    for scene, count in image_submits_before.items():
        assert image.submits_by_scene[scene] == count, f"{scene} image was resubmitted"
    for scene, count in video_submits_before.items():
        assert video.submits_by_scene[scene] == count, f"{scene} video was resubmitted"
    # 破損した実体は自動で削除・上書きされていない（ADR-0033 §Decision(4)）
    assert (await store.get_bytes(media_key)) == corrupted


# ==================================================================== 区間A: 共有障害の抑止ゲート


async def test_auth_incident_threshold_suppresses_new_submits_for_same_provider_only(
    factory, store, tmp_path
) -> None:
    """要件6: 閾値を超えた認可拒否は同じproviderの新規submitだけを止める（ADR-0030）。

    本物の ``PaidJobRunner``・実DB・実MinIOに対して直接呼ぶ（``ProductionWorkflow`` 全体を
    ``AUTH_INCIDENT_SUPPRESSION_THRESHOLD`` 回起動し直すよりずっと速く、同じ実コード経路を検査
    できる）。
    """
    from contracts.states import ArtifactType as AT
    from domain.errors import ProviderCredentialSuspectedOutageError
    from infrastructure.production.paid_job import PaidJobSpec

    episode_id = await _seed(factory, store)
    runner = PaidJobRunner(
        session_factory=factory, store=store, workdir=_workdir(tmp_path / "work")
    )

    failing_video = FaultyVideoGenerator(pending_polls=0)
    failing_video.fail_scenes = {"sb1", "sb2", "sb3"}

    for scene in ("sb1", "sb2", "sb3"):
        spec = PaidJobSpec(
            episode_id=episode_id, scene_id=scene, provider=ProviderCall.FAL_VIDEO,
            artifact_type=AT.SCENE_VIDEO, input_hash=f"{scene}-hash".ljust(64, "0"), round=1,
        )  # fmt: skip
        with pytest.raises(ProviderUnavailableError):
            await runner.submit(spec, failing_video, VideoRequest(
                prompt=f"[{scene}] x", source_image=b"x", source_image_mime="image/png",
                duration_ms=4000, aspect="9:16",
            ))  # fmt: skip

    async with factory() as session:
        count = await ProviderAuthIncidentRepository(session).count_unresolved_within_window(
            ProviderCall.FAL_VIDEO,
            since=__import__("datetime").datetime.min.replace(tzinfo=__import__("datetime").UTC),
        )
    assert count == AUTH_INCIDENT_SUPPRESSION_THRESHOLD

    blocked_spec = PaidJobSpec(
        episode_id=episode_id, scene_id="sb4", provider=ProviderCall.FAL_VIDEO,
        artifact_type=AT.SCENE_VIDEO, input_hash="sb4-hash".ljust(64, "0"), round=1,
    )  # fmt: skip
    healthy_video = FaultyVideoGenerator(pending_polls=0)
    with pytest.raises(ProviderCredentialSuspectedOutageError):
        await runner.submit(blocked_spec, healthy_video, VideoRequest(
            prompt="[sb4] x", source_image=b"x", source_image_mime="image/png",
            duration_ms=4000, aspect="9:16",
        ))  # fmt: skip
    assert healthy_video.prepares_by_scene == {}  # ゲートで止まり、prepareすら呼ばれない

    # 別 provider（画像）は影響を受けない
    image_spec = PaidJobSpec(
        episode_id=episode_id, scene_id="sb1", provider=ProviderCall.FAL_IMAGE,
        artifact_type=AT.SCENE_IMAGE, input_hash="sb1-image-hash".ljust(64, "0"), round=1,
    )  # fmt: skip
    outcome = await runner.submit(
        image_spec,
        SceneAwareImageGenerator(pending_polls=0),
        ImageRequest(prompt="[sb1] x", width=1080, height=1920, aspect="9:16"),
    )
    assert isinstance(outcome, Submitted) and outcome.newly_submitted


# ==================================================================== 区間B: watchdog + 統一再開


async def test_watchdog_flags_stopped_pipeline_before_resume_then_resume_recovers(
    client, factory
) -> None:
    """要件5: Temporalがcompletedでも outcome=stopped なら watchdog は健全と判定しない。

    ``test_episode_resume.py`` の Stack（fake工程、専用queue）を再利用し、production が
    blocked で止まった pipeline に対して本物の ``run_daily_watchdog`` + 本物の Temporal client
    （``TemporalPipelineOutcomeChecker``）を実行して検査する。

    **実装時に発見した副産物**: このローカル Temporal サーバ（``default`` namespace）は
    PostgreSQL のようにテストごとにスキーマ分離されておらず、当日実行された他の統合試験
    （このセッションの以前のテスト実行、さらには本番投資調査で見つかった実際の事故
    Episode ``aedaaef1-...`` の実行履歴まで、保持期間 24h の間）が全て
    ``list_stopped_since`` に載ってくる。それらの Episode は（本テストの一時スキーマには
    存在しないので）DB 上は「存在しない」。この状況で watchdog がクラッシュせず、
    見つからない Episode をログして飛ばし、残りを正しく処理し続けることを、この試験は
    副次的に証明している（``infrastructure/temporal/watchdog.py`` の
    ``_check_outcome_mismatch`` に本ADR作業中に追加した頑健性）。Temporal namespace の
    テスト隔離が無いこと自体は既知の負債として別途記録する（本ADRのスコープ外）。
    """
    from datetime import UTC, datetime, timedelta

    from tests.integration.test_episode_resume import (
        TO_STORYBOARD_READY,
        _episode_at,
    )
    from tests.integration.test_episode_resume import (
        Stack as ResumeStack,
    )
    from tests.integration.test_episode_resume import (
        _status as resume_status,
    )

    resume_stack = ResumeStack(factory, client)
    episode_id = await _episode_at(factory, TO_STORYBOARD_READY)

    async with resume_stack.worker():
        async with await resume_stack.app_client() as api:
            first = await api.post(f"/episodes/{episode_id}/resume")
            assert first.status_code == 202, first.text
        stopped = await resume_stack.pipeline_result(episode_id)
        assert stopped["outcome"] == "stopped" and stopped["stopped_stage"] == "production"
        assert await resume_status(factory, episode_id) is EpisodeStatus.BLOCKED

        # Temporal の visibility store は結果整合（execution が閉じてから list_workflows に
        # 反映されるまで遅延がありうる）。少し待って数回問い合わせる（本物のTemporalの特性で
        # あり、production・watchdog 側のコードの問題ではない）
        outcome_checker = TemporalPipelineOutcomeChecker(client)
        stopped_since_start: list[Any] = []
        now = datetime.now(UTC)
        for _ in range(10):
            now = datetime.now(UTC)
            stopped_since_start = await outcome_checker.list_stopped_since(
                EPISODE_PIPELINE_WORKFLOW[0], now - timedelta(minutes=5)
            )
            if any(r.episode_id == episode_id for r in stopped_since_start):
                break
            await asyncio.sleep(1)
        assert any(r.episode_id == episode_id for r in stopped_since_start), (
            "the real Temporal client did not report this pipeline execution as outcome=stopped"
        )

        watchdog_result = await run_daily_watchdog(
            control=FakeScheduleControl(clock=[now]),
            session_factory=factory,
            workflow_counter=_AlwaysOneCounter(),
            notifier=_DiscardingNotifier(),
            request=WatchdogCheckRequest(now=now.isoformat()),
            pipeline_outcome_checker=outcome_checker,
        )
        assert (
            AnomalyKind.EPISODE_STAGE_STALLED.value in watchdog_result.anomalies
            or AnomalyKind.PIPELINE_OUTCOME_MISMATCH.value in watchdog_result.anomalies
        ), watchdog_result.anomalies
        async with factory() as session:
            open_anomalies = await OperationalAnomalyRepository(session).list_open()
        assert any(str(a.episode_id) == episode_id for a in open_anomalies), (
            "watchdog did not record an open anomaly for the stopped/blocked episode"
        )

        # ---- 復旧: resume で render・upload まで到達する
        async with await resume_stack.app_client() as api:
            second = await api.post(f"/episodes/{episode_id}/resume")
            assert second.status_code == 202, second.text
        finished = await resume_stack.pipeline_result(episode_id)

    assert finished["outcome"] == "completed"
    assert await resume_status(factory, episode_id) is EpisodeStatus.UPLOADED


class _AlwaysOneCounter:
    async def count_started_since(self, workflow_type: str, since) -> int:  # noqa: ANN001
        return 1


class _DiscardingNotifier:
    async def notify(self, notice) -> None:  # noqa: ANN001
        return None
