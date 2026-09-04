"""実MinIOに対する ArtifactStore 検査（INV-9 / INV-11）。docker compose が必要。"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from infrastructure.storage.artifact_store import ArtifactConflictError
from infrastructure.storage.minio_store import MinioArtifactStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("MINIO_ENDPOINT"),
    reason="MINIO_ENDPOINT must be set (docker compose --profile core up -d)",
)


@pytest_asyncio.fixture
async def store():
    from infrastructure.config import Settings

    settings = Settings()
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    return store


async def test_put_get_exists_roundtrip_against_real_minio(store) -> None:
    episode_id = str(uuid.uuid4())
    payload = {
        "episode_id": episode_id,
        "type": "dummy",
        "schema_version": "1.0",
        "message": "workflow completed",
    }
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode_id, "dummy", digest)

    assert await store.exists(key) is False
    result = await store.put_json(key, payload)
    assert result.sha256 == digest
    assert result.existed is False
    assert await store.exists(key) is True
    assert await store.get_json(key) == payload


async def test_reput_is_idempotent_and_conflict_is_detected(store) -> None:
    episode_id = str(uuid.uuid4())
    payload = {"episode_id": episode_id, "type": "dummy", "schema_version": "1.0", "message": "m"}
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode_id, "dummy", digest)

    await store.put_json(key, payload)
    again = await store.put_json(key, payload)
    assert again.existed is True
    assert again.sha256 == digest

    with pytest.raises(ArtifactConflictError):
        await store.put_json(key, payload | {"message": "different"})
