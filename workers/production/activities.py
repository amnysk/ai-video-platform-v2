"""Production 工程の状態系 Activity（ADR-0017）。

- 入場（admit）/ 作業計画（plan）/ マニフェスト組み立て（assemble）/
  完了（mark_ready）/ 失敗（record_failure）
- **次に何をするかは決めない**（INV-4）。メディア生成は別 worker の Activity（名前で呼ばれる）
- 有料呼び出しは持たない。予約台帳には触れない
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.artifacts import (
    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
    ScriptArtifact,
    StoryboardArtifact,
    parse_production_manifest,
    parse_scene_image_artifact,
    parse_scene_video_artifact,
    parse_scene_voice_artifact,
)
from contracts.production_activities import (
    PRODUCTION_ADMIT,
    PRODUCTION_ASSEMBLE_MANIFEST,
    PRODUCTION_MARK_READY,
    PRODUCTION_PLAN,
    PRODUCTION_RECORD_FAILURE,
    ProductionAdmitRequest,
    ProductionAdmitResult,
    ProductionAssembleRequest,
    ProductionFailureOutcome,
    ProductionMarkReadyRequest,
    ProductionMarkReadyResult,
    ProductionPlan,
    ProductionPlanRequest,
    ProductionRecordFailureRequest,
    SceneArtifactResult,
    SceneImageWork,
    SceneVideoWork,
    SceneVoiceWork,
)
from contracts.states import (
    JOB_TERMINAL_STATUSES,
    PRODUCTION_ADMISSIBLE_STATUSES,
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
)
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    ArtifactConflictError,
    ProductionInputInvalidError,
    ProductionInputMissingError,
    classify_failure,
)
from domain.job.transitions import JobEvent, episode_event_for_failure, job_event_for_failure
from domain.production.manifest import ArtifactRef, build_manifest, check_manifest_coverage
from domain.production.planning import plan_production
from domain.production.voice_fit import check_voices_fit_spans
from domain.storyboard.coverage import script_scene_spans
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.storage.artifact_store import ArtifactStore
from workers.production.run_inspector import WorkflowRunInspector

logger = logging.getLogger(__name__)

#: production 工程へ入ってよい Episode 状態と、入場で適用する事象（ADR-0017）。
ADMISSIBLE_STATUSES = PRODUCTION_ADMISSIBLE_STATUSES
ADMIT_EVENTS: dict[EpisodeStatus, EpisodeEvent] = {
    EpisodeStatus.STORYBOARD_READY: EpisodeEvent.STAGE_ADMITTED,
    EpisodeStatus.ASSETS_READY: EpisodeEvent.STAGE_ADMITTED,
    #: 人間の POST が再試行 / 再開の操作（ADR-0015 の「再開 API が無い」負債を production で解消）
    EpisodeStatus.NEEDS_WORK: EpisodeEvent.RETRY_ADMITTED,
    EpisodeStatus.BLOCKED: EpisodeEvent.RESUMED,
}
#: 再開は **production 自身が止めた** Episode だけ（入場トークンの workflow id で判定）。
#: 他工程（storyboard 等）で止まった Episode を production から再開すると上流を飛ばす。
RESUMABLE_STATUSES = frozenset({EpisodeStatus.NEEDS_WORK, EpisodeStatus.BLOCKED})

#: record_failure が閉じる job の種類。**workflow 自身が作る job だけ。**
#: PRODUCE_SCENE_* はメディア worker の所有物で、兄弟 cancel 中も worker が書いている。
#: ここで触ると競合する。非終端のまま残り、次の実行の ``find_open`` が再利用する。
WORKFLOW_OWNED_JOB_TYPES = frozenset({JobType.ASSEMBLE_PRODUCTION})

_RETRYABLE = frozenset({FailureClass.TRANSIENT, FailureClass.RETRYABLE})


def admission_token(workflow_id: str, run_id: str) -> str:
    """入場トークン（ADR-0015 と同形）。workflow id は再利用されるので run id まで含める。"""
    return f"{workflow_id}:{run_id}"


def parse_admission_token(token: str | None) -> tuple[str, str] | None:
    """``workflow_id:run_id`` を分ける。形が違えば（他工程の相関 id 等）``None``。"""
    if not token or ":" not in token:
        return None
    workflow_id, run_id = token.rsplit(":", 1)
    if not workflow_id or not run_id:
        return None
    return workflow_id, run_id


@dataclass(frozen=True)
class _Loaded:
    meta: ArtifactMetadata
    payload: dict[str, Any]


class ProductionActivities:
    """外部依存をすべて注入する（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
        run_inspector: WorkflowRunInspector | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket
        #: 無ければ ``in_progress`` の引き継ぎをしない（安全側）
        self._run_inspector = run_inspector

    def all_activities(self) -> Sequence[Callable[..., object]]:
        """``production`` queue の worker へ登録する Activity。メディア系は含めない。"""
        return [
            self.admit,
            self.plan,
            self.assemble_manifest,
            self.mark_ready,
            self.record_failure,
        ]

    # ------------------------------------------------------------------ 入場

    @activity.defn(name=PRODUCTION_ADMIT)
    async def admit(self, request: ProductionAdmitRequest) -> ProductionAdmitResult:
        """入場: ``ADMIT_EVENTS`` の状態 → ``in_progress`` + 入場トークン。

        - ``in_progress``: トークンが完全一致すれば通す（Activity 再実行）。同じ workflow id の
          **閉じた** run のトークンなら引き継ぐ（run が record_failure 前に死んだ /
          cancel された）。走っている run・他工程のトークンは引き継がない
        - ``needs_work`` / ``blocked``: 記録されたトークンが同じ workflow id
          （production 自身の失敗）のときだけ再開する
        - それ以外は入れず、何も書かない
        """
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(request.episode_id)
            if episode is None:
                return ProductionAdmitResult(admitted=False, status="")
            owner = await episodes.get_workflow_id(request.episode_id)
            if episode.status is EpisodeStatus.IN_PROGRESS:
                if owner == token:
                    return ProductionAdmitResult(admitted=True, status=episode.status.value)
                if not await self._stale_production_run(owner, request.workflow_id):
                    return ProductionAdmitResult(admitted=False, status=episode.status.value)
                logger.warning(
                    "production admit takes over episode=%s from closed run %s", episode.id, owner
                )
                await episodes.set_workflow_id(request.episode_id, token)
                await session.commit()
                return ProductionAdmitResult(admitted=True, status=episode.status.value)
            event = ADMIT_EVENTS.get(episode.status)
            if event is None:
                return ProductionAdmitResult(admitted=False, status=episode.status.value)
            if episode.status in RESUMABLE_STATUSES:
                parsed = parse_admission_token(owner)
                if parsed is None or parsed[0] != request.workflow_id:
                    return ProductionAdmitResult(admitted=False, status=episode.status.value)
            updated = await episodes.apply_event(request.episode_id, event)
            await episodes.set_workflow_id(request.episode_id, token)
            await session.commit()
            return ProductionAdmitResult(admitted=True, status=updated.status.value)

    async def _stale_production_run(self, owner: str | None, workflow_id: str) -> bool:
        """記録されたトークンが同じ workflow id の閉じた run か。問い合わせ失敗は例外（retry）。"""
        parsed = parse_admission_token(owner)
        if parsed is None or parsed[0] != workflow_id or self._run_inspector is None:
            return False
        return await self._run_inspector.is_closed(parsed[0], parsed[1])

    # ------------------------------------------------------------------ 計画

    @activity.defn(name=PRODUCTION_PLAN)
    async def plan(self, request: ProductionPlanRequest) -> ProductionPlan:
        """現行 storyboard と、それが参照する台本から作業一覧を作る。"""
        storyboard_loaded, storyboard = await self._load_current_storyboard(request.episode_id)
        script_loaded, script = await self._load_source_script(storyboard)
        work = plan_production(script, storyboard)
        return ProductionPlan(
            storyboard_artifact_id=storyboard_loaded.meta.id,
            storyboard_sha256=storyboard_loaded.meta.sha256,
            script_artifact_id=script_loaded.meta.id,
            script_sha256=script_loaded.meta.sha256,
            images=[SceneImageWork(scene_id=i.scene_id) for i in work.images],
            videos=[
                SceneVideoWork(scene_id=v.scene_id, requested_duration_ms=v.requested_duration_ms)
                for v in work.videos
            ],
            voices=[
                SceneVoiceWork(
                    script_scene_id=v.script_scene_id,
                    storyboard_scene_ids=list(v.storyboard_scene_ids),
                )
                for v in work.voices
            ],
        )

    async def _load(self, meta: ArtifactMetadata, label: str) -> _Loaded:
        """オブジェクトを読み、正規形の sha256 をメタデータと照合する。"""
        try:
            payload = await self._store.get_json(meta.object_key)
        except Exception as exc:
            raise ProductionInputInvalidError(
                f"{label} artifact {meta.object_key} is not readable: {type(exc).__name__}"
            ) from exc
        digest = sha256_hex(canonical_json_bytes(payload))
        if digest != meta.sha256:
            raise ProductionInputInvalidError(
                f"{label} artifact sha256 mismatch: stored={digest} metadata={meta.sha256}"
            )
        return _Loaded(meta=meta, payload=payload)

    async def _load_current_storyboard(self, episode_id: str) -> tuple[_Loaded, StoryboardArtifact]:
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.STORYBOARD
            )
        if meta is None:
            raise ProductionInputMissingError(f"no current storyboard artifact for {episode_id}")
        loaded = await self._load(meta, "storyboard")
        try:
            storyboard = StoryboardArtifact.model_validate(loaded.payload)
        except Exception as exc:  # pydantic ValidationError を含む
            raise ProductionInputInvalidError(f"storyboard invalid: {str(exc)[:500]}") from exc
        return loaded, storyboard

    async def _load_source_script(
        self, storyboard: StoryboardArtifact
    ) -> tuple[_Loaded, ScriptArtifact]:
        ref = storyboard.source_script
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).get(ref.artifact_id)
        if meta is None or meta.artifact_type is not ArtifactType.SCRIPT:
            raise ProductionInputMissingError(
                f"script artifact {ref.artifact_id} referenced by storyboard not found"
            )
        if meta.sha256 != ref.sha256:
            raise ProductionInputInvalidError(
                f"script sha256 {meta.sha256} != storyboard source_script {ref.sha256}"
            )
        loaded = await self._load(meta, "script")
        try:
            script = ScriptArtifact.model_validate(loaded.payload)
        except Exception as exc:
            raise ProductionInputInvalidError(f"script invalid: {str(exc)[:500]}") from exc
        return loaded, script

    # ------------------------------------------------------------------ マニフェスト

    @activity.defn(name=PRODUCTION_ASSEMBLE_MANIFEST)
    async def assemble_manifest(self, request: ProductionAssembleRequest) -> SceneArtifactResult:
        """現行のシーン Artifact を集めてマニフェストを作る。同じ構成なら既存を返す（INV-17）。"""
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.find_open(request.episode_id, JobType.ASSEMBLE_PRODUCTION)
            if job is None:
                job = await jobs.create(
                    episode_id=request.episode_id, type=JobType.ASSEMBLE_PRODUCTION
                )
                await session.commit()
        try:
            return await self._assemble(request, job.id)
        except Exception as exc:
            await self._mark_job_failed(job.id, exc)
            raise

    async def _assemble(
        self, request: ProductionAssembleRequest, job_id: str
    ) -> SceneArtifactResult:
        storyboard_loaded, storyboard = await self._load_current_storyboard(request.episode_id)
        if storyboard_loaded.meta.id != request.storyboard_artifact_id:
            raise ProductionInputInvalidError(
                f"current storyboard {storyboard_loaded.meta.id} != planned "
                f"{request.storyboard_artifact_id} (storyboard changed during production)"
            )
        script_loaded, script = await self._load_source_script(storyboard)
        if script_loaded.meta.id != request.script_artifact_id:
            raise ProductionInputInvalidError(
                f"storyboard source script {script_loaded.meta.id} != planned "
                f"{request.script_artifact_id}"
            )
        sb_sha = storyboard_loaded.meta.sha256
        scene_ids = {s.scene_id for s in storyboard.scenes}
        script_ids = {s.id for s in script.scenes}

        image_media: dict[str, str] = {}
        images = await self._collect(
            request.episode_id,
            ArtifactType.SCENE_IMAGE,
            scene_ids,
            storyboard_loaded,
            None,
            image_media=image_media,
        )
        voice_durations: dict[str, int] = {}
        voices = await self._collect(
            request.episode_id,
            ArtifactType.SCENE_VOICE,
            script_ids,
            storyboard_loaded,
            script_loaded,
            voice_durations=voice_durations,
        )
        # 描画（place_voices）が音声を置く区間と同じ窓に、実尺が収まること（ADR-0027）
        try:
            spans = script_scene_spans(storyboard, script)
        except KeyError as exc:  # 台本シーンの一部が storyboard に無い（区間が定まらない）
            raise ProductionInputInvalidError(
                f"storyboard {storyboard_loaded.meta.id} does not cover script scene {exc}"
            ) from exc
        check_voices_fit_spans(spans, voice_durations)
        videos = await self._collect(
            request.episode_id,
            ArtifactType.SCENE_VIDEO,
            scene_ids,
            storyboard_loaded,
            None,
            images=images,
            image_media=image_media,
        )

        manifest = build_manifest(
            episode_id=request.episode_id,
            storyboard_ref=_ref(storyboard_loaded.meta),
            script_ref=_ref(script_loaded.meta),
            storyboard=storyboard,
            script=script,
            images=images,
            videos=videos,
            voices=voices,
        )
        check_manifest_coverage(
            parse_production_manifest(manifest),
            storyboard,
            script,
            storyboard_sha256=sb_sha,
            script_sha256=script_loaded.meta.sha256,
        )
        input_hash = manifest_input_hash(
            storyboard_sha256=sb_sha,
            script_sha256=script_loaded.meta.sha256,
            member_sha256s=[
                r.sha256 for r in (*images.values(), *videos.values(), *voices.values())
            ],
        )

        async with self._session_factory() as session:
            existing = await ArtifactMetadataRepository(session).find_current(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.PRODUCTION_MANIFEST,
                input_hash=input_hash,
            )
            if existing is not None:
                await self._finish_job(session, job_id, skipped=True)
                await session.commit()
                return _result(existing, reused=True)

        digest = sha256_hex(canonical_json_bytes(manifest))
        key = artifact_object_key(
            request.episode_id, ArtifactType.PRODUCTION_MANIFEST.value, digest
        )
        put = await self._store.put_json(key, manifest)
        readback = sha256_hex(canonical_json_bytes(await self._store.get_json(put.key)))
        if readback != digest or put.sha256 != digest:
            raise ArtifactConflictError(
                f"manifest readback sha256 mismatch at {put.key}: expected={digest} "
                f"put={put.sha256} readback={readback}"
            )
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.PRODUCTION_MANIFEST,
                schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
                bucket=self._bucket,
                object_key=put.key,
                sha256=digest,
                size_bytes=put.size,
                produced_by_job_id=job_id,
                input_hash=input_hash,
            )
            await self._finish_job(session, job_id, skipped=False)
            await session.commit()
        return _result(meta, reused=False)

    async def _collect(
        self,
        episode_id: str,
        artifact_type: ArtifactType,
        expected: set[str],
        storyboard: _Loaded,
        script: _Loaded | None,
        *,
        images: dict[str, ArtifactRef] | None = None,
        image_media: dict[str, str] | None = None,
        voice_durations: dict[str, int] | None = None,
    ) -> dict[str, ArtifactRef]:
        """期待するシーンの現行 Artifact を読み、入力の固定（storyboard / 台本 / 画像）を照合する。

        画像を集めるときは ``image_media`` にシーン → メディア本体の sha256 を書き込む。
        動画の元画像はメディア本体の sha256 で照合する（動画の ``input_hash`` が覆うのは
        画像のメディア sha。画像 Artifact が同じメディアで記録し直されても動画は有効）。

        期待しないシーンの行（前の storyboard の残り）は無視する。欠けは ``build_manifest`` が
        missing にする。
        """
        async with self._session_factory() as session:
            rows = await ArtifactMetadataRepository(session).list_current_by_type(
                episode_id, artifact_type
            )
        refs: dict[str, ArtifactRef] = {}
        for meta in rows:
            if meta.scene_id is None or meta.scene_id not in expected:
                continue
            loaded = await self._load(meta, artifact_type.value)
            try:
                if artifact_type is ArtifactType.SCENE_IMAGE:
                    image = parse_scene_image_artifact(loaded.payload)
                    scene_key, source_sb = image.scene_id, image.source_storyboard
                    if image_media is not None:
                        image_media[image.scene_id] = image.media.sha256
                elif artifact_type is ArtifactType.SCENE_VIDEO:
                    video = parse_scene_video_artifact(loaded.payload)
                    scene_key, source_sb = video.scene_id, video.source_storyboard
                    current_image = (images or {}).get(video.scene_id)
                    current_media = (image_media or {}).get(video.scene_id)
                    if current_image is not None and current_media is not None:
                        source_media = await self._source_image_media_sha(
                            video.source_image.artifact_id,
                            video.source_image.sha256,
                            current_image,
                            current_media,
                        )
                        if source_media != current_media:
                            raise ProductionInputInvalidError(
                                f"video {meta.id} for {video.scene_id} was made from image media "
                                f"{source_media}, not the current image media {current_media}"
                            )
                else:
                    voice = parse_scene_voice_artifact(loaded.payload)
                    scene_key, source_sb = voice.script_scene_id, voice.source_storyboard
                    if voice_durations is not None:
                        voice_durations[voice.script_scene_id] = voice.duration_ms
                    assert script is not None
                    if voice.source_script.sha256 != script.meta.sha256:
                        raise ProductionInputInvalidError(
                            f"voice {meta.id} source_script {voice.source_script.sha256} != "
                            f"{script.meta.sha256}"
                        )
            except ProductionInputInvalidError:
                raise
            except Exception as exc:
                raise ProductionInputInvalidError(
                    f"{artifact_type.value} {meta.id} invalid: {str(exc)[:500]}"
                ) from exc
            if scene_key != meta.scene_id:
                raise ProductionInputInvalidError(
                    f"{artifact_type.value} {meta.id} scene {scene_key} != metadata {meta.scene_id}"
                )
            if (
                source_sb.sha256 != storyboard.meta.sha256
                or source_sb.artifact_id != storyboard.meta.id
            ):
                raise ProductionInputInvalidError(
                    f"{artifact_type.value} {meta.id} for {scene_key} references storyboard "
                    f"{source_sb.sha256}, not the current {storyboard.meta.sha256}"
                )
            refs[scene_key] = _ref(meta)
        return refs

    async def _source_image_media_sha(
        self, artifact_id: str, sha256: str, current: ArtifactRef, current_media: str
    ) -> str:
        """動画が参照する画像 Artifact のメディア sha256。参照先が読めなければ invalid。"""
        if artifact_id == current.artifact_id and sha256 == current.sha256:
            return current_media
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).get(artifact_id)
        if meta is None or meta.artifact_type is not ArtifactType.SCENE_IMAGE:
            raise ProductionInputInvalidError(f"video source image {artifact_id} not found")
        if meta.sha256 != sha256:
            raise ProductionInputInvalidError(
                f"video source image {artifact_id} sha256 {meta.sha256} != referenced {sha256}"
            )
        loaded = await self._load(meta, "source scene_image")
        return parse_scene_image_artifact(loaded.payload).media.sha256

    @staticmethod
    async def _finish_job(session: AsyncSession, job_id: str, *, skipped: bool) -> None:
        jobs = JobRepository(session)
        job = await jobs.get(job_id)
        if job is None or job.status in JOB_TERMINAL_STATUSES:
            return
        if skipped:
            await jobs.mark_skipped(job_id)
            return
        if job.status is not JobStatus.RUNNING:
            await jobs.start(job_id)
        await jobs.succeed(job_id)

    async def _mark_job_failed(self, job_id: str, exc: BaseException) -> None:
        failure_class = classify_failure(exc)
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is None or job.status in JOB_TERMINAL_STATUSES:
                return
            if job.status is JobStatus.RETRYABLE_FAILED and failure_class in _RETRYABLE:
                return  # 表に辺が無い。同じ分類の重複を書かない
            await jobs.record_failure(
                job_id,
                event=job_event_for_failure(failure_class),
                failure_class=failure_class,
                error_summary=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()

    # ------------------------------------------------------------------ 完了 / 失敗

    @activity.defn(name=PRODUCTION_MARK_READY)
    async def mark_ready(self, request: ProductionMarkReadyRequest) -> ProductionMarkReadyResult:
        """入場トークンが一致する実行だけが ``assets_ready`` へ進める。"""
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            current = await episodes.get(request.episode_id)
            if current is None:
                return ProductionMarkReadyResult(status="", owned=False)
            if not await _owns(episodes, request.episode_id, token, "mark_ready"):
                return ProductionMarkReadyResult(status=current.status.value, owned=False)
            if current.status is EpisodeStatus.ASSETS_READY:
                return ProductionMarkReadyResult(status=current.status.value)  # 再実行
            episode = await episodes.apply_event(request.episode_id, EpisodeEvent.ASSETS_READY)
            await session.commit()
            return ProductionMarkReadyResult(status=episode.status.value)

    @activity.defn(name=PRODUCTION_RECORD_FAILURE)
    async def record_failure(
        self, request: ProductionRecordFailureRequest
    ) -> ProductionFailureOutcome:
        """失敗クラス → Episode 事象。workflow が所有する job を閉じるか再開可能にする。

        job の扱い（``WORKFLOW_OWNED_JOB_TYPES`` だけ。PRODUCE_SCENE_* には触れない）:
        - ``job_id`` が指定されていればその job に失敗クラスの事象を適用する
        - それ以外の非終端 job（cancel で中断したマニフェスト組み立て等）:
          ``permanent`` なら終端へ。そうでなければ ``queued`` / ``running`` を
          ``retryable_failed`` にし、次の実行で ``start`` できるようにする
          （``running`` のまま残すと ``start`` の辺が無く再開できない）
        """
        failure_class = FailureClass(request.failure_class)
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            if not await _owns(episodes, request.episode_id, token, "record_failure"):
                current = await episodes.get(request.episode_id)
                return ProductionFailureOutcome(
                    episode_status=current.status.value if current else "", owned=False
                )
            await self._settle_jobs(session, request, failure_class)

            current = await episodes.get(request.episode_id)
            if current is not None and current.status is not EpisodeStatus.IN_PROGRESS:
                await session.commit()
                return ProductionFailureOutcome(episode_status=current.status.value)
            episode = await episodes.apply_event(
                request.episode_id,
                episode_event_for_failure(failure_class),
                blocked_reason=f"{failure_class.value}: {request.error_summary}"[:1000],
            )
            if request.retry_exhausted and episode.status is EpisodeStatus.NEEDS_WORK:
                episode = await episodes.apply_event(
                    request.episode_id, EpisodeEvent.RETRY_BUDGET_EXHAUSTED
                )
            await session.commit()
            return ProductionFailureOutcome(episode_status=episode.status.value)

    @staticmethod
    async def _settle_jobs(
        session: AsyncSession,
        request: ProductionRecordFailureRequest,
        failure_class: FailureClass,
    ) -> None:
        jobs = JobRepository(session)
        summary = request.error_summary
        for job in await jobs.list_for_episode(request.episode_id):
            if job.type not in WORKFLOW_OWNED_JOB_TYPES or job.status in JOB_TERMINAL_STATUSES:
                continue
            if job.id == request.job_id or failure_class is FailureClass.PERMANENT:
                if job.status is JobStatus.RETRYABLE_FAILED:
                    event = (
                        JobEvent.ATTEMPTS_EXHAUSTED
                        if failure_class in _RETRYABLE
                        else JobEvent.PERMANENT_FAILURE
                    )
                else:
                    event = job_event_for_failure(failure_class)
                await jobs.record_failure(
                    job.id, event=event, failure_class=failure_class, error_summary=summary
                )
            elif job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                await jobs.record_failure(
                    job.id,
                    event=JobEvent.RETRYABLE_FAILURE,
                    failure_class=FailureClass.RETRYABLE,
                    error_summary=f"interrupted: production stopped ({summary})",
                )


async def _owns(episodes: EpisodeRepository, episode_id: str, token: str, action: str) -> bool:
    owner = await episodes.get_workflow_id(episode_id)
    if owner == token:
        return True
    logger.warning(
        "production %s refused: admission token mismatch episode=%s recorded=%s caller=%s",
        action,
        episode_id,
        owner,
        token,
    )
    return False


def manifest_input_hash(
    *, storyboard_sha256: str, script_sha256: str, member_sha256s: Sequence[str]
) -> str:
    """マニフェストの入力指紋: storyboard / 台本 / 構成メンバーの sha256（順序に依存しない）。"""
    payload = {
        "artifact_type": ArtifactType.PRODUCTION_MANIFEST.value,
        "schema_version": PRODUCTION_ARTIFACT_SCHEMA_VERSION,
        "storyboard_sha256": storyboard_sha256,
        "script_sha256": script_sha256,
        "members": sorted(member_sha256s),
    }
    return sha256_hex(canonical_json_bytes(payload))


def _ref(meta: ArtifactMetadata) -> ArtifactRef:
    return ArtifactRef(artifact_id=meta.id, sha256=meta.sha256, schema_version=meta.schema_version)


def _result(meta: ArtifactMetadata, *, reused: bool) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=meta.id, object_key=meta.object_key, sha256=meta.sha256, reused=reused
    )


__all__ = [
    "ADMISSIBLE_STATUSES",
    "ADMIT_EVENTS",
    "RESUMABLE_STATUSES",
    "WORKFLOW_OWNED_JOB_TYPES",
    "ProductionActivities",
    "admission_token",
    "parse_admission_token",
    "manifest_input_hash",
]
