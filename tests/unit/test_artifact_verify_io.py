"""infrastructure/artifact/verify.py の I/O 検証オーケストレーター（ADR-0033）。

InMemoryArtifactStore（既存の fake、新しい fake は作らない）を使い、
欠落・sha256不一致・size不一致・schema不正・profile不一致それぞれで正しい verdict になること、
streaming プリミティブ（``sha256_of``）をそのまま使うことを検査する。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.verification import ArtifactVerdict
from infrastructure.artifact.verify import verify_artifact
from infrastructure.storage.memory_store import InMemoryArtifactStore

EPISODE_ID = str(uuid.uuid4())
SOURCE_ID = str(uuid.uuid4())
DESCRIPTOR_KEY = "artifacts/scene_image/sb1.json"
MEDIA_KEY = "artifacts/scene_image/sb1.png"
MEDIA_BODY = b"png1"
#: 「今まさに構成されている generator」の profile id を模す（fal の固定定数は使わない。ADR-0033）。
CURRENT_PROFILE_ID = "fake-image-profile-v1"


def _source_ref() -> dict:
    return {"artifact_id": SOURCE_ID, "sha256": "a" * 64, "schema_version": "1.0"}


def _payload(
    *, generation_profile_id: str = CURRENT_PROFILE_ID, media_body: bytes = MEDIA_BODY
) -> dict:
    return {
        "episode_id": EPISODE_ID,
        "type": "scene_image",
        "schema_version": "1.0",
        "source_storyboard": _source_ref(),
        "scene_id": "sb1",
        "media": {
            "object_key": MEDIA_KEY,
            "sha256": hashlib.sha256(media_body).hexdigest(),
            "bytes": len(media_body),
            "mime": "image/png",
        },
        "width": 1080,
        "height": 1920,
        "generator": {
            "generator": "fal",
            "generator_model": "seedream-4.5",
            "generation_profile_id": generation_profile_id,
        },
    }


async def _seed(
    store: InMemoryArtifactStore, *, payload: dict, media_body: bytes = MEDIA_BODY
) -> ArtifactMetadata:
    put = await store.put_json(DESCRIPTOR_KEY, payload)
    await store.put_bytes(MEDIA_KEY, media_body, "image/png")
    return ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id=EPISODE_ID,
        artifact_type=ArtifactType.SCENE_IMAGE,
        schema_version="1.0",
        bucket="b",
        object_key=DESCRIPTOR_KEY,
        sha256=put.sha256,
        created_at=datetime.now(UTC),
        scene_id="sb1",
        size_bytes=put.size,
    )


@pytest.fixture
def store() -> InMemoryArtifactStore:
    return InMemoryArtifactStore()


async def test_intact_artifact_is_reusable(store: InMemoryArtifactStore) -> None:
    artifact = await _seed(store, payload=_payload())
    result = await verify_artifact(
        store, artifact, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.REUSABLE


async def test_missing_descriptor_is_missing(store: InMemoryArtifactStore) -> None:
    artifact = await _seed(store, payload=_payload())
    # 記述子オブジェクト自体を無かったことにする（DB行は残る想定）
    fake_missing = ArtifactMetadata(
        id=artifact.id,
        episode_id=artifact.episode_id,
        artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version,
        bucket=artifact.bucket,
        object_key="artifacts/scene_image/does-not-exist.json",
        sha256=artifact.sha256,
        created_at=artifact.created_at,
        scene_id=artifact.scene_id,
        size_bytes=artifact.size_bytes,
    )
    result = await verify_artifact(
        store, fake_missing, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.MISSING


async def test_missing_media_is_missing(store: InMemoryArtifactStore) -> None:
    put = await store.put_json(DESCRIPTOR_KEY, _payload())
    # media は書かない
    artifact = ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id=EPISODE_ID,
        artifact_type=ArtifactType.SCENE_IMAGE,
        schema_version="1.0",
        bucket="b",
        object_key=DESCRIPTOR_KEY,
        sha256=put.sha256,
        created_at=datetime.now(UTC),
        scene_id="sb1",
        size_bytes=put.size,
    )
    result = await verify_artifact(
        store, artifact, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.MISSING


async def test_descriptor_sha256_mismatch_is_corrupt_hash(store: InMemoryArtifactStore) -> None:
    artifact = await _seed(store, payload=_payload())
    tampered = ArtifactMetadata(
        id=artifact.id,
        episode_id=artifact.episode_id,
        artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version,
        bucket=artifact.bucket,
        object_key=artifact.object_key,
        sha256="0" * 64,  # DB記録と実体が食い違う
        created_at=artifact.created_at,
        scene_id=artifact.scene_id,
        size_bytes=artifact.size_bytes,
    )
    result = await verify_artifact(
        store, tampered, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.CORRUPT_HASH


async def test_descriptor_size_mismatch_is_corrupt_hash(store: InMemoryArtifactStore) -> None:
    artifact = await _seed(store, payload=_payload())
    tampered = ArtifactMetadata(
        id=artifact.id,
        episode_id=artifact.episode_id,
        artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version,
        bucket=artifact.bucket,
        object_key=artifact.object_key,
        sha256=artifact.sha256,
        created_at=artifact.created_at,
        scene_id=artifact.scene_id,
        size_bytes=(artifact.size_bytes or 0) + 999,  # size だけ食い違う
    )
    result = await verify_artifact(
        store, tampered, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.CORRUPT_HASH


async def test_unrecorded_size_skips_size_check_and_verifies_by_hash_only(
    store: InMemoryArtifactStore,
) -> None:
    artifact = await _seed(store, payload=_payload())
    legacy = ArtifactMetadata(
        id=artifact.id,
        episode_id=artifact.episode_id,
        artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version,
        bucket=artifact.bucket,
        object_key=artifact.object_key,
        sha256=artifact.sha256,
        created_at=artifact.created_at,
        scene_id=artifact.scene_id,
        size_bytes=None,  # 本ADR以前の行を模す
    )
    result = await verify_artifact(store, legacy, current_generation_profile_id=CURRENT_PROFILE_ID)
    assert result.verdict is ArtifactVerdict.REUSABLE


async def test_media_sha256_mismatch_is_corrupt_hash(store: InMemoryArtifactStore) -> None:
    payload = _payload()
    put = await store.put_json(DESCRIPTOR_KEY, payload)
    await store.put_bytes(MEDIA_KEY, b"tampered-bytes-not-matching-payload-sha", "image/png")
    artifact = ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id=EPISODE_ID,
        artifact_type=ArtifactType.SCENE_IMAGE,
        schema_version="1.0",
        bucket="b",
        object_key=DESCRIPTOR_KEY,
        sha256=put.sha256,
        created_at=datetime.now(UTC),
        scene_id="sb1",
        size_bytes=put.size,
    )
    result = await verify_artifact(
        store, artifact, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.CORRUPT_HASH


async def test_schema_invalid_payload_is_corrupt_schema(store: InMemoryArtifactStore) -> None:
    bad_payload = {"episode_id": EPISODE_ID, "type": "scene_image", "schema_version": "1.0"}
    put = await store.put_json(DESCRIPTOR_KEY, bad_payload)
    artifact = ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id=EPISODE_ID,
        artifact_type=ArtifactType.SCENE_IMAGE,
        schema_version="1.0",
        bucket="b",
        object_key=DESCRIPTOR_KEY,
        sha256=put.sha256,
        created_at=datetime.now(UTC),
        scene_id="sb1",
        size_bytes=put.size,
    )
    result = await verify_artifact(
        store, artifact, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.CORRUPT_SCHEMA


async def test_stale_generation_profile_id_is_version_mismatch(
    store: InMemoryArtifactStore,
) -> None:
    artifact = await _seed(store, payload=_payload(generation_profile_id="fal-seedream-3.0:old"))
    result = await verify_artifact(
        store, artifact, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.VERSION_MISMATCH


async def test_omitted_current_profile_id_skips_the_profile_check(
    store: InMemoryArtifactStore,
) -> None:
    """呼び出し元が現在値を渡さなければ、この型のチェックは行わない（推測しない。ADR-0033）。"""
    artifact = await _seed(store, payload=_payload(generation_profile_id="anything-at-all"))
    result = await verify_artifact(store, artifact)  # current_generation_profile_id を省略
    assert result.verdict is ArtifactVerdict.REUSABLE


async def test_dummy_artifact_has_no_media_and_no_profile_check(
    store: InMemoryArtifactStore,
) -> None:
    payload = {
        "episode_id": EPISODE_ID,
        "type": "dummy",
        "schema_version": "1.0",
        "message": "workflow completed",
    }
    put = await store.put_json("artifacts/dummy.json", payload)
    artifact = ArtifactMetadata(
        id=str(uuid.uuid4()),
        episode_id=EPISODE_ID,
        artifact_type=ArtifactType.DUMMY,
        schema_version="1.0",
        bucket="b",
        object_key="artifacts/dummy.json",
        sha256=put.sha256,
        created_at=datetime.now(UTC),
        size_bytes=put.size,
    )
    result = await verify_artifact(
        store, artifact, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.REUSABLE


async def test_verification_never_writes_or_deletes_anything(store: InMemoryArtifactStore) -> None:
    artifact = await _seed(store, payload=_payload(generation_profile_id="stale"))
    before_descriptor_writes = store.write_count(DESCRIPTOR_KEY)
    before_media_writes = store.write_count(MEDIA_KEY)
    before_descriptor = await store.get_bytes(DESCRIPTOR_KEY)
    before_media = await store.get_bytes(MEDIA_KEY)

    result = await verify_artifact(
        store, artifact, current_generation_profile_id=CURRENT_PROFILE_ID
    )
    assert result.verdict is ArtifactVerdict.VERSION_MISMATCH  # 破損検出でも

    assert store.write_count(DESCRIPTOR_KEY) == before_descriptor_writes
    assert store.write_count(MEDIA_KEY) == before_media_writes
    assert await store.get_bytes(DESCRIPTOR_KEY) == before_descriptor
    assert await store.get_bytes(MEDIA_KEY) == before_media
