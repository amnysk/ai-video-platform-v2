"""Artifact の世代管理（ADR-0012）。

非決定的な生成器では sha256 が同一性の代わりにならないので、
``input_hash`` / ``version`` / ``superseded_at`` で「現行の1本」を決める。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from contracts.states import ArtifactType
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository


def _kwargs(episode_id: str, *, sha: str, input_hash: str) -> dict:
    return dict(
        episode_id=episode_id,
        artifact_type=ArtifactType.SCRIPT,
        schema_version="1.0",
        bucket="artifacts",
        object_key=f"artifacts/{episode_id}/script/{sha}.json",
        sha256=sha,
        input_hash=input_hash,
    )


async def _rows(session, episode_id: str) -> list[ArtifactMetadataRow]:
    stmt = (
        select(ArtifactMetadataRow)
        .where(ArtifactMetadataRow.episode_id == uuid.UUID(episode_id))
        .order_by(ArtifactMetadataRow.version)
    )
    return list((await session.scalars(stmt)).all())


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


async def test_recording_a_new_generation_supersedes_the_previous_current(session) -> None:
    episode_id = await _episode(session)
    artifacts = ArtifactMetadataRepository(session)

    await artifacts.record(**_kwargs(episode_id, sha="a" * 64, input_hash="h1"))
    await session.commit()
    await artifacts.record(**_kwargs(episode_id, sha="b" * 64, input_hash="h2"))
    await session.commit()

    rows = await _rows(session, episode_id)
    assert [r.version for r in rows] == [1, 2]
    assert rows[0].superseded_at is not None
    assert rows[1].superseded_at is None


async def test_only_one_current_artifact_per_episode_and_type(session) -> None:
    """partial unique index が「現行は常に1本」をDBで保証する。"""
    episode_id = await _episode(session)
    artifacts = ArtifactMetadataRepository(session)
    await artifacts.record(**_kwargs(episode_id, sha="a" * 64, input_hash="h1"))
    await session.commit()

    with pytest.raises(IntegrityError):
        session.add(
            ArtifactMetadataRow(
                id=uuid.uuid4(),
                episode_id=uuid.UUID(episode_id),
                artifact_type=ArtifactType.SCRIPT.value,
                schema_version="1.0",
                bucket="artifacts",
                object_key="artifacts/x/script/c.json",
                sha256="c" * 64,
                input_hash="h9",
                version=99,
                superseded_at=None,
            )
        )
        await session.commit()
    await session.rollback()


async def test_find_current_matches_by_input_hash(session) -> None:
    episode_id = await _episode(session)
    artifacts = ArtifactMetadataRepository(session)
    await artifacts.record(**_kwargs(episode_id, sha="a" * 64, input_hash="h1"))
    await session.commit()

    assert await artifacts.find_current(episode_id, ArtifactType.SCRIPT, "h1") is not None
    assert await artifacts.find_current(episode_id, ArtifactType.SCRIPT, "nope") is None

    # 世代が上がると、古い input_hash では現行として引けない（=再生成の条件）。
    await artifacts.record(**_kwargs(episode_id, sha="b" * 64, input_hash="h2"))
    await session.commit()
    assert await artifacts.find_current(episode_id, ArtifactType.SCRIPT, "h1") is None
    assert await artifacts.find_current(episode_id, ArtifactType.SCRIPT, "h2") is not None


async def test_same_input_hash_returns_the_existing_current_artifact(session) -> None:
    """skip 判定の本体（INV-17）。課金なしで同じ Artifact を返す。"""
    episode_id = await _episode(session)
    artifacts = ArtifactMetadataRepository(session)
    first = await artifacts.record(**_kwargs(episode_id, sha="a" * 64, input_hash="h1"))
    await session.commit()

    found = await artifacts.find_current(episode_id, ArtifactType.SCRIPT, "h1")
    assert found is not None
    assert found.id == first.id
    assert len(await _rows(session, episode_id)) == 1


async def test_version_increments_monotonically(session) -> None:
    episode_id = await _episode(session)
    artifacts = ArtifactMetadataRepository(session)
    for i in range(3):
        await artifacts.record(**_kwargs(episode_id, sha=str(i) * 64, input_hash=f"h{i}"))
        await session.commit()

    rows = await _rows(session, episode_id)
    assert [r.version for r in rows] == [1, 2, 3]
    assert [r.superseded_at is None for r in rows] == [False, False, True]


async def test_input_hash_defaults_to_sha256_for_deterministic_callers(session) -> None:
    """既存の dummy 呼び出し元は input_hash を渡さない（決定論的なので sha256 を流用）。"""
    episode_id = await _episode(session)
    artifacts = ArtifactMetadataRepository(session)
    meta = await artifacts.record(
        episode_id=episode_id,
        artifact_type=ArtifactType.DUMMY,
        schema_version="1.0",
        bucket="artifacts",
        object_key="artifacts/x/dummy/a.json",
        sha256="a" * 64,
    )
    await session.commit()
    assert meta is not None
    assert await artifacts.find_current(episode_id, ArtifactType.DUMMY, "a" * 64) is not None
