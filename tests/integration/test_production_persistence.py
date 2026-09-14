"""実PostgreSQL + 実MinIO で production の永続化を検査する（ADR-0017 / ADR-0018）。

**共有 DB を壊さない**: 一時スキーマに閉じ込め、最後にスキーマごと消す。
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.states import ArtifactType, JobType, ProviderCall
from domain.artifact.hashing import sha256_hex
from domain.artifact.keys import media_object_key
from domain.errors import ArtifactConflictError, InvalidTransitionError
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.storage.artifact_store import readback_sha256
from tests.support.production import make_mp4, make_png

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or "postgresql" not in DATABASE_URL,
    reason="DATABASE_URL must point at PostgreSQL (docker compose core)",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"prod_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def _episode(factory) -> str:
    async with factory() as session:
        episode = await EpisodeRepository(session).create(topic="pg production")
        await session.commit()
        return episode.id


async def _record(factory, episode_id, sha, *, scene_id=None, type=ArtifactType.SCENE_IMAGE):
    async with factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=type,
            schema_version="1.0",
            bucket="artifacts",
            object_key=f"k/{scene_id}/{sha}",
            sha256=sha,
            input_hash=sha,
            scene_id=scene_id,
        )
        await session.commit()
        return meta


async def test_scene_scoped_versioning_on_postgres(pg_session_factory) -> None:
    f = pg_session_factory
    episode_id = await _episode(f)
    await _record(f, episode_id, "1" * 64, scene_id="sb1")
    b = await _record(f, episode_id, "2" * 64, scene_id="sb2")
    a2 = await _record(f, episode_id, "3" * 64, scene_id="sb1")
    reuse = await _record(f, episode_id, "3" * 64, scene_id="sb1")
    script = await _record(f, episode_id, "4" * 64, type=ArtifactType.SCRIPT)
    script2 = await _record(f, episode_id, "5" * 64, type=ArtifactType.SCRIPT)
    assert reuse.id == a2.id and script.id != script2.id

    async with f() as session:
        repo = ArtifactMetadataRepository(session)
        assert (
            await repo.find_current_by_type(episode_id, ArtifactType.SCENE_IMAGE, "sb1")
        ).id == a2.id  # type: ignore[union-attr]
        assert (
            await repo.find_current_by_type(episode_id, ArtifactType.SCENE_IMAGE, "sb2")
        ).id == b.id  # type: ignore[union-attr]
        assert (await repo.find_current_by_type(episode_id, ArtifactType.SCRIPT)).id == script2.id  # type: ignore[union-attr]
        rows = (
            await session.execute(
                text(
                    "SELECT artifact_type, scene_id, version, superseded_at IS NULL "
                    "FROM artifact_metadata ORDER BY artifact_type, scene_id, version"
                )
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("scene_image", "sb1", 1, False),
        ("scene_image", "sb1", 2, True),
        ("scene_image", "sb2", 1, True),
        ("script", None, 1, False),
        ("script", None, 2, True),
    ]


async def test_postgres_enforces_one_current_per_scene_key(pg_session_factory) -> None:
    episode_id = await _episode(pg_session_factory)
    insert = text(
        "INSERT INTO artifact_metadata (id, episode_id, artifact_type, schema_version, bucket, "
        "object_key, sha256, input_hash, version, scene_id) VALUES "
        "(:id, :ep, 'scene_image', '1.0', 'b', 'k', :sha, :sha, :v, :scene)"
    )
    async with pg_session_factory() as session:
        await session.execute(
            insert, {"id": uuid.uuid4(), "ep": episode_id, "sha": "1" * 64, "v": 1, "scene": "sb1"}
        )
        await session.execute(
            insert, {"id": uuid.uuid4(), "ep": episode_id, "sha": "2" * 64, "v": 1, "scene": "sb2"}
        )
        await session.commit()
    async with pg_session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert,
                {"id": uuid.uuid4(), "ep": episode_id, "sha": "3" * 64, "v": 2, "scene": "sb1"},
            )
            await session.flush()


async def test_scene_jobs_and_reservations_on_postgres(pg_session_factory) -> None:
    f = pg_session_factory
    episode_id = await _episode(f)
    async with f() as session:
        jobs = JobRepository(session)
        job = await jobs.create(
            episode_id=episode_id, type=JobType.PRODUCE_SCENE_VIDEO, scene_id="sb3"
        )
        reservations = ProviderReservationRepository(session)
        row = await reservations.reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key="k" * 64,
            input_hash="h" * 64,
            round=1,
            job_id=job.id,
            scene_id="sb3",
            estimated_cost_usd=Decimal("0.4000"),
        )
        await session.commit()

    async with f() as session:
        jobs = JobRepository(session)
        found = await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_VIDEO, "sb3")
        assert found is not None and found.id == job.id
        reservations = ProviderReservationRepository(session)
        assert await reservations.find_unreconciled(episode_id, ProviderCall.FAL_VIDEO, "sb4") == []
        assert (
            len(await reservations.find_unreconciled(episode_id, ProviderCall.FAL_VIDEO, "sb3"))
            == 1
        )

        with pytest.raises(InvalidTransitionError):
            await reservations.record_provider_job_ref(row.id, "req-1")
        await reservations.mark_dispatched(row.id)
        await session.commit()
        await reservations.record_provider_job_ref(row.id, "req-1")
        await session.commit()

    async with f() as session:
        reservations = ProviderReservationRepository(session)
        await reservations.record_provider_job_ref(row.id, "req-1")  # 同じ参照は no-op
        with pytest.raises(InvalidTransitionError):
            await reservations.record_provider_job_ref(row.id, "req-2")
        loaded = await reservations.get(row.id)
    assert loaded is not None
    assert loaded.provider_job_ref == "req-1"
    assert loaded.estimated_cost_usd == Decimal("0.4000")


@pytest.mark.skipif(not os.environ.get("MINIO_ENDPOINT"), reason="MINIO_ENDPOINT must be set")
async def test_media_bytes_roundtrip_through_minio() -> None:
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    store = MinioArtifactStore.from_settings(Settings())
    await store.ensure_bucket()
    episode = str(uuid.uuid4())
    for data, mime, ext, probe in (
        (make_png(1080, 1920), "image/png", "png", "probe_image"),
        (make_mp4(1000), "video/mp4", "mp4", "probe_video"),
    ):
        digest = sha256_hex(data)
        key = media_object_key(episode, "scene_image", "sb1", digest, ext)
        put = await store.put_bytes(key, data, mime)
        assert put.sha256 == digest and not put.existed
        assert (await store.put_bytes(key, data, mime)).existed
        assert await readback_sha256(store, key) == digest
        stat = await store.stat(key)
        assert stat.size == len(data) and stat.content_type == mime and stat.etag
        getattr(PillowAvMediaProbe(), probe)(await store.get_bytes(key))
        with pytest.raises(ArtifactConflictError):
            await store.put_bytes(key, data + b"x", mime)
    with pytest.raises(KeyError):
        await store.stat(f"media/{episode}/missing")
    with pytest.raises(KeyError):
        await store.get_bytes(f"media/{episode}/missing")


async def test_provider_job_ref_is_write_once_across_concurrent_sessions(
    pg_session_factory, monkeypatch
) -> None:
    """検査と書き込みの間に別セッションが参照を書いても上書きできない（ADR-0017）。

    競合の窓を決定的に再現するため、先行セッションの読み取りを「参照なし」の古い
    スナップショットに固定する。write-once は DB の条件付き UPDATE が守る。
    """
    from sqlalchemy.orm.attributes import set_committed_value

    from infrastructure.db.models import ProviderReservationRow

    f = pg_session_factory
    episode_id = await _episode(f)
    async with f() as session:
        reservations = ProviderReservationRepository(session)
        row = await reservations.reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_IMAGE,
            idempotency_key="r" * 64,
            input_hash="h" * 64,
            round=1,
            scene_id="sb1",
        )
        await reservations.mark_dispatched(row.id)
        await session.commit()

    async with f() as first, f() as second:
        first_repo = ProviderReservationRepository(first)
        original_row = first_repo._row

        async def stale_row(reservation_id):
            loaded = await original_row(reservation_id)
            assert isinstance(loaded, ProviderReservationRow)
            set_committed_value(loaded, "provider_job_ref", None)
            return loaded

        await ProviderReservationRepository(second).record_provider_job_ref(row.id, "req-B")
        await second.commit()

        monkeypatch.setattr(first_repo, "_row", stale_row)
        with pytest.raises(InvalidTransitionError):
            await first_repo.record_provider_job_ref(row.id, "req-A")
        await first.rollback()
        # 同じ参照の再記録は古い読み取りからでも no-op
        again = await first_repo.record_provider_job_ref(row.id, "req-B")
        assert again.provider_job_ref == "req-B"
        await first.commit()

    async with f() as session:
        loaded = await ProviderReservationRepository(session).get(row.id)
    assert loaded is not None and loaded.provider_job_ref == "req-B"
