"""実 PostgreSQL + 実 MinIO で scene image の submit→await→Artifact を通す（ADR-0017 Phase 4A）。

生成器は fake（INV-18）。一時スキーマに閉じ込め、最後にスキーマごと消す。
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.artifacts import parse_scene_image_artifact
from contracts.production_activities import ImageAwaitRequest, ImageSubmitRequest
from contracts.states import ArtifactType, JobStatus, JobType, ReservationStatus
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.storage.artifact_store import readback_sha256
from infrastructure.workdir import WorkDirectory
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.production import FakeImageGenerator, sample_storyboard
from workers.production_image.activities import ImageProductionActivities

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="TEST_DATABASE_URL (*_test) and MINIO_ENDPOINT must be set",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"img_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    store = MinioArtifactStore.from_settings(Settings())
    await store.ensure_bucket()
    return store


async def _seed(factory, store) -> tuple[str, str]:
    async with factory() as session:
        episode = await EpisodeRepository(session).create(topic="pg image")
        await session.commit()
    payload = sample_storyboard(episode.id).model_dump(mode="json")
    digest = sha256_hex(canonical_json_bytes(payload))
    key = artifact_object_key(episode.id, "storyboard", digest)
    await store.put_json(key, payload)
    async with factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode.id,
            artifact_type=ArtifactType.STORYBOARD,
            schema_version="1.0",
            bucket=store.bucket,
            object_key=key,
            sha256=digest,
        )
        await session.commit()
    return episode.id, meta.id


def _activities(factory, store, generator, tmp_path) -> ImageProductionActivities:
    return ImageProductionActivities(
        session_factory=factory,
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        runner=PaidJobRunner(
            session_factory=factory,
            store=store,
            workdir=WorkDirectory(tmp_path / "work", forbidden=()),
        ),
        bucket=store.bucket,
        poll_interval_seconds=0,
    )


async def test_scene_image_end_to_end_on_postgres_and_minio(
    pg_session_factory, minio_store, tmp_path
) -> None:
    f = pg_session_factory
    episode_id, sb_id = await _seed(f, minio_store)
    gen = FakeImageGenerator(pending_polls=1, cost_usd=0.04)
    acts = _activities(f, minio_store, gen, tmp_path)

    results = {}
    for scene in ("sb1", "sb2"):
        submitted = await acts.submit(
            ImageSubmitRequest(episode_id, "wf", "run", scene, sb_id, round=1)
        )
        assert submitted.artifact is None
        results[scene] = (
            submitted.reservation_id,
            await acts.await_image(
                ImageAwaitRequest(episode_id, "wf", "run", scene, sb_id, submitted.reservation_id)
            ),
        )
    assert gen.submit_calls == 2

    for scene, (reservation_id, result) in results.items():
        artifact = parse_scene_image_artifact(await minio_store.get_json(result.object_key))
        assert artifact.scene_id == scene and (artifact.width, artifact.height) == (1080, 1920)
        assert (
            await readback_sha256(minio_store, artifact.media.object_key) == artifact.media.sha256
        )
        async with f() as session:
            row = await ProviderReservationRepository(session).get(reservation_id)
        assert row is not None
        assert row.status is ReservationStatus.SPENT and row.reconciled_by == "evidence"
        assert row.provider_job_ref and row.estimated_cost_usd == Decimal("0.0400")
        assert row.scene_id == scene and row.outcome_artifact_id == result.artifact_id
        assert row.raw_output_key and await minio_store.exists(row.raw_output_key)

    async with f() as session:
        current = await ArtifactMetadataRepository(session).list_current_by_type(
            episode_id, ArtifactType.SCENE_IMAGE
        )
    assert sorted((m.scene_id, m.id) for m in current) == sorted(  # type: ignore[type-var]
        (s, r.artifact_id) for s, (_, r) in results.items()
    )

    # 2回目: 新しい submit なし、job は skipped
    again = await acts.submit(ImageSubmitRequest(episode_id, "wf", "run", "sb1", sb_id, round=1))
    assert again.artifact is not None and again.artifact.reused
    assert again.artifact.artifact_id == results["sb1"][1].artifact_id
    assert gen.submit_calls == 2
    async with f() as session:
        jobs = [
            (j.scene_id, j.status)
            for j in await JobRepository(session).list_for_episode(episode_id)
            if j.type is JobType.PRODUCE_SCENE_IMAGE
        ]
    assert sorted(jobs) == [
        ("sb1", JobStatus.SKIPPED),
        ("sb1", JobStatus.SUCCEEDED),
        ("sb2", JobStatus.SUCCEEDED),
    ]
