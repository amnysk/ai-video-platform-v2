"""ArtifactStore の契約（INV-9 / INV-11 / INV-17）。

MinIO実装とインメモリ実装の両方が同じ契約を満たすことを、同じテスト本体で確認する。
実MinIOに対する実行は tests/integration/test_minio_store.py（-m integration）。
"""

from __future__ import annotations

import hashlib

import pytest

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from infrastructure.storage.artifact_store import ArtifactConflictError
from infrastructure.storage.memory_store import InMemoryArtifactStore

EPISODE_ID = "0192f0c0-0000-7000-8000-000000000001"
PAYLOAD = {
    "episode_id": EPISODE_ID,
    "type": "dummy",
    "schema_version": "1.0",
    "message": "workflow completed",
}


@pytest.fixture
def store() -> InMemoryArtifactStore:
    return InMemoryArtifactStore()


async def test_put_then_get_roundtrip(store: InMemoryArtifactStore) -> None:
    key = artifact_object_key(EPISODE_ID, "dummy", sha256_hex(canonical_json_bytes(PAYLOAD)))
    result = await store.put_json(key, PAYLOAD)
    assert result.key == key
    assert await store.get_json(key) == PAYLOAD


async def test_exists_reflects_stored_objects(store: InMemoryArtifactStore) -> None:
    key = artifact_object_key(EPISODE_ID, "dummy", "0" * 64)
    assert await store.exists(key) is False
    await store.put_json(key, PAYLOAD)
    assert await store.exists(key) is True


async def test_sha256_matches_independently_computed_digest(store: InMemoryArtifactStore) -> None:
    body = canonical_json_bytes(PAYLOAD)
    expected = hashlib.sha256(body).hexdigest()
    result = await store.put_json(artifact_object_key(EPISODE_ID, "dummy", expected), PAYLOAD)
    assert result.sha256 == expected
    assert result.size == len(body)


def test_canonical_json_is_key_order_independent() -> None:
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert canonical_json_bytes(a) == canonical_json_bytes(b)
    assert sha256_hex(canonical_json_bytes(a)) == sha256_hex(canonical_json_bytes(b))


async def test_reput_of_identical_content_is_safe_and_does_not_rewrite(
    store: InMemoryArtifactStore,
) -> None:
    """INV-17: 同じ Artifact key への再実行が安全（Activityの再実行）。"""
    key = artifact_object_key(EPISODE_ID, "dummy", sha256_hex(canonical_json_bytes(PAYLOAD)))
    first = await store.put_json(key, PAYLOAD)
    second = await store.put_json(key, PAYLOAD)

    assert first.existed is False
    assert second.existed is True
    assert second.sha256 == first.sha256
    assert store.write_count(key) == 1, "immutableなオブジェクトを書き直してはならない (INV-11)"
    assert await store.get_json(key) == PAYLOAD


async def test_reput_with_different_content_raises_instead_of_overwriting(
    store: InMemoryArtifactStore,
) -> None:
    """INV-11: 一度書いたオブジェクトを上書きしない。"""
    key = artifact_object_key(EPISODE_ID, "dummy", "0" * 64)
    await store.put_json(key, PAYLOAD)
    with pytest.raises(ArtifactConflictError):
        await store.put_json(key, {**PAYLOAD, "message": "different"})


def test_object_key_is_content_addressed_and_deterministic() -> None:
    digest = sha256_hex(canonical_json_bytes(PAYLOAD))
    key = artifact_object_key(EPISODE_ID, "dummy", digest)
    assert key == artifact_object_key(EPISODE_ID, "dummy", digest)
    assert EPISODE_ID in key
    assert digest in key
