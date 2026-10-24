"""provider breaker と保留の永続化（ADR-0057 / INV-72 / INV-73）。SQLite + fake。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from contracts.provider_health import (
    DEFAULT_ACCOUNT_LABEL,
    BreakerState,
    HoldReason,
    ProviderAccount,
    ProviderFamily,
    RefusalKind,
)
from contracts.states import ProviderCall
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository

FAL = ProviderAccount(ProviderFamily.FAL)
OTHER = ProviderAccount(ProviderFamily.FAL, "channel-b")


@pytest.fixture
def health(session_factory) -> PostgresProviderHealth:
    return PostgresProviderHealth(session_factory)


async def _episode(session_factory) -> str:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    return str(episode.id)


async def _open(health, episode_id=None, **kw):
    return await health.open_breaker(
        FAL,
        reason=kw.pop("reason", RefusalKind.BALANCE_EXHAUSTED),
        http_status=403,
        evidence="User is locked: Exhausted balance",
        episode_id=episode_id,
        **kw,
    )


async def test_default_snapshot_is_closed(health) -> None:
    snap = await health.snapshot(FAL)
    assert snap.state is BreakerState.CLOSED and snap.reason is None
    assert snap.account_key == f"fal:{DEFAULT_ACCOUNT_LABEL}"


async def test_open_is_idempotent_and_keeps_the_first_reason(health, session_factory) -> None:
    ep = await _episode(session_factory)
    assert await _open(health, ep) is True
    assert await _open(health, ep, reason=RefusalKind.UNKNOWN) is False
    detail = await health.detail(FAL)
    assert detail is not None
    assert detail.snapshot.state is BreakerState.OPEN
    assert detail.snapshot.reason is RefusalKind.BALANCE_EXHAUSTED  # 最初の理由を保つ
    assert detail.refusal_count == 2  # 観測した拒否は数える
    assert detail.evidence == "User is locked: Exhausted balance"


async def test_repeated_opens_leave_exactly_one_row_and_one_winner(health) -> None:
    """逐次の再入。真の並行（別接続）は tests/integration/test_provider_breaker_postgres.py。

    unit の SQLite は接続を1本共有するので、並行 session の commit/rollback が互いの
    トランザクションを巻き込む。ここで gather しても並行性の証明にならない。
    """
    results = [await _open(health) for _ in range(8)]
    assert results.count(True) == 1
    detail = await health.detail(FAL)
    assert detail is not None and detail.refusal_count == 8


async def test_breaker_never_closes_with_time(health) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=30)
    await _open(health, now=long_ago)
    snap = await health.snapshot(FAL)
    assert snap.state is BreakerState.OPEN
    assert snap.opened_at is not None
    # accepted の観測（open では閉じない。承認が先）
    assert await health.record_accepted(FAL) is False
    assert (await health.snapshot(FAL)).state is BreakerState.OPEN


async def test_accounts_are_independent(health) -> None:
    await _open(health)
    assert (await health.snapshot(OTHER)).state is BreakerState.CLOSED


async def test_half_open_needs_open_then_accepted_submit_closes_it(health) -> None:
    assert (
        await health.half_open(
            FAL, approval_fingerprint="a" * 16, approved_by="o", approval_evidence="x"
        )
        is False
    )  # closed からは half_open にならない
    await _open(health)
    assert await health.half_open(
        FAL, approval_fingerprint="a" * 16, approved_by="owner", approval_evidence="topped up"
    )
    detail = await health.detail(FAL)
    assert detail is not None
    assert detail.snapshot.state is BreakerState.HALF_OPEN
    assert detail.approval_fingerprint == "a" * 16 and detail.approved_by == "owner"
    assert await health.record_accepted(FAL) is True
    assert (await health.snapshot(FAL)).state is BreakerState.CLOSED


async def test_refusal_in_half_open_reopens(health) -> None:
    await _open(health)
    await health.half_open(
        FAL, approval_fingerprint="a" * 16, approved_by="o", approval_evidence="x"
    )
    assert await _open(health) is True  # half_open → open はこの呼び出しが開いた
    assert (await health.snapshot(FAL)).state is BreakerState.OPEN


async def test_close_requires_open_or_half_open(health) -> None:
    kw = {"approval_fingerprint": "b" * 16, "approved_by": "o", "approval_evidence": "x"}
    assert await health.close(FAL, **kw) is False
    await _open(health)
    assert await health.close(FAL, **kw) is True
    assert (await health.snapshot(FAL)).state is BreakerState.CLOSED
    # 閉じた後の再オープン
    assert await _open(health) is True
    assert (await health.snapshot(FAL)).state is BreakerState.OPEN


async def test_hold_is_idempotent_and_records_snapshot(health, session_factory) -> None:
    ep = await _episode(session_factory)
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        row = await repo.reserve(
            episode_id=ep,
            provider=ProviderCall.FAL_IMAGE,
            idempotency_key="k" * 64,
            input_hash="h" * 64,
            round=1,
            scene_id="sb1",
        )
        await session.commit()
    for _ in range(3):
        await health.hold_episode(
            episode_id=ep,
            scene_id="sb2",
            account=FAL,
            reason=HoldReason.WAITING_FOR_CREDITS,
            resumable_stage="production",
            generation_profile_id="profile-v1",
        )
    holds = await health.list_open_holds()
    assert len(holds) == 1
    hold = holds[0]
    assert hold.episode_id == ep and hold.scene_id == "sb2"
    assert hold.reason is HoldReason.WAITING_FOR_CREDITS
    assert hold.resumable_stage == "production" and hold.generation_profile_id == "profile-v1"
    assert hold.snapshot["reservation_ids"] == [str(row.id)]
    assert hold.snapshot["succeeded_artifact_ids"] == []
    assert await health.open_hold_count() == 1


async def test_holds_for_different_scenes_share_one_held_episode(health, session_factory) -> None:
    ep = await _episode(session_factory)
    for scene in ("sb1", "sb2", None):
        await health.hold_episode(
            episode_id=ep,
            scene_id=scene,
            account=FAL,
            reason=HoldReason.PROVIDER_REFUSAL_UNKNOWN,
            resumable_stage="production",
            generation_profile_id=None,
        )
    assert len(await health.list_open_holds()) == 3
    assert await health.open_hold_count() == 1  # Episode 単位で数える


async def test_release_frees_the_slot_and_a_new_hold_can_be_recorded(
    health, session_factory
) -> None:
    ep = await _episode(session_factory)
    kw = {
        "episode_id": ep,
        "scene_id": "sb1",
        "account": FAL,
        "reason": HoldReason.WAITING_FOR_CREDITS,
        "resumable_stage": "production",
        "generation_profile_id": None,
    }
    await health.hold_episode(**kw)
    assert await health.release_holds(ep, account=FAL, approval_fingerprint="c" * 16) == 1
    assert await health.open_hold_count() == 0
    await health.hold_episode(**kw)  # 再開後にまた止まった
    assert await health.open_hold_count() == 1


async def test_held_episodes_come_oldest_first(health, session_factory) -> None:
    first, second = await _episode(session_factory), await _episode(session_factory)
    for ep in (second, first):  # 保留した順は second → first
        await health.hold_episode(
            episode_id=ep,
            scene_id=None,
            account=FAL,
            reason=HoldReason.WAITING_FOR_CREDITS,
            resumable_stage="production",
            generation_profile_id=None,
        )
        await asyncio.sleep(0.01)
    assert await health.held_episode_ids_oldest_first(account=FAL, limit=1) == [second]
    assert await health.held_episode_ids_oldest_first(account=FAL, limit=5) == [second, first]
