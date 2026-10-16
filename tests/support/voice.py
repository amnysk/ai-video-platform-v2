"""Production Voice テストの共通部品（Phase 4B）。現行の台本 + storyboard を記録する。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.artifacts import (
    SCRIPT_ARTIFACT_SCHEMA_VERSION,
    STORYBOARD_ARTIFACT_SCHEMA_VERSION,
    build_script_artifact,
    build_storyboard_artifact,
)
from contracts.production_activities import VoiceGenerateRequest
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.storage.artifact_store import ArtifactStore
from tests.support.production import SCRIPT_SCENES

BUCKET = "artifacts"

#: sb1=s1, sb2/sb3=s2, sb4=s3（tests.support.production.sample_storyboard と同じ形）
STORYBOARD_LAYOUT = [
    ("s1", 0, 8000, "broll", "土器のクローズアップ"),
    ("s2", 8000, 4000, "animation", "炉に火を入れる"),
    ("s2", 12000, 5000, "animation", "煮炊きする再現"),
    ("s3", 17000, 8000, "diagram", "集落の俯瞰図"),
]


async def _record(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    episode_id: str,
    artifact_type: ArtifactType,
    schema_version: str,
    payload: dict[str, Any],
) -> ArtifactMetadata:
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode_id, artifact_type.value, digest)
    put = await store.put_json(key, payload)
    async with factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            bucket=BUCKET,
            object_key=put.key,
            sha256=put.sha256,
            size_bytes=put.size,
            input_hash=sha256_hex(f"input:{digest}".encode()),
        )
        await session.commit()
    return meta


async def create_episode(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        episode = await EpisodeRepository(session).create(topic="voice")
        await session.commit()
        return episode.id


async def record_inputs(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    episode_id: str,
    *,
    language: str = "ja",
    narrations: dict[str, str] | None = None,
) -> tuple[ArtifactMetadata, ArtifactMetadata]:
    """台本と、それを参照する storyboard を現行世代として記録する。(script, storyboard)"""
    scenes = [
        {**scene, "narration": (narrations or {}).get(scene["id"], scene["narration"])}
        for scene in SCRIPT_SCENES
    ]
    script = await _record(
        factory,
        store,
        episode_id,
        ArtifactType.SCRIPT,
        SCRIPT_ARTIFACT_SCHEMA_VERSION,
        build_script_artifact(
            episode_id=episode_id,
            language=language,
            title="縄文の食",
            hook="土器が語る",
            scenes=scenes,
            metadata={"topic": "縄文", "generator": "fake", "generator_model": "fake"},
        ),
    )
    storyboard = await _record(
        factory,
        store,
        episode_id,
        ArtifactType.STORYBOARD,
        STORYBOARD_ARTIFACT_SCHEMA_VERSION,
        build_storyboard_artifact(
            episode_id=episode_id,
            source_script={
                "artifact_id": script.id,
                "sha256": script.sha256,
                "schema_version": script.schema_version,
            },
            scenes=[
                {
                    "scene_id": f"sb{i}",
                    "order": i,
                    "script_scene_id": sid,
                    "start_ms": start,
                    "duration_ms": duration,
                    "visual_kind": kind,
                    "visual_description": desc,
                }
                for i, (sid, start, duration, kind, desc) in enumerate(STORYBOARD_LAYOUT, start=1)
            ],
            total_duration_ms=25000,
            metadata={
                "generator": "fake",
                "generator_model": "fake",
                "generation_spec_id": "spec-1",
            },
        ),
    )
    return script, storyboard


def voice_request(
    episode_id: str,
    script: ArtifactMetadata,
    storyboard: ArtifactMetadata,
    script_scene_id: str = "s2",
) -> VoiceGenerateRequest:
    layout = enumerate(STORYBOARD_LAYOUT, start=1)
    ids = [f"sb{i}" for i, row in layout if row[0] == script_scene_id]
    return VoiceGenerateRequest(
        episode_id=episode_id,
        workflow_id="wf-voice",
        run_id="run-1",
        script_scene_id=script_scene_id,
        storyboard_scene_ids=ids,
        storyboard_artifact_id=storyboard.id,
        script_artifact_id=script.id,
    )
