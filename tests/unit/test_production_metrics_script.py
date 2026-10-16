"""``scripts/production-metrics.py`` の集計（ADR-0035 §5）。sqlite 上で SELECT だけ。"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from decimal import Decimal

from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from infrastructure.db.models import EpisodeRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderRejectionRepository,
    ProviderReservationRepository,
)
from tests.support.production import LIKENESS_REJECTION

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "production-metrics.py"


def _load():
    spec = importlib.util.spec_from_file_location("production_metrics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _episode(session, status: EpisodeStatus) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    row = await session.get(EpisodeRow, uuid.UUID(episode.id))
    row.status = status.value
    await session.flush()
    return episode.id


async def _paid(session, episode_id, provider, scene, cost, *, round=1, rejected=False):
    reservations = ProviderReservationRepository(session)
    row = await reservations.reserve(
        episode_id=episode_id,
        provider=provider,
        idempotency_key=uuid.uuid4().hex,
        input_hash=uuid.uuid4().hex * 2,
        round=round,
        scene_id=scene,
        estimated_cost_usd=Decimal(cost),
    )
    await reservations.mark_dispatched(row.id)
    spent = await reservations.mark_spent(
        row.id, raw_output_key=None if rejected else "k", input_rejected_by_provider=rejected
    )
    if rejected:
        await ProviderRejectionRepository(session).record(
            episode_id=episode_id,
            scene_id=scene,
            provider=provider,
            reservation_id=spent.id,
            input_hash=spent.input_hash,
            rejection=LIKENESS_REJECTION,
            source_media_sha256=None,
        )


async def test_metrics_report_rates_alternatives_cost_and_completion(session_factory) -> None:
    async with session_factory() as session:
        done = await _episode(session, EpisodeStatus.UPLOADED)
        stuck = await _episode(session, EpisodeStatus.BLOCKED)
        await _paid(session, done, ProviderCall.FAL_IMAGE, "sb1", "0.04")
        await _paid(session, done, ProviderCall.FAL_VIDEO, "sb1", "0.97")
        await _paid(session, stuck, ProviderCall.FAL_VIDEO, "sb2", "0.97", rejected=True)
        await _paid(session, stuck, ProviderCall.FAL_VIDEO, "sb2", "0.97", round=2)
        await ArtifactMetadataRepository(session).record(
            episode_id=stuck,
            artifact_type=ArtifactType.SCENE_VISUAL_OVERRIDE,
            schema_version="1.0",
            bucket="b",
            object_key="o",
            sha256="s" * 64,
            scene_id="sb2",
        )
        await session.commit()

    async with session_factory() as session:
        report = await _load().collect(session)

    video = report["providers"]["fal_video"]
    assert video == {
        "submitted": 3,
        "rejected": 1,
        "rejection_rate": round(1 / 3, 4),
        "later_round_submits": 1,
    }
    assert report["providers"]["fal_image"]["rejection_rate"] == 0.0
    assert report["scene_alternatives"] == {"total": 1, "episodes_with_alternatives": 1}
    assert report["episodes"]["completion_rate"] == 0.5
    assert report["estimated_cost_usd"]["total"] == "2.9500"
    assert report["estimated_cost_usd"]["per_uploaded_episode"] == "1.0100"
