"""シーン単位の Artifact / job / 予約（ADR-0018）と provider job 参照（ADR-0017）。SQLite。

実PostgreSQL版: tests/integration/test_production_persistence.py。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from contracts.states import ArtifactType, JobType, ProviderCall, ReservationStatus
from domain.errors import InvalidTransitionError
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


async def _record(repo, episode_id, sha, *, scene_id=None, type=ArtifactType.SCENE_IMAGE, ih=None):
    return await repo.record(
        episode_id=episode_id,
        artifact_type=type,
        schema_version="1.0",
        bucket="b",
        object_key=f"k/{scene_id}/{sha}",
        sha256=sha,
        input_hash=ih or sha,
        scene_id=scene_id,
    )


async def test_scene_artifacts_have_independent_current_generations(session) -> None:
    episode_id = await _episode(session)
    repo = ArtifactMetadataRepository(session)
    a1 = await _record(repo, episode_id, "1" * 64, scene_id="sb1")
    b1 = await _record(repo, episode_id, "2" * 64, scene_id="sb2")
    a2 = await _record(repo, episode_id, "3" * 64, scene_id="sb1")
    await session.commit()

    current_a = await repo.find_current_by_type(episode_id, ArtifactType.SCENE_IMAGE, "sb1")
    current_b = await repo.find_current_by_type(episode_id, ArtifactType.SCENE_IMAGE, "sb2")
    assert current_a is not None and current_a.id == a2.id and current_a.scene_id == "sb1"
    assert current_b is not None and current_b.id == b1.id
    assert a1.id != a2.id
    # scene を指定しない引きは Episode 単位の行だけを見る
    assert await repo.find_current_by_type(episode_id, ArtifactType.SCENE_IMAGE) is None
    currents = await repo.list_current_by_type(episode_id, ArtifactType.SCENE_IMAGE)
    assert [m.scene_id for m in currents] == ["sb1", "sb2"]


async def test_same_content_in_two_scenes_is_two_rows(session) -> None:
    episode_id = await _episode(session)
    repo = ArtifactMetadataRepository(session)
    first = await _record(repo, episode_id, "9" * 64, scene_id="sb1")
    second = await _record(repo, episode_id, "9" * 64, scene_id="sb2")
    again = await _record(repo, episode_id, "9" * 64, scene_id="sb1")
    await session.commit()
    assert first.id != second.id
    assert again.id == first.id  # INV-17 は scene キーの中で成り立つ


async def test_find_current_by_input_hash_is_scene_scoped(session) -> None:
    episode_id = await _episode(session)
    repo = ArtifactMetadataRepository(session)
    await _record(repo, episode_id, "1" * 64, scene_id="sb1", ih="h" * 64)
    await session.commit()
    assert await repo.find_current(episode_id, ArtifactType.SCENE_IMAGE, "h" * 64, "sb1")
    assert await repo.find_current(episode_id, ArtifactType.SCENE_IMAGE, "h" * 64, "sb2") is None
    assert await repo.find_current(episode_id, ArtifactType.SCENE_IMAGE, "h" * 64) is None


async def test_scene_rows_do_not_disturb_episode_level_artifacts(session) -> None:
    """回帰: script / storyboard は scene_id を持たず、従来どおり1本の現行を持つ。"""
    episode_id = await _episode(session)
    repo = ArtifactMetadataRepository(session)
    s1 = await _record(repo, episode_id, "a" * 64, type=ArtifactType.STORYBOARD)
    await _record(repo, episode_id, "b" * 64, scene_id="sb1", type=ArtifactType.SCENE_IMAGE)
    await session.commit()
    current = await repo.find_current_by_type(episode_id, ArtifactType.STORYBOARD)
    assert current is not None and current.id == s1.id and current.scene_id is None

    s2 = await _record(repo, episode_id, "c" * 64, type=ArtifactType.STORYBOARD)
    await session.commit()
    current = await repo.find_current_by_type(episode_id, ArtifactType.STORYBOARD)
    assert current is not None and current.id == s2.id
    scene = await repo.find_current_by_type(episode_id, ArtifactType.SCENE_IMAGE, "sb1")
    assert scene is not None  # シーン単位の現行は降ろされない


async def test_scene_versions_restart_per_scene(session) -> None:
    from sqlalchemy import select

    from infrastructure.db.models import ArtifactMetadataRow

    episode_id = await _episode(session)
    repo = ArtifactMetadataRepository(session)
    await _record(repo, episode_id, "1" * 64, scene_id="sb1")
    await _record(repo, episode_id, "2" * 64, scene_id="sb1")
    await _record(repo, episode_id, "3" * 64, scene_id="sb2")
    await session.commit()
    rows = (await session.scalars(select(ArtifactMetadataRow))).all()
    assert sorted((r.scene_id, r.version) for r in rows) == [("sb1", 1), ("sb1", 2), ("sb2", 1)]


async def test_open_jobs_are_found_per_scene(session) -> None:
    episode_id = await _episode(session)
    jobs = JobRepository(session)
    j1 = await jobs.create(episode_id=episode_id, type=JobType.PRODUCE_SCENE_IMAGE, scene_id="sb1")
    await jobs.create(episode_id=episode_id, type=JobType.PRODUCE_SCENE_IMAGE, scene_id="sb2")
    await session.commit()
    found = await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_IMAGE, "sb1")
    assert found is not None and found.id == j1.id and found.scene_id == "sb1"
    assert await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_IMAGE, "sb3") is None
    assert await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_IMAGE) is None

    await jobs.start(j1.id)
    await jobs.succeed(j1.id)
    await session.commit()
    assert await jobs.find_open(episode_id, JobType.PRODUCE_SCENE_IMAGE, "sb1") is None


async def _reserve(
    session, episode_id, *, key: str, scene_id: str | None, cost=None, provider=None
):
    repo = ProviderReservationRepository(session)
    row = await repo.reserve(
        episode_id=episode_id,
        provider=provider or ProviderCall.FAL_IMAGE,
        idempotency_key=key,
        input_hash="h" * 64,
        round=1,
        scene_id=scene_id,
        estimated_cost_usd=cost,
    )
    await session.commit()
    return repo, row


async def test_unreconciled_reservations_are_scene_scoped(session) -> None:
    episode_id = await _episode(session)
    repo, _ = await _reserve(session, episode_id, key="k1", scene_id="sb1")
    await _reserve(session, episode_id, key="k2", scene_id="sb3")
    await _reserve(
        session, episode_id, key="k3", scene_id=None, provider=ProviderCall.CODEX_STORYBOARD
    )
    assert [
        r.idempotency_key
        for r in await repo.find_unreconciled(episode_id, ProviderCall.FAL_IMAGE, "sb1")
    ] == ["k1"]
    assert await repo.find_unreconciled(episode_id, ProviderCall.FAL_IMAGE, "sb2") == []
    # scene を指定しない引きは Episode 単位の行だけを見る
    assert await repo.find_unreconciled(episode_id, ProviderCall.FAL_IMAGE) == []
    assert [
        r.idempotency_key
        for r in await repo.find_unreconciled(episode_id, ProviderCall.CODEX_STORYBOARD)
    ] == ["k3"]


async def test_estimated_cost_is_persisted(session) -> None:
    episode_id = await _episode(session)
    repo, row = await _reserve(session, episode_id, key="k", scene_id="sb1", cost=Decimal("0.0350"))
    loaded = await repo.find_by_key("k")
    assert loaded is not None and loaded.estimated_cost_usd == Decimal("0.0350")
    assert row.scene_id == "sb1"


async def test_provider_job_ref_requires_dispatch_and_is_write_once(session) -> None:
    episode_id = await _episode(session)
    repo, row = await _reserve(session, episode_id, key="k", scene_id="sb1")

    with pytest.raises(InvalidTransitionError, match="not dispatched"):
        await repo.record_provider_job_ref(row.id, "job-1")

    await repo.mark_dispatched(row.id)
    await session.commit()
    written = await repo.record_provider_job_ref(row.id, "job-1")
    await session.commit()
    assert written.provider_job_ref == "job-1"
    assert written.status is ReservationStatus.RESERVED

    same = await repo.record_provider_job_ref(row.id, "job-1")
    assert same.provider_job_ref == "job-1"
    with pytest.raises(InvalidTransitionError, match="different"):
        await repo.record_provider_job_ref(row.id, "job-2")
    with pytest.raises(ValueError):
        await repo.record_provider_job_ref(row.id, "")


async def test_provider_job_ref_cannot_be_added_after_reconciliation(session) -> None:
    episode_id = await _episode(session)
    repo, row = await _reserve(session, episode_id, key="k", scene_id="sb1")
    await repo.mark_dispatched(row.id)
    await repo.mark_spent(row.id, raw_output_key=None, reconciled_by="conservative")
    await session.commit()
    with pytest.raises(InvalidTransitionError, match="spent"):
        await repo.record_provider_job_ref(row.id, "job-1")
    loaded = await repo.get(row.id)
    assert loaded is not None and loaded.provider_job_ref is None


async def test_scene_scope_is_enforced_by_the_schema(session) -> None:
    """ADR-0018: シーン単位の型は scene_id 必須、Episode 単位の型は scene_id NULL。"""
    from sqlalchemy.exc import IntegrityError

    episode_id = await _episode(session)
    repo = ArtifactMetadataRepository(session)
    with pytest.raises(IntegrityError):
        await _record(repo, episode_id, "1" * 64, scene_id=None)
    await session.rollback()
    with pytest.raises(IntegrityError):
        await _record(repo, episode_id, "2" * 64, scene_id="sb1", type=ArtifactType.SCRIPT)
    await session.rollback()
    with pytest.raises(IntegrityError):
        await JobRepository(session).create(episode_id=episode_id, type=JobType.PRODUCE_SCENE_VOICE)
    await session.rollback()
    with pytest.raises(IntegrityError):
        await _reserve(
            session, episode_id, key="k9", scene_id=None, provider=ProviderCall.FAL_VIDEO
        )


async def test_mark_dispatched_is_a_one_shot_conditional_update(session) -> None:
    from domain.errors import UnreconciledReservationError

    episode_id = await _episode(session)
    repo, row = await _reserve(session, episode_id, key="k", scene_id="sb1")
    first = await repo.mark_dispatched(row.id)
    await session.commit()
    assert first.dispatched_at is not None
    # 2度目（並行する submit・再実行）は「呼んだかもしれない」行を上書きしない
    with pytest.raises(UnreconciledReservationError):
        await repo.mark_dispatched(row.id)
    await session.rollback()
    loaded = await repo.get(row.id)
    assert loaded is not None and loaded.dispatched_at == first.dispatched_at

    _, spent = await _reserve(session, episode_id, key="k2", scene_id="sb2")
    await repo.mark_dispatched(spent.id)
    await repo.mark_spent(spent.id, raw_output_key=None, reconciled_by="conservative")
    await session.commit()
    with pytest.raises(UnreconciledReservationError):
        await repo.mark_dispatched(spent.id)
