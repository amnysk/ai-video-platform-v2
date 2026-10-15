"""Episode 単位の agentic 成果物（UserBrief / StrategyBrief / WorkflowPlan 等）の読み書き。

内容は ``artifact_metadata`` + ArtifactStore（INV-9/11/17）。読むときは ``parse_artifact`` を通し、
未知の schema 版は黙って読み替えず拒否する（INV-54）。commit は呼び出し側。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.artifact_registry import parse_artifact
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.storage.artifact_store import ArtifactStore


async def read_current_artifact(
    session: AsyncSession,
    store: ArtifactStore,
    episode_id: uuid.UUID | str,
    artifact_type: ArtifactType,
) -> tuple[str, BaseModel] | None:
    """現行の ``(revision_id, model)``。無ければ ``None``。"""
    meta = await ArtifactMetadataRepository(session).find_current_by_type(episode_id, artifact_type)
    if meta is None:
        return None
    return meta.id, parse_artifact(artifact_type, await store.get_json(meta.object_key))


async def write_episode_artifact(
    session: AsyncSession,
    store: ArtifactStore,
    *,
    bucket: str,
    episode_id: uuid.UUID | str,
    model: BaseModel,
) -> str:
    """モデルを content-addressed に保存して現行にする（同じ内容は既存行）。revision id を返す。"""
    document = model.model_dump(mode="json")
    artifact_type = ArtifactType(document["type"])
    digest = sha256_hex(canonical_json_bytes(document))
    key = artifact_object_key(str(episode_id), artifact_type.value, digest)
    put = await store.put_json(key, document)
    meta = await ArtifactMetadataRepository(session).record(
        episode_id=episode_id,
        artifact_type=artifact_type,
        schema_version=str(document["schema_version"]),
        bucket=bucket,
        object_key=key,
        sha256=put.sha256,
        size_bytes=put.size,
    )
    return meta.id
