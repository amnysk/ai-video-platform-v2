"""Production Voice Worker の Activity（ADR-0017 / ADR-0018 / Phase 4B）。

台本シーン1件のナレーション音声を作る。**次に何をするかは決めない**（INV-4）。
他の worker を import しない（INV-3）。workflow とは ``contracts.production_activities`` の
名前と型だけを共有する。

音声合成はローカル・非課金なので予約台帳を通さず、Temporal の retry（``VOICE_MAX_ATTEMPTS``）に
委ねる（ADR-0017 §5）。ナレーション文は台本 Artifact から読み、どこへも複製しない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from contracts.artifacts import (
    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
    ScriptArtifact,
    StoryboardArtifact,
    build_scene_voice_artifact,
)
from contracts.production_activities import (
    VOICE_GENERATE,
    VOICE_MAX_ATTEMPTS,
    SceneArtifactResult,
    VoiceGenerateRequest,
)
from contracts.states import ArtifactType, FailureClass, JobStatus, JobType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.errors import (
    ArtifactConflictError,
    DomainError,
    ProductionInputInvalidError,
    ProductionInputMissingError,
    classify_failure,
)
from domain.job.transitions import job_event_for_failure
from domain.production.identity import narration_sha256, voice_input_hash
from domain.production.media import MediaProbe, validate_voice
from domain.production.ports import VoiceGenerator
from infrastructure.db.repositories import ArtifactMetadataRepository, JobRepository
from infrastructure.media.destination import FileMediaDestination
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256
from infrastructure.workdir import WorkDirectory

logger = logging.getLogger(__name__)

VOICE_MIME = "audio/wav"
VOICE_EXTENSION = "wav"
#: 話速を公開しない生成器の既定（等速）
DEFAULT_SPEED_PERMILLE = 1000

_JOB_DONE = frozenset({JobStatus.SUCCEEDED, JobStatus.SKIPPED, JobStatus.TERMINAL_FAILED})


@dataclass(frozen=True)
class _Loaded:
    meta: ArtifactMetadata
    payload: dict[str, Any]


def compute_voice_input_hash(
    *,
    episode_id: str,
    script_sha256: str,
    storyboard_sha256: str,
    script_scene_id: str,
    storyboard_scene_ids: Sequence[str],
    narration: str,
    language: str,
    generator: VoiceGenerator,
) -> str:
    """``voice_input_hash`` の呼び出しを1箇所に閉じる。

    ``storyboard_sha256`` / ``storyboard_scene_ids`` は foundation 側の修正で
    ``voice_input_hash`` の引数に加わる予定（merge 時にここへ渡す）。現時点の関数は受け取らない。
    """
    del storyboard_sha256, storyboard_scene_ids  # TODO(merge): voice_input_hash へ渡す
    return voice_input_hash(
        episode_id=episode_id,
        artifact_type=ArtifactType.SCENE_VOICE.value,
        schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
        script_sha256=script_sha256,
        script_scene_id=script_scene_id,
        narration_sha256=narration_sha256(narration),
        voice_id=generator.voice_id,
        language=language,
        speed_permille=int(getattr(generator, "speed_permille", DEFAULT_SPEED_PERMILLE)),
        generator_id=generator.generator_id,
        generation_profile_id=generator.generation_profile_id,
    )


class VoiceActivities:
    """外部依存をすべて注入する。テストは実サービスなしで同じコードパスを走らせる（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        generator: VoiceGenerator,
        probe: MediaProbe,
        workdir: WorkDirectory,
        bucket: str,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._generator = generator
        self._probe = probe
        self._workdir = workdir
        self._bucket = bucket

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.generate_voice]

    @activity.defn(name=VOICE_GENERATE)
    async def generate_voice(self, request: VoiceGenerateRequest) -> SceneArtifactResult:
        """ドメイン例外は型名を ``type`` にした ``ApplicationError`` にする。

        needs_input / permanent は ``non_retryable``。未分類の例外はそのまま投げ、
        Temporal の retry と workflow の型名分類（INV-12）に委ねる。
        """
        try:
            return await self._generate(request)
        except DomainError as exc:
            failure_class = classify_failure(exc)
            raise ApplicationError(
                f"{type(exc).__name__}: {exc}",
                type=type(exc).__name__,
                non_retryable=failure_class in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT},
            ) from exc

    async def _generate(self, request: VoiceGenerateRequest) -> SceneArtifactResult:
        storyboard_loaded = await self._load_current(
            request.episode_id, ArtifactType.STORYBOARD, request.storyboard_artifact_id
        )
        script_loaded = await self._load_current(
            request.episode_id, ArtifactType.SCRIPT, request.script_artifact_id
        )
        storyboard = _parse(StoryboardArtifact, storyboard_loaded)
        script = _parse(ScriptArtifact, script_loaded)
        if (
            storyboard.source_script.artifact_id != script_loaded.meta.id
            or storyboard.source_script.sha256 != script_loaded.meta.sha256
        ):
            raise ProductionInputInvalidError(
                f"storyboard {storyboard_loaded.meta.id} was not made from current script "
                f"{script_loaded.meta.id}"
            )

        scene = next((s for s in script.scenes if s.id == request.script_scene_id), None)
        if scene is None:
            raise ProductionInputInvalidError(
                f"script scene {request.script_scene_id} not in script {script_loaded.meta.id}"
            )
        storyboard_scene_ids = [
            s.scene_id for s in storyboard.scenes if s.script_scene_id == scene.id
        ]
        if not storyboard_scene_ids or storyboard_scene_ids != list(request.storyboard_scene_ids):
            raise ProductionInputInvalidError(
                f"storyboard scenes for {scene.id} are {storyboard_scene_ids}, "
                f"request says {list(request.storyboard_scene_ids)}"
            )

        input_hash = compute_voice_input_hash(
            episode_id=request.episode_id,
            script_sha256=script_loaded.meta.sha256,
            storyboard_sha256=storyboard_loaded.meta.sha256,
            script_scene_id=scene.id,
            storyboard_scene_ids=storyboard_scene_ids,
            narration=scene.narration,
            language=script.language,
            generator=self._generator,
        )

        # 同じ入力の現行 Artifact があれば合成しない（INV-17）
        async with self._session_factory() as session:
            existing = await ArtifactMetadataRepository(session).find_current(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.SCENE_VOICE,
                input_hash=input_hash,
                scene_id=scene.id,
            )
            if existing is not None:
                jobs = JobRepository(session)
                job = await self._open_job(jobs, request.episode_id, scene.id)
                await jobs.mark_skipped(job.id)
                await session.commit()
                return _result(existing, reused=True)

        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await self._open_job(jobs, request.episode_id, scene.id)
            await jobs.start(job.id)
            await session.commit()
            job_id = job.id

        try:
            meta = await self._produce(
                request=request,
                job_id=job_id,
                storyboard_meta=storyboard_loaded.meta,
                script_meta=script_loaded.meta,
                script_scene_id=scene.id,
                storyboard_scene_ids=storyboard_scene_ids,
                narration=scene.narration,
                language=script.language,
                input_hash=input_hash,
            )
        except Exception as exc:
            await self._mark_job_failed(job_id, exc)
            raise
        return _result(meta, reused=False)

    async def _produce(
        self,
        *,
        request: VoiceGenerateRequest,
        job_id: str,
        storyboard_meta: ArtifactMetadata,
        script_meta: ArtifactMetadata,
        script_scene_id: str,
        storyboard_scene_ids: list[str],
        narration: str,
        language: str,
        input_hash: str,
    ) -> ArtifactMetadata:
        work = self._workdir.create(request.episode_id, job_id)
        try:
            out = work.output / f"voice.{VOICE_EXTENSION}"
            out.unlink(missing_ok=True)  # retry で前の試行の残りに追記しない
            await self._generator.synthesize(narration, language, FileMediaDestination(out))
            data = out.read_bytes() if out.is_file() else b""
            info = self._probe.probe_audio(data)
            validate_voice(info, len(data))

            media_sha = sha256_hex(data)
            media_key = media_object_key(
                request.episode_id,
                ArtifactType.SCENE_VOICE.value,
                script_scene_id,
                media_sha,
                VOICE_EXTENSION,
            )
            put = await self._store.put_bytes(media_key, data, VOICE_MIME)
            readback = await readback_sha256(self._store, put.key)
            if put.sha256 != media_sha or readback != media_sha:
                raise ArtifactConflictError(
                    f"voice media readback sha256 mismatch at {put.key}: "
                    f"expected={media_sha} put={put.sha256} readback={readback}"
                )

            artifact = build_scene_voice_artifact(
                episode_id=request.episode_id,
                source_storyboard=_source_ref(storyboard_meta),
                source_script=_source_ref(script_meta),
                script_scene_id=script_scene_id,
                storyboard_scene_ids=storyboard_scene_ids,
                language=language,
                voice_id=self._generator.voice_id,
                media={
                    "object_key": put.key,
                    "sha256": media_sha,
                    "bytes": len(data),
                    "mime": VOICE_MIME,
                },
                duration_ms=info.duration_ms,
                sample_rate_hz=info.sample_rate_hz,
                channels=info.channels,
                generator={
                    "generator": self._generator.generator_id,
                    "generator_model": self._generator.voice_id,
                    "generation_profile_id": self._generator.generation_profile_id,
                },
            )
            digest = sha256_hex(canonical_json_bytes(artifact))
            key = artifact_object_key(
                request.episode_id, ArtifactType.SCENE_VOICE.value, digest, script_scene_id
            )
            stored = await self._store.put_json(key, artifact)
            artifact_readback = sha256_hex(canonical_json_bytes(await self._store.get_json(key)))
            if stored.sha256 != digest or artifact_readback != digest:
                raise ArtifactConflictError(
                    f"voice artifact readback sha256 mismatch at {key}: "
                    f"expected={digest} put={stored.sha256} readback={artifact_readback}"
                )

            async with self._session_factory() as session:
                meta = await ArtifactMetadataRepository(session).record(
                    episode_id=request.episode_id,
                    artifact_type=ArtifactType.SCENE_VOICE,
                    schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
                    bucket=self._bucket,
                    object_key=stored.key,
                    sha256=digest,
                    size_bytes=stored.size,
                    produced_by_job_id=job_id,
                    input_hash=input_hash,
                    scene_id=script_scene_id,
                )
                await JobRepository(session).succeed(job_id)
                await session.commit()
            logger.info(
                "voice generated episode=%s script_scene=%s duration_ms=%s artifact=%s",
                request.episode_id,
                script_scene_id,
                info.duration_ms,
                meta.id,
            )
            return meta
        finally:
            try:
                self._workdir.cleanup(request.episode_id, job_id)
            except DomainError:
                logger.warning("voice work directory cleanup failed job=%s", job_id, exc_info=True)

    # ------------------------------------------------------------------ 補助

    async def _load_current(
        self, episode_id: str, artifact_type: ArtifactType, expected_id: str
    ) -> _Loaded:
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, artifact_type
            )
        if meta is None:
            raise ProductionInputMissingError(
                f"no current {artifact_type.value} artifact for {episode_id}"
            )
        if meta.id != expected_id:
            raise ProductionInputInvalidError(
                f"{artifact_type.value} {expected_id} is not current (current is {meta.id})"
            )
        try:
            payload = await self._store.get_json(meta.object_key)
        except Exception as exc:
            raise ProductionInputInvalidError(
                f"{artifact_type.value} artifact {meta.object_key} is not readable: "
                f"{type(exc).__name__}"
            ) from exc
        digest = sha256_hex(canonical_json_bytes(payload))
        if digest != meta.sha256:
            raise ProductionInputInvalidError(
                f"{artifact_type.value} sha256 mismatch: stored={digest} metadata={meta.sha256}"
            )
        return _Loaded(meta=meta, payload=payload)

    @staticmethod
    async def _open_job(jobs: JobRepository, episode_id: str, scene_id: str):
        job = await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_VOICE, scene_id=scene_id)
        if job is None:
            job = await jobs.create(
                episode_id=episode_id,
                type=JobType.PRODUCE_SCENE_VOICE,
                max_attempts=VOICE_MAX_ATTEMPTS,
                scene_id=scene_id,
            )
        return job

    async def _mark_job_failed(self, job_id: str, exc: BaseException) -> None:
        """job に失敗を記録する。

        未分類の例外（ドメイン型でない）は Activity からそのまま投げ、Temporal が
        ``VOICE_MAX_ATTEMPTS`` まで retry する。その間 job を終端にすると次の試行が別 job を
        作ってしまうので、job 上は ``retryable`` として残す。retry が尽きたときの最終分類は
        workflow が型名で行う（未知の型名 → needs_input / INV-12）。
        """
        failure_class = (
            classify_failure(exc) if isinstance(exc, DomainError) else FailureClass.RETRYABLE
        )
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is None or job.status in _JOB_DONE:
                return
            if job.status is JobStatus.RETRYABLE_FAILED:
                return
            await jobs.record_failure(
                job_id,
                event=job_event_for_failure(failure_class),
                failure_class=failure_class,
                error_summary=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()


def _parse[T: (StoryboardArtifact, ScriptArtifact)](model: type[T], loaded: _Loaded) -> T:
    try:
        return model.model_validate(loaded.payload)
    except Exception as exc:  # pydantic ValidationError を含む
        raise ProductionInputInvalidError(
            f"{loaded.meta.artifact_type} artifact invalid: {str(exc)[:500]}"
        ) from exc


def _source_ref(meta: ArtifactMetadata) -> dict[str, str]:
    return {"artifact_id": meta.id, "sha256": meta.sha256, "schema_version": meta.schema_version}


def _result(meta: ArtifactMetadata, *, reused: bool) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=meta.id, object_key=meta.object_key, sha256=meta.sha256, reused=reused
    )


__all__ = ["VoiceActivities", "compute_voice_input_hash"]
