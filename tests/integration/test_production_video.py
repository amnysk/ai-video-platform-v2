"""実 PostgreSQL + 実 MinIO で scene video の submit→await→Artifact を通す（ADR-0017 Phase 4C）。

生成器は fake（実 mp4 を返す / INV-18）。一時スキーマに閉じ込め、最後にスキーマごと消す。
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.artifacts import parse_scene_video_artifact
from contracts.production_activities import VideoAwaitRequest, VideoSubmitRequest
from contracts.states import ArtifactType, JobStatus, JobType, ReservationStatus
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.storage.artifact_store import readback_sha256
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeVideoGenerator
from tests.unit.test_production_video_activities import DURATIONS, seed
from workers.production_video.activities import VideoProductionActivities

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or "postgresql" not in DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="DATABASE_URL (PostgreSQL) and MINIO_ENDPOINT must be set (docker compose core)",
)

SEEDANCE_RATE = 0.2419


class _PricedFake(FakeVideoGenerator):
    """見積もりを Seedance と同じ規則（秒 × 0.2419）にする。"""

    def estimate_cost_usd(self, request) -> float:
        return request.duration_ms / 1000 * SEEDANCE_RATE


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"vid_test_{uuid.uuid4().hex[:12]}"
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


@pytest_asyncio.fixture
async def minio_store():
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    store = MinioArtifactStore.from_settings(Settings())
    await store.ensure_bucket()
    return store


async def test_scene_video_end_to_end_on_postgres_and_minio(
    pg_session_factory, minio_store, tmp_path
) -> None:
    f = pg_session_factory
    ep, sb, images = await seed(f, minio_store, scenes=("sb2", "sb3"))
    gen = _PricedFake(pending_polls=1)
    acts = VideoProductionActivities(
        session_factory=f,
        store=minio_store,
        generator=gen,
        probe=PillowAvMediaProbe(),
        runner=PaidJobRunner(
            session_factory=f, store=minio_store, workdir=WorkDirectory(tmp_path, forbidden=())
        ),
        bucket=minio_store.bucket,
        poll_interval_seconds=0,
    )

    results = {}
    for scene in ("sb2", "sb3"):
        img = images[scene]
        submitted = await acts.submit(
            VideoSubmitRequest(ep, "wf", "run", scene, sb, img, DURATIONS[scene], round=1)
        )
        assert submitted.artifact is None
        result = await acts.await_video(
            VideoAwaitRequest(
                ep, "wf", "run", scene, sb, img, DURATIONS[scene], submitted.reservation_id
            )
        )
        results[scene] = (submitted.reservation_id, result)
    assert gen.submit_calls == 2

    for scene, (reservation_id, result) in results.items():
        artifact = parse_scene_video_artifact(await minio_store.get_json(result.object_key))
        assert artifact.scene_id == scene and artifact.source_image.artifact_id == images[scene]
        assert artifact.requested_duration_ms == DURATIONS[scene] and not artifact.has_audio
        assert (
            await readback_sha256(minio_store, artifact.media.object_key) == artifact.media.sha256
        )
        async with f() as session:
            row = await ProviderReservationRepository(session).get(reservation_id)
        assert row is not None and row.provider_job_ref
        assert row.status is ReservationStatus.SPENT and row.reconciled_by == "evidence"
        expected = Decimal(str(DURATIONS[scene] / 1000 * SEEDANCE_RATE)).quantize(Decimal("0.0001"))
        assert row.estimated_cost_usd == expected
        assert row.outcome_artifact_id == result.artifact_id
        assert row.raw_output_key and await minio_store.exists(row.raw_output_key)

    async with f() as session:
        current = await ArtifactMetadataRepository(session).list_current_by_type(
            ep, ArtifactType.SCENE_VIDEO
        )
    assert sorted((m.scene_id, m.id) for m in current) == sorted(  # type: ignore[type-var]
        (s, r.artifact_id) for s, (_, r) in results.items()
    )

    again = await acts.submit(
        VideoSubmitRequest(ep, "wf", "run", "sb2", sb, images["sb2"], DURATIONS["sb2"], round=1)
    )
    assert again.artifact is not None and again.artifact.reused
    assert again.artifact.artifact_id == results["sb2"][1].artifact_id
    assert gen.submit_calls == 2
    async with f() as session:
        jobs = sorted(
            (j.scene_id, j.status)
            for j in await JobRepository(session).list_for_episode(ep)
            if j.type is JobType.PRODUCE_SCENE_VIDEO
        )
    assert jobs == [
        ("sb2", JobStatus.SKIPPED),
        ("sb2", JobStatus.SUCCEEDED),
        ("sb3", JobStatus.SUCCEEDED),
    ]
