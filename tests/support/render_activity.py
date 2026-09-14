"""Render Activity テストの共通部品（ADR-0019）。

- 現行の台本・storyboard・シーン動画・音声・マニフェストを記録する（メディアは偽のバイト列）

計画・検査は本物の domain/render、エンジンと probe は tests/support/fake_render_engine。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.artifacts import (
    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
    build_scene_video_artifact,
    build_scene_voice_artifact,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.episode.transitions import EpisodeEvent
from domain.production.manifest import ArtifactRef, build_manifest
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.storage.artifact_store import ArtifactStore
from tests.support.production import GENERATOR
from tests.support.voice import BUCKET, STORYBOARD_LAYOUT, record_inputs

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


__all__ = [
    "FAKE_IMAGE_SHA",
    "STORYBOARD_TOTAL_MS",
    "TO_ASSETS_READY",
    "VOICE_DURATION_MS",
    "RenderSeed",
    "record_artifact",
    "record_manifest",
    "record_scene_video",
    "record_scene_voice",
    "seed_render_inputs",
]
