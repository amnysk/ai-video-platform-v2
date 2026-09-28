"""シーンの現行の代替映像案（``SCENE_VISUAL_OVERRIDE``）を読み、実効シーンを作る（ADR-0035）。

画像・動画の Activity は互いを import しない（INV-3）ので、読み込みと検証をここに1つ置く。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.artifacts import (
    SceneVisualOverrideArtifact,
    StoryboardScene,
    parse_scene_visual_override_artifact,
)
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import ProductionInputInvalidError, TransientError
from domain.production.effective_scene import apply_override
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.production.activity_errors import translate_error
from infrastructure.storage.artifact_store import ArtifactStore

__all__ = ["load_current_override", "load_effective_scene"]


async def load_current_override(
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    *,
    episode_id: str,
    scene_id: str,
    storyboard_meta: ArtifactMetadata,
) -> SceneVisualOverrideArtifact | None:
    """現行の代替映像案。無ければ ``None``。

    別の storyboard 世代に対する案は適用しない（``ProductionInputInvalidError``。黙って無視すると
    拒否された元の映像で作り直してしまう）。
    """
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE, scene_id
        )
    if meta is None:
        return None
    try:
        payload = await store.get_json(meta.object_key)
    except Exception as exc:
        if isinstance(translate_error(exc), TransientError):
            raise
        raise ProductionInputInvalidError(
            f"scene override {meta.object_key} is not readable: {type(exc).__name__}"
        ) from exc
    if sha256_hex(canonical_json_bytes(payload)) != meta.sha256:
        raise ProductionInputInvalidError(f"scene override sha256 mismatch at {meta.object_key}")
    try:
        override = parse_scene_visual_override_artifact(payload)
    except Exception as exc:
        raise ProductionInputInvalidError(f"scene override invalid: {str(exc)[:500]}") from exc
    if override.episode_id != episode_id or override.scene_id != scene_id:
        raise ProductionInputInvalidError(
            f"scene override {meta.id} is not for episode {episode_id} scene {scene_id}"
        )
    if override.source_storyboard.sha256 != storyboard_meta.sha256:
        raise ProductionInputInvalidError(
            f"scene override {meta.id} was planned for a different storyboard"
        )
    return override


async def load_effective_scene(
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    *,
    episode_id: str,
    scene: StoryboardScene,
    storyboard_meta: ArtifactMetadata,
) -> tuple[StoryboardScene, SceneVisualOverrideArtifact | None]:
    override = await load_current_override(
        session_factory,
        store,
        episode_id=episode_id,
        scene_id=scene.scene_id,
        storyboard_meta=storyboard_meta,
    )
    return apply_override(scene, override), override
