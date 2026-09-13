"""storyboard テストの共通の下ごしらえ（台本 Artifact を持つ ``script_ready`` の Episode）。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.artifacts import SCRIPT_ARTIFACT_SCHEMA_VERSION, build_script_artifact
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.storage.artifact_store import ArtifactStore

BUCKET = "artifacts"
PROMPT_ID = "storyboard_test"
PROMPT_VERSION = "1"

#: s1=8000 / s2=9000 / s3=8000（総尺 25000 ms）
SCRIPT_SCENES = [
    {"id": "s1", "narration": "縄文土器には焦げ跡が残る。", "visual": "土器", "duration_ms": 8000},
    {"id": "s2", "narration": "煮炊きに使われた証拠だ。", "visual": "炉", "duration_ms": 9000},
    {"id": "s3", "narration": "食が定住を支えた。", "visual": "集落", "duration_ms": 8000},
]


def good_storyboard(*, description: str = "土器のクローズアップ") -> str:
    """``FakeStoryboardGenerator`` の既定解釈が読める、台本を過不足なく覆う出力。"""
    return json.dumps(
        {
            "scenes": [
                {
                    "script_scene_id": "s1",
                    "start_ms": 0,
                    "duration_ms": 8000,
                    "visual_kind": "broll",
                    "visual_description": description,
                },
                {
                    "script_scene_id": "s2",
                    "start_ms": 8000,
                    "duration_ms": 9000,
                    "visual_kind": "animation",
                    "visual_description": "炉で煮炊きする再現アニメーション",
                },
                {
                    "script_scene_id": "s3",
                    "start_ms": 17000,
                    "duration_ms": 8000,
                    "visual_kind": "diagram",
                    "visual_description": "集落の俯瞰図",
                },
            ]
        },
        ensure_ascii=False,
    )


#: 台本シーン s3 を覆わない（カバレッジ違反 → retryable）。
UNCOVERED_STORYBOARD = json.dumps(
    {
        "scenes": [
            {
                "script_scene_id": "s1",
                "start_ms": 0,
                "duration_ms": 12500,
                "visual_kind": "broll",
                "visual_description": "a",
            },
            {
                "script_scene_id": "s2",
                "start_ms": 12500,
                "duration_ms": 12500,
                "visual_kind": "broll",
                "visual_description": "b",
            },
        ]
    }
)


async def record_script(
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    episode_id: str,
    *,
    title: str = "縄文の火",
) -> ArtifactMetadata:
    """台本 Artifact を保存して現行世代として記録する（台本 worker と同じ形）。"""
    payload: dict[str, Any] = build_script_artifact(
        episode_id=episode_id,
        language="ja",
        title=title,
        hook="この土器、なぜ焦げているのか",
        scenes=SCRIPT_SCENES,
        metadata={"topic": "縄文土器", "generator": "fake", "generator_model": "fake-model"},
    )
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        artifact_object_key(episode_id, ArtifactType.SCRIPT.value, digest), payload
    )
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.SCRIPT,
            schema_version=SCRIPT_ARTIFACT_SCHEMA_VERSION,
            bucket=BUCKET,
            object_key=put.key,
            sha256=put.sha256,
            size_bytes=put.size,
            input_hash=sha256_hex(f"script-input:{digest}".encode()),
        )
        await session.commit()
    return meta


async def create_episode_at_script_ready(
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore | None,
    *,
    with_script: bool = True,
) -> str:
    """``planned → in_progress → script_ready`` まで遷移表どおりに進めた Episode。"""
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="縄文土器")
        await episodes.apply_event(episode.id, EpisodeEvent.WORKFLOW_STARTED)
        await episodes.apply_event(episode.id, EpisodeEvent.SCRIPT_READY)
        await session.commit()
    if with_script:
        assert store is not None
        await record_script(session_factory, store, episode.id)
    return episode.id


async def artifact_rows(
    session_factory: async_sessionmaker[AsyncSession], episode_id: str, artifact_type: ArtifactType
) -> list[ArtifactMetadataRow]:
    """世代管理の列（version / superseded_at / input_hash）はドメイン表現に無いので行で読む。"""
    async with session_factory() as session:
        stmt = (
            select(ArtifactMetadataRow)
            .where(
                ArtifactMetadataRow.episode_id == uuid.UUID(episode_id),
                ArtifactMetadataRow.artifact_type == artifact_type.value,
            )
            .order_by(ArtifactMetadataRow.version)
        )
        return list((await session.scalars(stmt)).all())
