"""生出力（provider raw）の保存（ADR-0013）。

生出力は**Artifactではない**。スキーマ検証を通らないので INV-10
「スキーマ無しの成果物を作らない」と衝突させないため、
`artifact_metadata` には載せず、予約台帳の `raw_output_key` からのみ参照する。

これは「呼んだ証拠」であり、これが在ることで crash 後に再送せず照合できる。
"""

from __future__ import annotations

import pytest

from infrastructure.storage.artifact_store import ArtifactConflictError
from infrastructure.storage.memory_store import InMemoryArtifactStore

RAW = '```json\n{"title": "x"}\n```'


async def test_put_and_get_text_roundtrip() -> None:
    store = InMemoryArtifactStore()
    result = await store.put_text("provider-raw/ep-1/r1.txt", RAW)
    assert result.key == "provider-raw/ep-1/r1.txt"
    assert await store.get_text("provider-raw/ep-1/r1.txt") == RAW


async def test_text_objects_are_immutable_like_artifacts() -> None:
    """INV-11 は生出力にも適用する。証拠を書き換えられては意味がない。"""
    store = InMemoryArtifactStore()
    key = "provider-raw/ep-1/r1.txt"
    first = await store.put_text(key, RAW)
    second = await store.put_text(key, RAW)

    assert first.existed is False
    assert second.existed is True
    assert store.write_count(key) == 1

    with pytest.raises(ArtifactConflictError):
        await store.put_text(key, "different evidence")


async def test_exists_covers_text_objects() -> None:
    store = InMemoryArtifactStore()
    assert await store.exists("provider-raw/ep-1/r1.txt") is False
    await store.put_text("provider-raw/ep-1/r1.txt", RAW)
    assert await store.exists("provider-raw/ep-1/r1.txt") is True


async def test_get_text_of_missing_key_raises() -> None:
    store = InMemoryArtifactStore()
    with pytest.raises(KeyError):
        await store.get_text("provider-raw/ep-1/missing.txt")
