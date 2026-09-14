"""Render 工程の Activity（ADR-0019）。

- 入場（admit）/ 描画（render_final_video）/ 完了（mark_ready）/ 失敗（record_failure）
- **次に何をするかは決めない**（INV-4）。順序は ``RenderWorkflow`` が持つ
- 計画・同一性・技術検査は domain/render の純粋関数、描画と実測は注入した port（INV-18）
- 予約台帳には触れない（ローカル計算 / ADR-0019 §7）
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.artifacts import (
    ProductionManifest,
    SceneVideoArtifact,
    SceneVoiceArtifact,
    ScriptArtifact,
    StoryboardArtifact,
    build_final_video_artifact,
    parse_production_manifest,
    parse_scene_video_artifact,
    parse_scene_voice_artifact,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from contracts.render import (
    FINAL_VIDEO_MAX_BYTES,
    FINAL_VIDEO_MIME_TYPE,
    RENDER_ARTIFACT_SCHEMA_VERSION,
    RenderPlan,
    TimelinePolicy,
    get_render_profile,
)
from contracts.render_activities import (
    RENDER_ADMIT,
    RENDER_FINAL_VIDEO,
    RENDER_MARK_READY,
    RENDER_RECORD_FAILURE,
    RenderAdmitRequest,
    RenderAdmitResult,
    RenderFailureOutcome,
    RenderFinalVideoRequest,
    RenderFinalVideoResult,
    RenderMarkReadyRequest,
    RenderMarkReadyResult,
    RenderRecordFailureRequest,
)
from contracts.states import (
    JOB_TERMINAL_STATUSES,
    RENDER_ADMISSIBLE_STATUSES,
    RETRYABLE_FAILURE_CLASSES,
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
)
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, episode_media_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    DomainError,
    FinalVideoCorruptError,
    FinalVideoValidationError,
    MediaValidationError,
    RenderEngineUnavailableError,
    RenderInputIntegrityError,
    RenderInputMissingError,
    RenderInputStaleError,
    RenderWorkspaceFullError,
    UnknownRenderProfileError,
    classify_failure,
)
from domain.job.transitions import JobEvent, episode_event_for_failure, job_event_for_failure
from domain.production.manifest import ArtifactRef
from domain.render.identity import render_input_hash, render_plan_sha256
from domain.render.plan import Pinned, build_render_plan
from domain.render.ports import FinalVideoProbe, RenderEngine, RenderRequest
from domain.render.qa import measured_from_info, run_technical_qa
from domain.render.subtitles import subtitle_display_texts
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.production.activity_errors import raise_activity_error, translate_error
from infrastructure.render.binary import file_sha256
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256
from infrastructure.workdir import JobWorkDir, WorkDirectory
from workers.render.run_inspector import WorkflowRunInspector

logger = logging.getLogger(__name__)

#: render 工程へ入ってよい Episode 状態と、入場で適用する事象（ADR-0019 §7）。
ADMISSIBLE_STATUSES = RENDER_ADMISSIBLE_STATUSES
ADMIT_EVENTS: dict[EpisodeStatus, EpisodeEvent] = {
    EpisodeStatus.ASSETS_READY: EpisodeEvent.STAGE_ADMITTED,
    EpisodeStatus.RENDER_READY: EpisodeEvent.STAGE_ADMITTED,
    EpisodeStatus.NEEDS_WORK: EpisodeEvent.RETRY_ADMITTED,
    EpisodeStatus.BLOCKED: EpisodeEvent.RESUMED,
}
#: 再開は **render 自身が止めた** Episode だけ（入場トークンの workflow id で判定）。
#: 他工程（production 等）で止まった Episode を render から再開すると上流を飛ばす。
RESUMABLE_STATUSES = frozenset({EpisodeStatus.NEEDS_WORK, EpisodeStatus.BLOCKED})
#: record_failure が閉じる job の種類（render workflow が作る job だけ）。
WORKFLOW_OWNED_JOB_TYPES = frozenset({JobType.RENDER_FINAL_VIDEO})
#: 入力量から見積もる作業領域の使用量の倍率（ADR-0019 §10）。
WORKSPACE_ESTIMATE_FACTOR = 4
FINAL_VIDEO_FILENAME = "final.mp4"

DiskUsage = Callable[[str], Any]
HeartbeatFn = Callable[..., None]


def admission_token(workflow_id: str, run_id: str) -> str:
    """入場トークン（ADR-0017 §8 と同形）。workflow id は再利用されるので run id まで含める。"""
    return f"{workflow_id}:{run_id}"


def parse_admission_token(token: str | None) -> tuple[str, str] | None:
    """``workflow_id:run_id`` を分ける。形が違えば（他工程の相関 id 等）``None``。"""
    if not token or ":" not in token:
        return None
    workflow_id, run_id = token.rsplit(":", 1)
    if not workflow_id or not run_id:
        return None
    return workflow_id, run_id


def _activity_heartbeat(*details: Any) -> None:
    """Activity の中なら heartbeat を送る。外（単体テスト）では何もしない。"""
    try:
        activity.info()
    except RuntimeError:
        return
    activity.heartbeat(*details)


def required_free_bytes(*, min_free_bytes: int, input_bytes: int) -> int:
    """事前検査で要求する空き容量（ADR-0019 §10）。"""
    return min_free_bytes + WORKSPACE_ESTIMATE_FACTOR * input_bytes


@dataclass(frozen=True)
class _Loaded:
    meta: ArtifactMetadata
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Inputs:
    """PostgreSQL の現行メタデータと MinIO の本体を突き合わせ、契約で読んだ入力一式。"""

    manifest: ProductionManifest
    manifest_meta: ArtifactMetadata
    script: Pinned[ScriptArtifact]
    storyboard: Pinned[StoryboardArtifact]
    videos: dict[str, Pinned[SceneVideoArtifact]]
    voices: dict[str, Pinned[SceneVoiceArtifact]]

    @property
    def input_bytes(self) -> int:
        return sum(v.artifact.media.bytes for v in self.videos.values()) + sum(
            v.artifact.media.bytes for v in self.voices.values()
        )


def _pin(meta: ArtifactMetadata) -> ArtifactRef:
    return ArtifactRef(artifact_id=meta.id, sha256=meta.sha256, schema_version=meta.schema_version)


class RenderActivities:
    """外部依存をすべて注入する（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
        workdir: WorkDirectory,
        engine: RenderEngine,
        probe: FinalVideoProbe,
        font_path: str | Path,
        font_sha256: str,
        render_timeout_seconds: int,
        min_free_bytes: int,
        policy: TimelinePolicy | None = None,
        run_inspector: WorkflowRunInspector | None = None,
        disk_usage: DiskUsage = shutil.disk_usage,
        heartbeat: HeartbeatFn = _activity_heartbeat,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket
        self._workdir = workdir
        self._engine = engine
        self._probe = probe
        self._font_path = Path(font_path)
        self._font_sha256 = font_sha256.strip().lower()
        self._timeout_seconds = render_timeout_seconds
        self._min_free_bytes = min_free_bytes
        self._policy = policy or TimelinePolicy()
        #: 無ければ ``in_progress`` の引き継ぎをしない（安全側）
        self._run_inspector = run_inspector
        self._disk_usage = disk_usage
        self._heartbeat = heartbeat

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.admit, self.render_final_video, self.mark_ready, self.record_failure]

    # ------------------------------------------------------------------ 入場

    @activity.defn(name=RENDER_ADMIT)
    async def admit(self, request: RenderAdmitRequest) -> RenderAdmitResult:
        """入場: ``ADMIT_EVENTS`` の状態 → ``in_progress`` + 入場トークン + 描画 job。

        規則は production の admit と同じ（ADR-0017 §8）:
        - ``in_progress``: トークン完全一致なら通す。同じ workflow id の**閉じた** run なら引き継ぐ
        - ``needs_work`` / ``blocked``: 記録されたトークンが同じ workflow id のときだけ再開する
        - それ以外は入れず、何も書かない
        """
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(request.episode_id)
            if episode is None:
                return RenderAdmitResult(admitted=False, status="")
            owner = await episodes.get_workflow_id(request.episode_id)
            if episode.status is EpisodeStatus.IN_PROGRESS:
                if owner == token:
                    await self._ensure_job(session, request.episode_id)
                    await session.commit()
                    return RenderAdmitResult(admitted=True, status=episode.status.value)
                if not await self._stale_render_run(owner, request.workflow_id):
                    return RenderAdmitResult(admitted=False, status=episode.status.value)
                logger.warning(
                    "render admit takes over episode=%s from closed run %s", episode.id, owner
                )
                await episodes.set_workflow_id(request.episode_id, token)
                await self._ensure_job(session, request.episode_id)
                await session.commit()
                return RenderAdmitResult(admitted=True, status=episode.status.value)
            event = ADMIT_EVENTS.get(episode.status)
            if event is None:
                return RenderAdmitResult(admitted=False, status=episode.status.value)
            if episode.status in RESUMABLE_STATUSES:
                parsed = parse_admission_token(owner)
                if parsed is None or parsed[0] != request.workflow_id:
                    return RenderAdmitResult(admitted=False, status=episode.status.value)
            updated = await episodes.apply_event(request.episode_id, event)
            await episodes.set_workflow_id(request.episode_id, token)
            await self._ensure_job(session, request.episode_id)
            await session.commit()
            return RenderAdmitResult(admitted=True, status=updated.status.value)

    @staticmethod
    async def _ensure_job(session: AsyncSession, episode_id: str) -> str:
        """非終端の描画 job を再利用し、無ければ作る（再実行で重複生成しない）。"""
        jobs = JobRepository(session)
        job = await jobs.find_open(episode_id, JobType.RENDER_FINAL_VIDEO)
        if job is None:
            job = await jobs.create(episode_id=episode_id, type=JobType.RENDER_FINAL_VIDEO)
        return job.id

    async def _stale_render_run(self, owner: str | None, workflow_id: str) -> bool:
        parsed = parse_admission_token(owner)
        if parsed is None or parsed[0] != workflow_id or self._run_inspector is None:
            return False
        return await self._run_inspector.is_closed(parsed[0], parsed[1])

    # ------------------------------------------------------------------ 描画

    @activity.defn(name=RENDER_FINAL_VIDEO)
    async def render_final_video(self, request: RenderFinalVideoRequest) -> RenderFinalVideoResult:
        """現行マニフェストから完成動画を描き、技術検査に合格したものだけを保存する。

        同じ input_hash の現行 ``final_video`` があれば描画せず job を ``skipped``（INV-17）。
        失敗は job に記録してから ``ApplicationError(type=<型名>)`` で送出する。
        cancel（``asyncio.CancelledError``）はエンジンへ伝わり、作業領域を片付けてから再送出する。
        """
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job_id = await self._ensure_job(session, request.episode_id)
            job = await jobs.get(job_id)
            if job is not None and job.status in {JobStatus.QUEUED, JobStatus.RETRYABLE_FAILED}:
                await jobs.start(job_id)
            await session.commit()
        try:
            return await self._render(request, job_id)
        except Exception as exc:
            mapped = _map_os_error(exc)
            await self._mark_job_failed(job_id, translate_error(mapped))
            raise_activity_error(mapped)

    async def _render(
        self, request: RenderFinalVideoRequest, job_id: str
    ) -> RenderFinalVideoResult:
        episode_id = request.episode_id
        try:
            profile = get_render_profile(request.render_profile_id)
        except KeyError as exc:
            raise UnknownRenderProfileError(
                f"unknown render profile: {request.render_profile_id!r}"
            ) from exc

        inputs = await self._load_inputs(episode_id)
        self._heartbeat("inputs_resolved")
        self._verify_font()
        engine = self._engine.identity()
        plan = build_render_plan(
            manifest=inputs.manifest,
            script=inputs.script,
            storyboard=inputs.storyboard,
            voices=inputs.voices,
            videos=inputs.videos,
            profile=profile,
            policy=self._policy,
            engine=engine,
        )
        input_hash = render_input_hash(
            manifest_sha256=inputs.manifest_meta.sha256,
            script_sha256=inputs.script.ref.sha256,
            storyboard_sha256=inputs.storyboard.ref.sha256,
            profile=profile,
            policy=self._policy,
            engine=engine,
            font_sha256=self._font_sha256,
        )

        async with self._session_factory() as session:
            existing = await ArtifactMetadataRepository(session).find_current(
                episode_id=episode_id, artifact_type=ArtifactType.FINAL_VIDEO, input_hash=input_hash
            )
            if existing is not None:
                await self._finish_job(session, job_id, skipped=True)
                await session.commit()
                logger.info(
                    "render skipped episode=%s: current final_video %s has the same input",
                    episode_id,
                    existing.id,
                )
                return _result(existing, skipped=True, job_id=job_id)

        self._preflight_disk(inputs.input_bytes)
        work = self._workdir.create(episode_id, job_id)
        try:
            video_paths, voice_paths = await self._download_media(inputs, work)
            self._heartbeat("media_downloaded")
            rendered = await self._engine.render(
                RenderRequest(
                    plan=plan,
                    scene_video_paths=video_paths,
                    voice_paths=voice_paths,
                    subtitle_texts=subtitle_display_texts(
                        inputs.script.artifact, plan.subtitle_cues, profile.subtitles
                    ),
                    font_path=self._font_path,
                    work_dir=work.tmp,
                    output_path=work.output / FINAL_VIDEO_FILENAME,
                    timeout_seconds=self._timeout_seconds,
                ),
                heartbeat=lambda: self._heartbeat("rendering"),
            )
            self._heartbeat("rendered")
            return await self._store_final(request, job_id, inputs, plan, input_hash, rendered.path)
        finally:
            try:
                self._workdir.cleanup(episode_id, job_id)
            except DomainError:
                logger.warning("render work directory cleanup failed job=%s", job_id, exc_info=True)

    async def _store_final(
        self,
        request: RenderFinalVideoRequest,
        job_id: str,
        inputs: _Inputs,
        plan: RenderPlan,
        input_hash: str,
        path: Path,
    ) -> RenderFinalVideoResult:
        episode_id = request.episode_id
        try:
            info = await asyncio.to_thread(self._probe.probe_final_video, str(path))
        except MediaValidationError as exc:
            raise FinalVideoCorruptError(f"final video is not decodable: {exc}") from exc
        self._heartbeat("probed")
        size = path.stat().st_size
        if size <= 0 or size > FINAL_VIDEO_MAX_BYTES:
            raise FinalVideoValidationError(
                f"final video is {size} bytes (allowed 1..{FINAL_VIDEO_MAX_BYTES})"
            )
        media_sha = await asyncio.to_thread(file_sha256, path)
        media_key = episode_media_object_key(
            episode_id, ArtifactType.FINAL_VIDEO.value, media_sha, "mp4"
        )
        put = await self._store.put_bytes(media_key, path, FINAL_VIDEO_MIME_TYPE)
        media_readback = await readback_sha256(self._store, put.key)
        self._heartbeat("media_stored")
        if put.sha256 != media_sha or media_readback != media_sha:
            raise FinalVideoCorruptError(
                f"final video readback sha256 mismatch at {put.key}: expected={media_sha} "
                f"put={put.sha256} readback={media_readback}"
            )
        # 合格したときだけ保存する（不合格は FinalVideoValidationError / FinalVideoCorruptError）
        report = run_technical_qa(
            plan,
            plan.profile,
            info,
            media_bytes=size,
            readback_sha_ok=True,
            sources_verified=True,
            manifest=inputs.manifest,
        )

        def _src(artifact_id: str, sha256: str) -> dict[str, str]:
            return {"artifact_id": artifact_id, "sha256": sha256, "schema_version": "1.0"}

        try:
            payload = build_final_video_artifact(
                episode_id=episode_id,
                source_production_manifest=_src(
                    inputs.manifest_meta.id, inputs.manifest_meta.sha256
                ),
                source_script=_src(inputs.script.ref.artifact_id, inputs.script.ref.sha256),
                source_storyboard=_src(
                    inputs.storyboard.ref.artifact_id, inputs.storyboard.ref.sha256
                ),
                render_profile=plan.profile,
                render_policy=plan.policy,
                render_plan_sha256=render_plan_sha256(plan),
                render_engine=plan.engine,
                template_version=plan.policy.template_version,
                media={
                    "object_key": put.key,
                    "sha256": media_sha,
                    "bytes": size,
                    "mime": FINAL_VIDEO_MIME_TYPE,
                },
                measured=measured_from_info(info),
                total_duration_ms=plan.total_duration_ms,
                timeline=plan.scenes,
                voice_placements=plan.voices,
                subtitle_cues=plan.subtitle_cues,
                technical_qa=report,
            )
        except ValueError as exc:  # pydantic ValidationError を含む
            raise FinalVideoValidationError(f"final_video contract: {str(exc)[:500]}") from exc

        digest = sha256_hex(canonical_json_bytes(payload))
        key = artifact_object_key(episode_id, ArtifactType.FINAL_VIDEO.value, digest)
        stored = await self._store.put_json(key, payload)
        readback = sha256_hex(canonical_json_bytes(await self._store.get_json(stored.key)))
        if stored.sha256 != digest or readback != digest:
            raise FinalVideoCorruptError(
                f"final_video readback sha256 mismatch at {stored.key}: expected={digest} "
                f"put={stored.sha256} readback={readback}"
            )
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=episode_id,
                artifact_type=ArtifactType.FINAL_VIDEO,
                schema_version=RENDER_ARTIFACT_SCHEMA_VERSION,
                bucket=self._bucket,
                object_key=stored.key,
                sha256=digest,
                size_bytes=stored.size,
                produced_by_job_id=job_id,
                input_hash=input_hash,
            )
            await self._finish_job(session, job_id, skipped=False)
            await session.commit()
        logger.info(
            "final video rendered episode=%s artifact=%s version=%s bytes=%s",
            episode_id,
            meta.id,
            meta.version,
            size,
        )
        return _result(meta, skipped=False, job_id=job_id)

    # ------------------------------------------------------------------ 入力

    async def _load_json(self, meta: ArtifactMetadata, label: str) -> _Loaded:
        """オブジェクトを読み、正規形の sha256 をメタデータと照合する。"""
        try:
            payload = await self._store.get_json(meta.object_key)
        except KeyError as exc:
            raise RenderInputMissingError(
                f"{label} artifact {meta.id} object {meta.object_key} is missing"
            ) from exc
        except ValueError as exc:
            raise RenderInputIntegrityError(
                f"{label} artifact {meta.id} is not valid JSON: {exc}"
            ) from exc
        digest = sha256_hex(canonical_json_bytes(payload))
        if digest != meta.sha256:
            raise RenderInputIntegrityError(
                f"{label} artifact {meta.id} sha256 mismatch: stored={digest} "
                f"metadata={meta.sha256}"
            )
        return _Loaded(meta=meta, payload=payload)

    async def _referenced(
        self,
        episode_id: str,
        artifact_type: ArtifactType,
        artifact_id: str,
        sha256: str,
        label: str,
        scene_id: str | None = None,
    ) -> _Loaded:
        """マニフェストが指す Artifact を引き、**現行であること**と sha256 を確かめて読む。"""
        async with self._session_factory() as session:
            repo = ArtifactMetadataRepository(session)
            meta = await repo.get(artifact_id)
            current = await repo.find_current_by_type(episode_id, artifact_type, scene_id)
        if meta is None or meta.artifact_type is not artifact_type:
            raise RenderInputMissingError(f"{label} artifact {artifact_id} not found")
        if meta.sha256 != sha256:
            raise RenderInputIntegrityError(
                f"{label} artifact {artifact_id} sha256 {meta.sha256} != referenced {sha256}"
            )
        if current is None or current.id != meta.id:
            raise RenderInputStaleError(
                f"{label} artifact {artifact_id} is not current "
                f"(current={current.id if current else None}); re-run production"
            )
        return await self._load_json(meta, label)

    async def _load_inputs(self, episode_id: str) -> _Inputs:
        """現行マニフェストと、それが固定した入力を読む。

        相互の整合（網羅・同じ storyboard / 台本由来）は ``build_render_plan`` が検査する。
        ここは PG に在る・現行・本体の sha256 がメタデータと一致・契約で読める、まで。
        """
        async with self._session_factory() as session:
            manifest_meta = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.PRODUCTION_MANIFEST
            )
        if manifest_meta is None:
            raise RenderInputMissingError(f"no current production_manifest for {episode_id}")
        manifest_loaded = await self._load_json(manifest_meta, "production_manifest")
        try:
            manifest = parse_production_manifest(manifest_loaded.payload)
        except ValueError as exc:
            raise RenderInputIntegrityError(f"production_manifest invalid: {exc}") from exc
        if manifest.episode_id != episode_id:
            raise RenderInputIntegrityError(
                f"manifest episode {manifest.episode_id} != {episode_id}"
            )

        script_ref, sb_ref = manifest.source_script, manifest.source_storyboard
        script_loaded = await self._referenced(
            episode_id, ArtifactType.SCRIPT, script_ref.artifact_id, script_ref.sha256, "script"
        )
        sb_loaded = await self._referenced(
            episode_id,
            ArtifactType.STORYBOARD,
            sb_ref.artifact_id,
            sb_ref.sha256,
            "storyboard",
        )
        script = _parse(parse_script_artifact, script_loaded, "script")
        storyboard = _parse(parse_storyboard_artifact, sb_loaded, "storyboard")

        videos: dict[str, Pinned[SceneVideoArtifact]] = {}
        for scene in manifest.scenes:
            label = f"scene_video {scene.scene_id}"
            loaded = await self._referenced(
                episode_id,
                ArtifactType.SCENE_VIDEO,
                scene.video.artifact_id,
                scene.video.sha256,
                label,
                scene.scene_id,
            )
            video = _parse(parse_scene_video_artifact, loaded, label)
            if video.source_storyboard.sha256 != sb_loaded.meta.sha256:
                raise RenderInputStaleError(f"{label} was made for another storyboard")
            videos[scene.scene_id] = Pinned(artifact=video, ref=_pin(loaded.meta))
            self._heartbeat("input", scene.scene_id)

        voices: dict[str, Pinned[SceneVoiceArtifact]] = {}
        for ref in manifest.voices:
            label = f"scene_voice {ref.script_scene_id}"
            loaded = await self._referenced(
                episode_id,
                ArtifactType.SCENE_VOICE,
                ref.artifact_id,
                ref.sha256,
                label,
                ref.script_scene_id,
            )
            voice = _parse(parse_scene_voice_artifact, loaded, label)
            if voice.source_script.sha256 != script_loaded.meta.sha256:
                raise RenderInputStaleError(f"{label} was made for another script")
            voices[ref.script_scene_id] = Pinned(artifact=voice, ref=_pin(loaded.meta))
            self._heartbeat("input", ref.script_scene_id)

        return _Inputs(
            manifest=manifest,
            manifest_meta=manifest_meta,
            script=Pinned(artifact=script, ref=_pin(script_loaded.meta)),
            storyboard=Pinned(artifact=storyboard, ref=_pin(sb_loaded.meta)),
            videos=videos,
            voices=voices,
        )

    async def _download_media(
        self, inputs: _Inputs, work: JobWorkDir
    ) -> tuple[dict[str, Path], dict[str, Path]]:
        """素材本体を作業領域の input/ へ置く。読み戻した sha256 を記述子と照合する。"""
        video_paths: dict[str, Path] = {}
        for scene_id, pinned in inputs.videos.items():
            media = pinned.artifact.media
            path = work.input / f"video-{scene_id}.mp4"
            await self._fetch_verified(media.object_key, media.sha256, path, scene_id)
            video_paths[scene_id] = path
        voice_paths: dict[str, Path] = {}
        for script_scene_id, pinned in inputs.voices.items():
            media = pinned.artifact.media
            path = work.input / f"voice-{script_scene_id}.wav"
            await self._fetch_verified(media.object_key, media.sha256, path, script_scene_id)
            voice_paths[script_scene_id] = path
        return video_paths, voice_paths

    async def _fetch_verified(self, key: str, sha256: str, path: Path, label: str) -> None:
        try:
            data = await self._store.get_bytes(key)
        except KeyError as exc:
            raise RenderInputMissingError(f"media for {label} is missing at {key}") from exc
        actual = sha256_hex(data)
        if actual != sha256:
            raise RenderInputIntegrityError(
                f"media for {label} sha256 mismatch at {key}: stored={actual} expected={sha256}"
            )
        await asyncio.to_thread(path.write_bytes, data)
        self._heartbeat("downloaded", label)

    # ------------------------------------------------------------------ 前提検査

    def _verify_font(self) -> None:
        if not self._font_path.is_file():
            raise RenderEngineUnavailableError(f"subtitle font not found: {self._font_path}")
        actual = file_sha256(self._font_path)
        if actual != self._font_sha256:
            raise RenderEngineUnavailableError(
                f"subtitle font sha256 mismatch: expected {self._font_sha256}, got {actual}"
            )

    def _preflight_disk(self, input_bytes: int) -> None:
        """作業領域の空きを確かめる。足りなければ自動削除せず retryable で止まる。"""
        probe = self._workdir.root
        while not os.path.exists(probe) and probe.parent != probe:
            probe = probe.parent
        free = int(self._disk_usage(str(probe)).free)
        required = required_free_bytes(min_free_bytes=self._min_free_bytes, input_bytes=input_bytes)
        if free < required:
            raise RenderWorkspaceFullError(
                f"work root {self._workdir.root} has {free} bytes free, render needs {required} "
                f"(min_free {self._min_free_bytes} + {WORKSPACE_ESTIMATE_FACTOR} x input "
                f"{input_bytes})"
            )

    # ------------------------------------------------------------------ job

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
        try:
            async with self._session_factory() as session:
                jobs = JobRepository(session)
                job = await jobs.get(job_id)
                if job is None or job.status in JOB_TERMINAL_STATUSES:
                    return
                if (
                    job.status is JobStatus.RETRYABLE_FAILED
                    and failure_class in RETRYABLE_FAILURE_CLASSES
                ):
                    return
                await jobs.record_failure(
                    job_id,
                    event=job_event_for_failure(failure_class),
                    failure_class=failure_class,
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
                await session.commit()
        except Exception:
            # 記録できなくても元の失敗を送出する（record_failure が後で job を閉じる）
            logger.warning("render job failure could not be recorded job=%s", job_id, exc_info=True)

    # ------------------------------------------------------------------ 完了 / 失敗

    @activity.defn(name=RENDER_MARK_READY)
    async def mark_ready(self, request: RenderMarkReadyRequest) -> RenderMarkReadyResult:
        """入場トークンが一致する実行だけが ``render_ready`` へ進める。"""
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            current = await episodes.get(request.episode_id)
            if current is None:
                return RenderMarkReadyResult(status="", owned=False)
            if not await _owns(episodes, request.episode_id, token, "mark_ready"):
                return RenderMarkReadyResult(status=current.status.value, owned=False)
            if current.status is EpisodeStatus.RENDER_READY:
                return RenderMarkReadyResult(status=current.status.value)  # 再実行
            episode = await episodes.apply_event(request.episode_id, EpisodeEvent.RENDER_READY)
            await session.commit()
            return RenderMarkReadyResult(status=episode.status.value)

    @activity.defn(name=RENDER_RECORD_FAILURE)
    async def record_failure(self, request: RenderRecordFailureRequest) -> RenderFailureOutcome:
        """失敗クラス → Episode 事象（production の record_failure と同じ規則 / ADR-0017 §8）。

        - retryable を使い切った（``retry_exhausted``）なら ``needs_work`` → ``blocked``
          （``RETRY_BUDGET_EXHAUSTED``）。terminal にしない
        - 描画 job: ``job_id`` 指定または permanent なら失敗クラスの事象を適用。それ以外で
          ``queued`` / ``running`` のまま（cancel で中断）なら ``retryable_failed``（再開可能）
        """
        failure_class = FailureClass(request.failure_class)
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            if not await _owns(episodes, request.episode_id, token, "record_failure"):
                current = await episodes.get(request.episode_id)
                return RenderFailureOutcome(
                    episode_status=current.status.value if current else "", owned=False
                )
            await _settle_jobs(session, request, failure_class)
            current = await episodes.get(request.episode_id)
            if current is not None and current.status is not EpisodeStatus.IN_PROGRESS:
                await session.commit()
                return RenderFailureOutcome(episode_status=current.status.value)
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
            return RenderFailureOutcome(episode_status=episode.status.value)


async def _settle_jobs(
    session: AsyncSession, request: RenderRecordFailureRequest, failure_class: FailureClass
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
                    if failure_class in RETRYABLE_FAILURE_CLASSES
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
                error_summary=f"interrupted: render stopped ({summary})",
            )


async def _owns(episodes: EpisodeRepository, episode_id: str, token: str, action: str) -> bool:
    owner = await episodes.get_workflow_id(episode_id)
    if owner == token:
        return True
    logger.warning(
        "render %s refused: admission token mismatch episode=%s recorded=%s caller=%s",
        action,
        episode_id,
        owner,
        token,
    )
    return False


def _map_os_error(exc: BaseException) -> BaseException:
    """ディスク満杯（ENOSPC / EDQUOT）は一時障害ではなく ``RenderWorkspaceFullError``。"""
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        mapped = RenderWorkspaceFullError(f"no space left in the work directory: {exc}")
        mapped.__cause__ = exc
        return mapped
    return exc


def _parse[T](parser: Callable[[dict[str, Any]], T], loaded: _Loaded, label: str) -> T:
    try:
        return parser(loaded.payload)
    except ValueError as exc:  # pydantic ValidationError を含む
        raise RenderInputIntegrityError(
            f"{label} artifact {loaded.meta.id} invalid: {str(exc)[:500]}"
        ) from exc


def _result(meta: ArtifactMetadata, *, skipped: bool, job_id: str) -> RenderFinalVideoResult:
    return RenderFinalVideoResult(
        artifact_id=meta.id,
        sha256=meta.sha256,
        version=meta.version,
        skipped=skipped,
        job_id=job_id,
    )


__all__ = [
    "ADMISSIBLE_STATUSES",
    "ADMIT_EVENTS",
    "FINAL_VIDEO_FILENAME",
    "RESUMABLE_STATUSES",
    "WORKFLOW_OWNED_JOB_TYPES",
    "WORKSPACE_ESTIMATE_FACTOR",
    "RenderActivities",
    "admission_token",
    "parse_admission_token",
    "required_free_bytes",
]
