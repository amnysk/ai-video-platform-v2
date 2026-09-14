"""Production Video Worker の Activity（ADR-0017 Phase 4C）。

Activity は「入力から出力 Artifact を作って結果を返す」だけ。次に何をするかは決めない（INV-4）。
他の worker を import しない（INV-3）。有料呼び出しの順序は ``infrastructure.production.paid_job``。

失敗は ``infrastructure.production.activity_errors`` が ``ApplicationError(type=<型名>)`` にする
（needs_input / permanent は non_retryable、DB・ストア・作業領域の一時障害は ``TransientError``）。

元画像のアップロードなど非課金の準備は、生成器の ``prepare`` として ``PaidJobRunner`` が
**予約の前**に呼ぶ（失敗しても台帳に曖昧な行を残さない）。
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
    SceneImageArtifact,
    StoryboardArtifact,
    StoryboardScene,
    build_scene_video_artifact,
    parse_scene_image_artifact,
)
from contracts.production_activities import (
    VIDEO_AWAIT,
    VIDEO_SUBMIT,
    SceneArtifactResult,
    SubmitResult,
    VideoAwaitRequest,
    VideoSubmitRequest,
)
from contracts.states import ArtifactType, FailureClass, JobStatus, JobType, ProviderCall
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.errors import (
    ArtifactConflictError,
    MediaValidationError,
    ProductionInputInvalidError,
    ProductionInputMissingError,
    TransientError,
    classify_failure,
)
from domain.job.transitions import job_event_for_failure
from domain.production.identity import video_input_hash
from domain.production.media import MediaProbe, validate_video
from domain.production.ports import VideoGenerator, VideoRequest
from domain.production.prompting import (
    DEFAULT_VIDEO_MOTION,
    VideoMotionProfile,
    build_video_prompt,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.production.activity_errors import (
    AWAIT_ROUND_FINAL_ERRORS,
    raise_activity_error,
    translate_error,
)
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Reused
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256

logger = logging.getLogger(__name__)

_JOB_DONE = frozenset({JobStatus.SUCCEEDED, JobStatus.SKIPPED, JobStatus.TERMINAL_FAILED})
VIDEO_MIME = "video/mp4"


@dataclass(frozen=True)
class _Inputs:
    storyboard_meta: ArtifactMetadata
    storyboard: StoryboardArtifact
    scene: StoryboardScene
    image_meta: ArtifactMetadata
    image: SceneImageArtifact
    image_bytes: bytes
    requested_duration_ms: int


class VideoProductionActivities:
    """外部依存をすべて注入する（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        generator: VideoGenerator,
        probe: MediaProbe,
        runner: PaidJobRunner,
        bucket: str,
        poll_interval_seconds: float,
        await_deadline_seconds: float | None = None,
        motion: VideoMotionProfile = DEFAULT_VIDEO_MOTION,
        heartbeat: Callable[..., None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._generator = generator
        self._probe = probe
        self._runner = runner
        self._bucket = bucket
        self._poll_interval_seconds = poll_interval_seconds
        self._await_deadline_seconds = await_deadline_seconds
        self._motion = motion
        self._heartbeat = heartbeat

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.submit, self.await_video]

    # ------------------------------------------------------------------ submit

    @activity.defn(name=VIDEO_SUBMIT)
    async def submit(self, request: VideoSubmitRequest) -> SubmitResult:
        """ドメイン例外は型名つき ``ApplicationError``、インフラの一時障害は ``TransientError``。"""
        try:
            return await self._submit(request)
        except Exception as exc:
            raise_activity_error(exc)

    async def _submit(self, request: VideoSubmitRequest) -> SubmitResult:
        inputs = await self._load_inputs(
            request.episode_id,
            request.storyboard_artifact_id,
            request.scene_id,
            request.source_image_artifact_id,
            request.requested_duration_ms,
        )
        input_hash = self._input_hash(inputs)
        job_id = await self._open_job(request.episode_id, request.scene_id)
        try:
            spec = PaidJobSpec(
                episode_id=request.episode_id,
                scene_id=request.scene_id,
                provider=ProviderCall.FAL_VIDEO,
                artifact_type=ArtifactType.SCENE_VIDEO,
                input_hash=input_hash,
                round=request.round,
                job_id=job_id,
            )
            async with self._session_factory() as session:
                existing = await ArtifactMetadataRepository(session).find_current(
                    request.episode_id, ArtifactType.SCENE_VIDEO, input_hash, request.scene_id
                )
            if existing is None:
                await self._start_job(job_id)
            outcome = await self._runner.submit(spec, self._generator, self._video_request(inputs))
        except Exception as exc:
            await self._mark_job_failed(job_id, exc)
            raise
        if isinstance(outcome, Reused):
            await self._skip_job(job_id)
            return SubmitResult(reservation_id="", artifact=_result(outcome.artifact, reused=True))
        return SubmitResult(reservation_id=outcome.reservation_id)

    # ------------------------------------------------------------------ await

    @activity.defn(name=VIDEO_AWAIT)
    async def await_video(self, request: VideoAwaitRequest) -> SceneArtifactResult:
        """ラウンド確定済みの失敗（``AWAIT_ROUND_FINAL_ERRORS``）は Activity の retry を止める。

        型名は変えないので workflow は retryable として新ラウンドへ進む（ADR-0017 §4）。
        """
        try:
            return await self._await_video(request)
        except Exception as exc:
            raise_activity_error(exc, final_for_activity=AWAIT_ROUND_FINAL_ERRORS)

    async def _await_video(self, request: VideoAwaitRequest) -> SceneArtifactResult:
        inputs = await self._load_inputs(
            request.episode_id,
            request.storyboard_artifact_id,
            request.scene_id,
            request.source_image_artifact_id,
            request.requested_duration_ms,
        )
        input_hash = self._input_hash(inputs)
        job_id = await self._open_job(request.episode_id, request.scene_id)
        try:
            return await self._await(request, inputs, input_hash, job_id)
        except Exception as exc:
            await self._mark_job_failed(job_id, exc)
            raise

    async def _await(
        self, request: VideoAwaitRequest, inputs: _Inputs, input_hash: str, job_id: str
    ) -> SceneArtifactResult:
        async with self._session_factory() as session:
            reservation = await ProviderReservationRepository(session).get(request.reservation_id)
        if reservation is None:
            raise ProductionInputMissingError(f"reservation {request.reservation_id} not found")
        if (
            reservation.episode_id != request.episode_id
            or reservation.scene_id != request.scene_id
            or reservation.input_hash != input_hash
            or reservation.provider is not ProviderCall.FAL_VIDEO
        ):
            raise ProductionInputInvalidError(
                f"reservation {reservation.id} does not match the requested scene video input"
            )

        output = await self._runner.await_output(
            request.reservation_id,
            self._generator,
            poll_interval_seconds=self._poll_interval_seconds,
            deadline_seconds=self._await_deadline_seconds,
            heartbeat=self._heartbeat or _activity_heartbeat,
        )
        if output.artifact is not None:
            await self._succeed_job(job_id)
            return _result(output.artifact, reused=True)

        # spent は commit 済み。ここから検証（落ちても課金の事実は残る / ADR-0013）
        data = output.data
        try:
            info = self._probe.probe_video(data)
            if info.has_audio:
                # 音声なしを要求した。混ざっていれば仕様違いの取得物（音声は voice が単一の真実）
                raise MediaValidationError("video has an audio stream but audio was not requested")
            validate_video(info, len(data), requested_duration_ms=inputs.requested_duration_ms)
        except MediaValidationError as exc:
            await self._record_rejected(request.reservation_id, exc)
            raise

        media_sha = sha256_hex(data)
        media_key = media_object_key(
            request.episode_id, ArtifactType.SCENE_VIDEO.value, request.scene_id, media_sha, "mp4"
        )
        put_media = await self._store.put_bytes(media_key, data, VIDEO_MIME)
        media_readback = await readback_sha256(self._store, put_media.key)
        if media_readback != media_sha or put_media.sha256 != media_sha:
            raise ArtifactConflictError(
                f"scene video readback sha256 mismatch at {put_media.key}: "
                f"expected={media_sha} put={put_media.sha256} readback={media_readback}"
            )

        artifact = build_scene_video_artifact(
            episode_id=request.episode_id,
            source_storyboard={
                "artifact_id": inputs.storyboard_meta.id,
                "sha256": inputs.storyboard_meta.sha256,
                "schema_version": inputs.storyboard_meta.schema_version,
            },
            scene_id=request.scene_id,
            source_image={
                "artifact_id": inputs.image_meta.id,
                "sha256": inputs.image_meta.sha256,
            },
            media={
                "object_key": put_media.key,
                "sha256": media_sha,
                "bytes": len(data),
                "mime": VIDEO_MIME,
            },
            duration_ms=info.decoded_duration_ms,
            requested_duration_ms=inputs.requested_duration_ms,
            width=info.width,
            height=info.height,
            fps_millis=info.fps_millis,
            generator={
                "generator": self._generator.generator_id,
                "generator_model": _model_label(self._generator),
                "generation_profile_id": self._generation_profile_id,
            },
        )
        digest = sha256_hex(canonical_json_bytes(artifact))
        object_key = artifact_object_key(
            request.episode_id, ArtifactType.SCENE_VIDEO.value, digest, request.scene_id
        )
        put = await self._store.put_json(object_key, artifact)
        readback = sha256_hex(canonical_json_bytes(await self._store.get_json(put.key)))
        if readback != digest or put.sha256 != digest:
            raise ArtifactConflictError(
                f"scene video artifact readback sha256 mismatch at {put.key}: "
                f"expected={digest} put={put.sha256} readback={readback}"
            )

        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.SCENE_VIDEO,
                schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
                bucket=self._bucket,
                object_key=put.key,
                sha256=digest,
                size_bytes=put.size,
                produced_by_job_id=job_id,
                input_hash=input_hash,
                scene_id=request.scene_id,
            )
            await ProviderReservationRepository(session).attach_artifact(
                request.reservation_id, meta.id
            )
            await session.commit()
        await self._succeed_job(job_id)
        return _result(meta, reused=False)

    async def _record_rejected(self, reservation_id: str, exc: BaseException) -> None:
        """検証に落ちた evidence を台帳に記録する（次の submit が新ラウンドへ進む）。"""
        try:
            await self._runner.record_output_rejected(reservation_id, exc)
        except Exception:  # 記録の失敗で検証の失敗を隠さない
            logger.warning(
                "could not record rejected output reservation=%s", reservation_id, exc_info=True
            )

    # ------------------------------------------------------------------ 入力

    async def _load_inputs(
        self,
        episode_id: str,
        storyboard_id: str,
        scene_id: str,
        image_id: str,
        requested_duration_ms: int,
    ) -> _Inputs:
        sb_meta = await self._get_meta(episode_id, storyboard_id, ArtifactType.STORYBOARD)
        sb_payload = await self._read_verified_json(sb_meta, "storyboard")
        try:
            storyboard = StoryboardArtifact.model_validate(sb_payload)
        except Exception as exc:
            raise ProductionInputInvalidError(f"storyboard invalid: {str(exc)[:500]}") from exc
        scene = next((s for s in storyboard.scenes if s.scene_id == scene_id), None)
        if scene is None:
            raise ProductionInputInvalidError(f"storyboard has no scene {scene_id}")

        supported = self._generator.supported_duration_ms(scene.duration_ms)
        if requested_duration_ms not in (scene.duration_ms, supported):
            raise ProductionInputInvalidError(
                f"requested duration {requested_duration_ms} ms does not match scene "
                f"{scene_id} ({scene.duration_ms} ms, provider-supported {supported} ms)"
            )

        image_meta = await self._get_meta(episode_id, image_id, ArtifactType.SCENE_IMAGE)
        if image_meta.scene_id != scene_id:
            raise ProductionInputInvalidError(
                f"scene image {image_id} belongs to scene {image_meta.scene_id}, not {scene_id}"
            )
        image_payload = await self._read_verified_json(image_meta, "scene image")
        try:
            image = parse_scene_image_artifact(image_payload)
        except Exception as exc:
            raise ProductionInputInvalidError(f"scene image invalid: {str(exc)[:500]}") from exc
        if image.scene_id != scene_id or image.episode_id != episode_id:
            raise ProductionInputInvalidError(f"scene image {image_id} is not for scene {scene_id}")
        if (
            image.source_storyboard.artifact_id != sb_meta.id
            or image.source_storyboard.sha256 != sb_meta.sha256
        ):
            raise ProductionInputInvalidError(
                f"scene image {image_id} was made from a different storyboard"
            )
        try:
            image_bytes = await self._store.get_bytes(image.media.object_key)
        except Exception as exc:
            if isinstance(translate_error(exc), TransientError):
                raise  # ストアの通信障害は入力の欠陥ではない（境界で TransientError）
            raise ProductionInputMissingError(
                f"scene image media {image.media.object_key} is not readable: {type(exc).__name__}"
            ) from exc
        if sha256_hex(image_bytes) != image.media.sha256:
            raise ProductionInputInvalidError(
                f"scene image media sha256 mismatch at {image.media.object_key}"
            )
        return _Inputs(
            storyboard_meta=sb_meta,
            storyboard=storyboard,
            scene=scene,
            image_meta=image_meta,
            image=image,
            image_bytes=image_bytes,
            requested_duration_ms=supported,
        )

    async def _get_meta(
        self, episode_id: str, artifact_id: str, artifact_type: ArtifactType
    ) -> ArtifactMetadata:
        label = artifact_type.value
        async with self._session_factory() as session:
            try:
                meta = await ArtifactMetadataRepository(session).get(artifact_id)
            except ValueError as exc:
                raise ProductionInputInvalidError(
                    f"{label} artifact id is not a UUID: {artifact_id!r}"
                ) from exc
        if meta is None:
            raise ProductionInputMissingError(f"{label} artifact {artifact_id} not found")
        if meta.episode_id != episode_id or meta.artifact_type is not artifact_type:
            raise ProductionInputInvalidError(
                f"artifact {artifact_id} is not a {label} of episode {episode_id}"
            )
        return meta

    async def _read_verified_json(self, meta: ArtifactMetadata, label: str) -> dict[str, Any]:
        try:
            payload = await self._store.get_json(meta.object_key)
        except Exception as exc:
            if isinstance(translate_error(exc), TransientError):
                raise  # ストアの通信障害は入力の欠陥ではない（境界で TransientError）
            raise ProductionInputInvalidError(
                f"{label} {meta.object_key} is not readable: {type(exc).__name__}"
            ) from exc
        digest = sha256_hex(canonical_json_bytes(payload))
        if digest != meta.sha256:
            raise ProductionInputInvalidError(
                f"{label} sha256 mismatch: stored={digest} metadata={meta.sha256}"
            )
        return payload

    @property
    def _generation_profile_id(self) -> str:
        """生成器のプロファイルと動画プロンプト規則の版。どちらが変わっても再生成対象。"""
        return f"{self._generator.generation_profile_id}+{self._motion.motion_profile_id}"

    def _input_hash(self, inputs: _Inputs) -> str:
        scene = inputs.scene
        return video_input_hash(
            episode_id=inputs.storyboard.episode_id,
            artifact_type=ArtifactType.SCENE_VIDEO.value,
            schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
            storyboard_sha256=inputs.storyboard_meta.sha256,
            scene_id=scene.scene_id,
            source_image_sha256=inputs.image.media.sha256,
            visual_description=scene.visual_description,
            camera_movement=scene.camera_movement,
            transition_in=scene.transition_in,
            requested_duration_ms=inputs.requested_duration_ms,
            generator_id=self._generator.generator_id,
            generation_profile_id=self._generation_profile_id,
        )

    def _video_request(self, inputs: _Inputs) -> VideoRequest:
        return VideoRequest(
            prompt=build_video_prompt(inputs.scene, self._motion),
            source_image=inputs.image_bytes,
            source_image_mime=inputs.image.media.mime,
            duration_ms=inputs.requested_duration_ms,
            aspect="9:16",
        )

    # ------------------------------------------------------------------ job

    async def _open_job(self, episode_id: str, scene_id: str) -> str:
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_VIDEO, scene_id)
            if job is None:
                job = await jobs.create(
                    episode_id=episode_id, type=JobType.PRODUCE_SCENE_VIDEO, scene_id=scene_id
                )
                await session.commit()
            return job.id

    async def _start_job(self, job_id: str) -> None:
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RETRYABLE_FAILED):
                await jobs.start(job_id)
                await session.commit()

    async def _skip_job(self, job_id: str) -> None:
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is None or job.status in _JOB_DONE:
                return
            if job.status is JobStatus.RETRYABLE_FAILED:
                # 表に retryable_failed → skipped の辺は無い。RETRY_ADMITTED で running へ戻す
                await jobs.start(job_id)
            await jobs.mark_skipped(job_id)
            await session.commit()

    async def _succeed_job(self, job_id: str) -> None:
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is None or job.status in _JOB_DONE:
                return
            if job.status is not JobStatus.RUNNING:
                await jobs.start(job_id)
            await jobs.succeed(job_id)
            await session.commit()

    async def _mark_job_failed(self, job_id: str, exc: BaseException) -> None:
        failure = translate_error(exc)
        failure_class = classify_failure(failure)
        try:
            async with self._session_factory() as session:
                jobs = JobRepository(session)
                job = await jobs.get(job_id)
                if job is None or job.status in _JOB_DONE:
                    return
                if job.status is JobStatus.RETRYABLE_FAILED and failure_class in {
                    FailureClass.TRANSIENT,
                    FailureClass.RETRYABLE,
                }:
                    return
                await jobs.record_failure(
                    job_id,
                    event=job_event_for_failure(failure_class),
                    failure_class=failure_class,
                    error_summary=f"{type(failure).__name__}: {failure}",
                )
                await session.commit()
        except Exception:  # 記録の失敗（DB 断など）で元の失敗を隠さない
            logger.warning("could not record %s job failure job=%s", "video", job_id, exc_info=True)


def _activity_heartbeat(*details: Any) -> None:
    if activity.in_activity():
        activity.heartbeat(*details)


def _model_label(generator: VideoGenerator) -> str:
    model = getattr(generator, "model_id", None)
    return model if isinstance(model, str) and model else generator.generator_id


def _result(meta: ArtifactMetadata, *, reused: bool) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=meta.id, object_key=meta.object_key, sha256=meta.sha256, reused=reused
    )


__all__ = ["VIDEO_MIME", "VideoProductionActivities"]
