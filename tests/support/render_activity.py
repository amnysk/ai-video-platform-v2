"""Render Activity テストの共通部品（ADR-0019）。

- 現行の台本・storyboard・シーン動画・音声・マニフェストを記録する（メディアは偽のバイト列）
- 描画エンジン・計画・probe の小さな fake（``workers.render.ports`` の形）

本物の計画（domain/render）とエンジン（infrastructure/render）は使わない。Activity の
入出力・版管理・失敗の写像だけを検査するため。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.artifact_refs import ArtifactDigestRef
from contracts.artifacts import (
    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
    build_scene_video_artifact,
    build_scene_voice_artifact,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from contracts.render import (
    RenderEngineIdentity,
    RenderPlan,
    RenderProfile,
    RenderTimelineScene,
    RenderVoicePlacement,
    SceneReconciliation,
    TechnicalQaCheck,
    TechnicalQaReport,
    TimelinePolicy,
)
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.episode.transitions import EpisodeEvent
from domain.production.manifest import ArtifactRef, build_manifest
from domain.render.ports import FinalVideoInfo
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.storage.artifact_store import ArtifactStore
from tests.support.production import GENERATOR
from tests.support.voice import BUCKET, STORYBOARD_LAYOUT, record_inputs
from workers.render.ports import Heartbeat, RenderInputs, RenderJob

VOICE_DURATION_MS = 1_000
STORYBOARD_TOTAL_MS = 25_000
FAKE_IMAGE_SHA = "c" * 64

TO_ASSETS_READY = [
    EpisodeEvent.WORKFLOW_STARTED,
    EpisodeEvent.SCRIPT_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.STORYBOARD_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.ASSETS_READY,
]


@dataclass
class RenderSeed:
    episode_id: str
    script: ArtifactMetadata
    storyboard: ArtifactMetadata
    videos: dict[str, ArtifactMetadata] = field(default_factory=dict)
    voices: dict[str, ArtifactMetadata] = field(default_factory=dict)
    manifest: ArtifactMetadata | None = None


async def record_artifact(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    episode_id: str,
    artifact_type: ArtifactType,
    payload: dict[str, Any],
    *,
    scene_id: str | None = None,
) -> ArtifactMetadata:
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode_id, artifact_type.value, digest, scene_id)
    put = await store.put_json(key, payload)
    async with factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=artifact_type,
            schema_version=PRODUCTION_ARTIFACT_SCHEMA_VERSION,
            bucket=BUCKET,
            object_key=put.key,
            sha256=put.sha256,
            size_bytes=put.size,
            input_hash=sha256_hex(f"input:{digest}".encode()),
            scene_id=scene_id,
        )
        await session.commit()
    return meta


def _src(meta: ArtifactMetadata) -> dict[str, str]:
    return {"artifact_id": meta.id, "sha256": meta.sha256, "schema_version": "1.0"}


def image_id(episode_id: str, scene_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"image-{episode_id}-{scene_id}"))


async def record_scene_video(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    seed: RenderSeed,
    scene_id: str,
    duration_ms: int,
    *,
    salt: str = "",
) -> ArtifactMetadata:
    data = f"fake-mp4:{scene_id}:{salt}".encode()
    sha = sha256_hex(data)
    key = media_object_key(seed.episode_id, "scene_video", scene_id, sha, "mp4")
    await store.put_bytes(key, data, "video/mp4")
    payload = build_scene_video_artifact(
        episode_id=seed.episode_id,
        source_storyboard=_src(seed.storyboard),
        scene_id=scene_id,
        source_image={"artifact_id": image_id(seed.episode_id, scene_id), "sha256": FAKE_IMAGE_SHA},
        media={"object_key": key, "sha256": sha, "bytes": len(data), "mime": "video/mp4"},
        duration_ms=duration_ms,
        requested_duration_ms=duration_ms,
        width=1080,
        height=1920,
        fps_millis=24_000,
        generator=GENERATOR,
    )
    meta = await record_artifact(
        factory, store, seed.episode_id, ArtifactType.SCENE_VIDEO, payload, scene_id=scene_id
    )
    seed.videos[scene_id] = meta
    return meta


async def record_scene_voice(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    seed: RenderSeed,
    script_scene_id: str,
    storyboard_scene_ids: list[str],
) -> ArtifactMetadata:
    data = f"fake-wav:{script_scene_id}".encode()
    sha = sha256_hex(data)
    key = media_object_key(seed.episode_id, "scene_voice", script_scene_id, sha, "wav")
    await store.put_bytes(key, data, "audio/wav")
    payload = build_scene_voice_artifact(
        episode_id=seed.episode_id,
        source_storyboard=_src(seed.storyboard),
        source_script=_src(seed.script),
        script_scene_id=script_scene_id,
        storyboard_scene_ids=storyboard_scene_ids,
        language="ja",
        voice_id="fake-voice",
        media={"object_key": key, "sha256": sha, "bytes": len(data), "mime": "audio/wav"},
        duration_ms=VOICE_DURATION_MS,
        sample_rate_hz=22_050,
        channels=1,
        generator=GENERATOR,
    )
    meta = await record_artifact(
        factory,
        store,
        seed.episode_id,
        ArtifactType.SCENE_VOICE,
        payload,
        scene_id=script_scene_id,
    )
    seed.voices[script_scene_id] = meta
    return meta


async def record_manifest(
    factory: async_sessionmaker[AsyncSession], store: ArtifactStore, seed: RenderSeed
) -> ArtifactMetadata:
    storyboard = parse_storyboard_artifact(await store.get_json(seed.storyboard.object_key))
    script = parse_script_artifact(await store.get_json(seed.script.object_key))
    payload = build_manifest(
        episode_id=seed.episode_id,
        storyboard_ref=ArtifactRef(seed.storyboard.id, seed.storyboard.sha256),
        script_ref=ArtifactRef(seed.script.id, seed.script.sha256),
        storyboard=storyboard,
        script=script,
        images={
            s.scene_id: ArtifactRef(image_id(seed.episode_id, s.scene_id), FAKE_IMAGE_SHA)
            for s in storyboard.scenes
        },
        videos={k: ArtifactRef(m.id, m.sha256) for k, m in seed.videos.items()},
        voices={k: ArtifactRef(m.id, m.sha256) for k, m in seed.voices.items()},
    )
    seed.manifest = await record_artifact(
        factory, store, seed.episode_id, ArtifactType.PRODUCTION_MANIFEST, payload
    )
    return seed.manifest


async def seed_render_inputs(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    *,
    events: list[EpisodeEvent] | None = None,
    with_manifest: bool = True,
) -> RenderSeed:
    """``assets_ready`` の Episode と、render が読む現行 Artifact 一式を記録する。"""
    async with factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="render")
        for event in TO_ASSETS_READY if events is None else events:
            await episodes.apply_event(episode.id, event)
        await session.commit()
    script, storyboard = await record_inputs(factory, store, episode.id)
    seed = RenderSeed(episode_id=episode.id, script=script, storyboard=storyboard)
    by_script: dict[str, list[str]] = {}
    for index, (script_scene_id, _start, duration, _kind, _desc) in enumerate(
        STORYBOARD_LAYOUT, start=1
    ):
        await record_scene_video(factory, store, seed, f"sb{index}", duration)
        by_script.setdefault(script_scene_id, []).append(f"sb{index}")
    for script_scene_id, scene_ids in by_script.items():
        await record_scene_voice(factory, store, seed, script_scene_id, scene_ids)
    if with_manifest:
        await record_manifest(factory, store, seed)
    return seed


# --------------------------------------------------------------------------- fakes


class FakePlanning:
    """計画: storyboard の順と尺。音声は最初のシーンの開始。字幕なし。"""

    def __init__(self) -> None:
        self.plan_error: BaseException | None = None
        self.qa_error: BaseException | None = None
        self.qa_calls: list[dict[str, Any]] = []

    def build_plan(
        self,
        inputs: RenderInputs,
        *,
        profile: RenderProfile,
        policy: TimelinePolicy,
        engine: RenderEngineIdentity,
    ) -> RenderPlan:
        if self.plan_error is not None:
            raise self.plan_error
        refs = {s.scene_id: s.video for s in inputs.manifest.scenes}
        scenes: list[RenderTimelineScene] = []
        starts: dict[str, int] = {}
        cursor = 0
        for order, sb in enumerate(inputs.storyboard.scenes, start=1):
            source = inputs.videos[sb.scene_id].duration_ms
            target = sb.duration_ms
            if source == target:
                recon = SceneReconciliation(mode="exact", trim_ms=0, freeze_ms=0)
            elif source > target:
                recon = SceneReconciliation(mode="trim", trim_ms=source - target, freeze_ms=0)
            else:
                recon = SceneReconciliation(mode="freeze_tail", trim_ms=0, freeze_ms=target - source)
            starts[sb.scene_id] = cursor
            scenes.append(
                RenderTimelineScene(
                    scene_id=sb.scene_id,
                    order=order,
                    source_video=ArtifactDigestRef(
                        artifact_id=refs[sb.scene_id].artifact_id, sha256=refs[sb.scene_id].sha256
                    ),
                    timeline_start_ms=cursor,
                    timeline_duration_ms=target,
                    requested_duration_ms=target,
                    source_duration_ms=source,
                    reconciliation=recon,
                )
            )
            cursor += target
        voices = tuple(
            RenderVoicePlacement(
                script_scene_id=ref.script_scene_id,
                source_voice=ArtifactDigestRef(artifact_id=ref.artifact_id, sha256=ref.sha256),
                start_ms=starts[inputs.voices[ref.script_scene_id].storyboard_scene_ids[0]],
                duration_ms=inputs.voices[ref.script_scene_id].duration_ms,
                storyboard_scene_ids=inputs.voices[ref.script_scene_id].storyboard_scene_ids,
            )
            for ref in inputs.manifest.voices
        )
        return RenderPlan(
            scenes=tuple(scenes),
            voices=voices,
            subtitle_cues=(),
            total_duration_ms=cursor,
            profile=profile,
            policy=policy,
            engine=engine,
        )

    def plan_sha256(self, plan: RenderPlan) -> str:
        return sha256_hex(canonical_json_bytes(plan.model_dump(mode="json")))

    def input_hash(
        self,
        *,
        manifest_sha256: str,
        script_sha256: str,
        storyboard_sha256: str,
        profile: RenderProfile,
        policy: TimelinePolicy,
        engine: RenderEngineIdentity,
        font_sha256: str,
    ) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "stage": "render",
                    "manifest": manifest_sha256,
                    "script": script_sha256,
                    "storyboard": storyboard_sha256,
                    "profile": profile.model_dump(mode="json"),
                    "policy": policy.model_dump(mode="json"),
                    "engine": engine.model_dump(mode="json"),
                    "font": font_sha256,
                }
            )
        )

    def subtitle_texts(self, plan: RenderPlan, script: Any) -> list[str]:
        return []

    def technical_qa(
        self,
        *,
        plan: RenderPlan,
        info: FinalVideoInfo,
        media_sha256: str,
        media_readback_sha256: str,
    ) -> TechnicalQaReport:
        self.qa_calls.append({"media_sha256": media_sha256, "readback": media_readback_sha256})
        if self.qa_error is not None:
            raise self.qa_error
        return TechnicalQaReport(
            passed=True,
            checks=(TechnicalQaCheck(check="fake_decodable", passed=True, detail="ok"),),
        )

    def final_media_key(self, episode_id: str, sha256: str) -> str:
        return f"media/{episode_id}/final_video/{sha256}.mp4"


class FakeRenderer:
    """書いたバイト列に profile id を混ぜる（profile が違えば本体の sha256 も違う）。"""

    def __init__(self) -> None:
        self.jobs: list[RenderJob] = []
        self.errors: list[BaseException] = []
        self.hang = False
        self.cancelled = False
        self.started = asyncio.Event()

    def identity(self) -> RenderEngineIdentity:
        return RenderEngineIdentity(engine="fake-engine", version="1.0", binary_sha256="e" * 64)

    async def render(self, job: RenderJob, heartbeat: Heartbeat) -> Path:
        self.jobs.append(job)
        heartbeat()
        missing = [p for p in (*job.scene_video_paths.values(), *job.voice_paths.values())]
        assert all(p.is_file() for p in missing), "inputs must be in the work directory"
        self.started.set()
        if self.errors:
            raise self.errors.pop(0)
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        job.output_path.write_bytes(f"fake-final:{job.plan.profile.profile_id}".encode())
        return job.output_path


class FakeFinalVideoProbe:
    def __init__(
        self, *, width: int = 1080, height: int = 1920, duration_ms: int = STORYBOARD_TOTAL_MS
    ) -> None:
        self.width = width
        self.height = height
        self.duration_ms = duration_ms
        self.error: BaseException | None = None

    def probe_final_video(self, path: str) -> FinalVideoInfo:
        if self.error is not None:
            raise self.error
        return FinalVideoInfo(
            duration_ms=self.duration_ms,
            width=self.width,
            height=self.height,
            fps_millis=30_000,
            frames_decoded=self.duration_ms * 30 // 1000,
            decode_errors=0,
            video_codec="h264",
            pix_fmt="yuv420p",
            audio_present=True,
            audio_codec="aac",
            audio_sample_rate_hz=48_000,
            audio_channels=2,
            audio_duration_ms=self.duration_ms,
            bytes=os.path.getsize(path),
        )


__all__ = [
    "FAKE_IMAGE_SHA",
    "STORYBOARD_TOTAL_MS",
    "TO_ASSETS_READY",
    "VOICE_DURATION_MS",
    "FakeFinalVideoProbe",
    "FakePlanning",
    "FakeRenderer",
    "RenderSeed",
    "record_artifact",
    "record_manifest",
    "record_scene_video",
    "record_scene_voice",
    "seed_render_inputs",
]
