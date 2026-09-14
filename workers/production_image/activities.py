"""Production Image Worker の Activity（ADR-0017 Phase 4A）。

Activity は「入力から出力 Artifact を作って結果を返す」だけ。次に何をするかは決めない（INV-4）。
他の worker を import しない（INV-3）。有料呼び出しの順序は ``infrastructure.production.paid_job``。

失敗は ``infrastructure.production.activity_errors`` が ``ApplicationError(type=<型名>)`` にする
（needs_input / permanent は non_retryable、DB・ストア・作業領域の一時障害は ``TransientError``）。
workflow は ``failure_class_from_type_name`` で分類する（voice / video と同じ）。
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
    StoryboardArtifact,
    StoryboardScene,
    build_scene_image_artifact,
)
from contracts.production_activities import (
    IMAGE_AWAIT,
    IMAGE_SUBMIT,
    ImageAwaitRequest,
    ImageSubmitRequest,
    SceneArtifactResult,
    SubmitResult,
)
from contracts.states import ArtifactType, FailureClass, JobStatus, JobType, ProviderCall
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.errors import (
    ArtifactConflictError,
    ProductionInputInvalidError,
    ProductionInputMissingError,
    TransientError,
    classify_failure,
)
from domain.job.transitions import job_event_for_failure
from domain.production.identity import image_input_hash
from domain.production.media import TARGET_HEIGHT, TARGET_WIDTH, MediaProbe, validate_image
from domain.production.ports import ImageGenerator, ImageRequest
from domain.production.prompting import DEFAULT_IMAGE_STYLE, ImageStyleProfile, build_image_prompt
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.media.normalize import normalize_image_9x16
from infrastructure.production.activity_errors import (
    AWAIT_ROUND_FINAL_ERRORS,
    raise_activity_error,
    translate_error,
)
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Reused
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256

logger = logging.getLogger(__name__)

_JOB_DONE = frozenset({JobStatus.SUCCEEDED, JobStatus.SKIPPED, JobStatus.TERMINAL_FAILED})
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


@dataclass(frozen=True)
class _LoadedStoryboard:
    meta: ArtifactMetadata
    storyboard: StoryboardArtifact
    scene: StoryboardScene


class ImageProductionActivities:
    """外部依存をすべて注入する（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        generator: ImageGenerator,
        probe: MediaProbe,
        runner: PaidJobRunner,
        bucket: str,
        poll_interval_seconds: float,
        await_deadline_seconds: float | None = None,
        style: ImageStyleProfile = DEFAULT_IMAGE_STYLE,
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
        self._style = style
        self._heartbeat = heartbeat

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.submit, self.await_image]

    # ------------------------------------------------------------------ submit

    @activity.defn(name=IMAGE_SUBMIT)
    async def submit(self, request: ImageSubmitRequest) -> SubmitResult:
        """ドメイン例外は型名つき ``ApplicationError``、インフラの一時障害は ``TransientError``。"""
        try:
            return await self._submit(request)
        except Exception as exc:
            raise_activity_error(exc)

    async def _submit(self, request: ImageSubmitRequest) -> SubmitResult:
        loaded = await self._load_storyboard(
            request.episode_id, request.storyboard_artifact_id, request.scene_id
        )
        input_hash = self._input_hash(loaded)
        job_id = await self._open_job(request.episode_id, request.scene_id)
        try:
            spec = PaidJobSpec(
                episode_id=request.episode_id,
                scene_id=request.scene_id,
                provider=ProviderCall.FAL_IMAGE,
                artifact_type=ArtifactType.SCENE_IMAGE,
                input_hash=input_hash,
                round=request.round,
                job_id=job_id,
            )
            # 再利用なら job を skipped にするので、start は再利用判定の後
            async with self._session_factory() as session:
                existing = await ArtifactMetadataRepository(session).find_current(
                    request.episode_id, ArtifactType.SCENE_IMAGE, input_hash, request.scene_id
                )
            if existing is None:
                await self._start_job(job_id)
            outcome = await self._runner.submit(spec, self._generator, self._image_request(loaded))
        except Exception as exc:
            await self._mark_job_failed(job_id, exc)
            raise
        if isinstance(outcome, Reused):
            await self._skip_job(job_id)
            return SubmitResult(reservation_id="", artifact=_result(outcome.artifact, reused=True))
        return SubmitResult(reservation_id=outcome.reservation_id)

    # ------------------------------------------------------------------ await

    @activity.defn(name=IMAGE_AWAIT)
    async def await_image(self, request: ImageAwaitRequest) -> SceneArtifactResult:
        """ラウンド確定済みの失敗（``AWAIT_ROUND_FINAL_ERRORS``）は Activity の retry を止める。

        型名は変えないので workflow は retryable として新ラウンドへ進む（ADR-0017 §4）。
        """
        try:
            return await self._await_image(request)
        except Exception as exc:
            raise_activity_error(exc, final_for_activity=AWAIT_ROUND_FINAL_ERRORS)

    async def _await_image(self, request: ImageAwaitRequest) -> SceneArtifactResult:
        loaded = await self._load_storyboard(
            request.episode_id, request.storyboard_artifact_id, request.scene_id
        )
        input_hash = self._input_hash(loaded)
        job_id = await self._open_job(request.episode_id, request.scene_id)
        try:
            return await self._await(request, loaded, input_hash, job_id)
        except Exception as exc:
            await self._mark_job_failed(job_id, exc)
            raise

    async def _await(
        self,
        request: ImageAwaitRequest,
        loaded: _LoadedStoryboard,
        input_hash: str,
        job_id: str,
    ) -> SceneArtifactResult:
        async with self._session_factory() as session:
            reservation = await ProviderReservationRepository(session).get(request.reservation_id)
        if reservation is None:
            raise ProductionInputMissingError(f"reservation {request.reservation_id} not found")
        if (
            reservation.episode_id != request.episode_id
            or reservation.scene_id != request.scene_id
            or reservation.input_hash != input_hash
            or reservation.provider is not ProviderCall.FAL_IMAGE
        ):
            raise ProductionInputInvalidError(
                f"reservation {reservation.id} does not match the requested scene image input"
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
        normalized = normalize_image_9x16(output.data)
        info = self._probe.probe_image(normalized.data)
        validate_image(info, len(normalized.data))

        media_sha = sha256_hex(normalized.data)
        media_key = media_object_key(
            request.episode_id,
            ArtifactType.SCENE_IMAGE.value,
            request.scene_id,
            media_sha,
            _IMAGE_EXT[normalized.mime],
        )
        put_media = await self._store.put_bytes(media_key, normalized.data, normalized.mime)
        media_readback = await readback_sha256(self._store, put_media.key)
        if media_readback != media_sha or put_media.sha256 != media_sha:
            raise ArtifactConflictError(
                f"scene image readback sha256 mismatch at {put_media.key}: "
                f"expected={media_sha} put={put_media.sha256} readback={media_readback}"
            )

        artifact = build_scene_image_artifact(
            episode_id=request.episode_id,
            source_storyboard={
                "artifact_id": loaded.meta.id,
                "sha256": loaded.meta.sha256,
                "schema_version": loaded.meta.schema_version,
            },
            scene_id=request.scene_id,
            media={
                "object_key": put_media.key,
                "sha256": media_sha,
                "bytes": len(normalized.data),
                "mime": normalized.mime,
            },
            width=info.width,
            height=info.height,
            generator={
                "generator": self._generator.generator_id,
                "generator_model": _model_label(self._generator),
                "generation_profile_id": self._generator.generation_profile_id,
            },
        )
        digest = sha256_hex(canonical_json_bytes(artifact))
        object_key = artifact_object_key(
            request.episode_id, ArtifactType.SCENE_IMAGE.value, digest, request.scene_id
        )
        put = await self._store.put_json(object_key, artifact)
        readback = sha256_hex(canonical_json_bytes(await self._store.get_json(put.key)))
        if readback != digest or put.sha256 != digest:
            raise ArtifactConflictError(
                f"scene image artifact readback sha256 mismatch at {put.key}: "
                f"expected={digest} put={put.sha256} readback={readback}"
            )

        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.SCENE_IMAGE,
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

    # ------------------------------------------------------------------ 入力

    async def _load_storyboard(
        self, episode_id: str, artifact_id: str, scene_id: str
    ) -> _LoadedStoryboard:
        async with self._session_factory() as session:
            try:
                meta = await ArtifactMetadataRepository(session).get(artifact_id)
            except ValueError as exc:
                raise ProductionInputInvalidError(
                    f"storyboard artifact id is not a UUID: {artifact_id!r}"
                ) from exc
        if meta is None:
            raise ProductionInputMissingError(f"storyboard artifact {artifact_id} not found")
        if meta.episode_id != episode_id or meta.artifact_type is not ArtifactType.STORYBOARD:
            raise ProductionInputInvalidError(
                f"artifact {artifact_id} is not a storyboard of episode {episode_id}"
            )
        try:
            payload = await self._store.get_json(meta.object_key)
        except Exception as exc:
            if isinstance(translate_error(exc), TransientError):
                raise  # ストアの通信障害は入力の欠陥ではない（境界で TransientError）
            raise ProductionInputInvalidError(
                f"storyboard {meta.object_key} is not readable: {type(exc).__name__}"
            ) from exc
        digest = sha256_hex(canonical_json_bytes(payload))
        if digest != meta.sha256:
            raise ProductionInputInvalidError(
                f"storyboard sha256 mismatch: stored={digest} metadata={meta.sha256}"
            )
        try:
            storyboard = StoryboardArtifact.model_validate(payload)
        except Exception as exc:
            raise ProductionInputInvalidError(f"storyboard invalid: {str(exc)[:500]}") from exc
        scene = next((s for s in storyboard.scenes if s.scene_id == scene_id), None)
        if scene is None:
            raise ProductionInputInvalidError(f"storyboard has no scene {scene_id}")
        return _LoadedStoryboard(meta=meta, storyboard=storyboard, scene=scene)

    def _input_hash(self, loaded: _LoadedStoryboard) -> str:
        scene = loaded.scene
        return image_input_hash(
            episode_id=loaded.storyboard.episode_id,
            artifact_type=ArtifactType.SCENE_IMAGE.value,
            schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
            storyboard_sha256=loaded.meta.sha256,
            scene_id=scene.scene_id,
            visual_description=scene.visual_description,
            visual_kind=scene.visual_kind.value,
            framing=scene.framing,
            style_profile_id=self._style.style_profile_id,
            generator_id=self._generator.generator_id,
            generation_profile_id=self._generator.generation_profile_id,
        )

    def _image_request(self, loaded: _LoadedStoryboard) -> ImageRequest:
        return ImageRequest(
            prompt=build_image_prompt(loaded.scene, self._style),
            width=TARGET_WIDTH,
            height=TARGET_HEIGHT,
            aspect="9:16",
        )

    # ------------------------------------------------------------------ job

    async def _open_job(self, episode_id: str, scene_id: str) -> str:
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_IMAGE, scene_id)
            if job is None:
                job = await jobs.create(
                    episode_id=episode_id, type=JobType.PRODUCE_SCENE_IMAGE, scene_id=scene_id
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
                await jobs.start(job_id)  # await の retry（retryable_failed / queued）から
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
            logger.warning("could not record %s job failure job=%s", "image", job_id, exc_info=True)


def _activity_heartbeat(*details: Any) -> None:
    if activity.in_activity():
        activity.heartbeat(*details)


def _model_label(generator: ImageGenerator) -> str:
    model = getattr(generator, "model_id", None)
    return model if isinstance(model, str) and model else generator.generator_id


def _result(meta: ArtifactMetadata, *, reused: bool) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=meta.id, object_key=meta.object_key, sha256=meta.sha256, reused=reused
    )


__all__ = ["ImageProductionActivities"]
