"""Research の外部呼び出し台帳（``research_calls``。ADR-0037 / INV-36）。

1 依頼の外部呼び出しは、依頼に凍結した上限（種別ごと）を超えない。``reserved`` / ``spent`` /
``abandoned`` のすべてが枠を数え、``call_seq`` は再利用しない。最後の砦は DB の
``UNIQUE(request_id, provider_call, call_seq)``。

理由は docs/testing/research-persistence-rationale.md。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from contracts.research import ResearchCall, ResearchCallStatus, parse_research_spec
from domain.errors import InvalidTransitionError
from domain.research.errors import (
    ResearchAmbiguousCallError,
    ResearchBudgetExceededError,
    ResearchIdempotencyConflictError,
)
from infrastructure.db.models import ResearchCallRow
from infrastructure.db.research_repositories import (
    ResearchCallRepository,
    ResearchRequestRepository,
)
from tests.support.research import evidence_payload


async def _running(session, limits: dict | None = None, key: str = "evidence:ep-1") -> str:
    payload = evidence_payload(limits=limits or {"max_searches": 2, "max_fetches": 3})
    repo = ResearchRequestRepository(session)
    created = await repo.create_or_get(
        idempotency_key=key,
        spec=parse_research_spec(payload),
        provider_config_version="provider-config-1",
    )
    await repo.mark_running(created.id)
    return created.id


async def _reserve(session, request_id: str, n: int, call=ResearchCall.SEARCH, **kwargs):
    return await ResearchCallRepository(session).reserve(
        request_id=request_id,
        call=call,
        idempotency_key=f"{request_id}:{call.value}:{n}",
        input_hash=kwargs.pop("input_hash", f"{n:064x}"),
        provider="fake",
        **kwargs,
    )


async def test_calls_get_consecutive_sequence_numbers_per_kind(session) -> None:
    request_id = await _running(session)
    first = await _reserve(session, request_id, 1)
    second = await _reserve(session, request_id, 2)
    fetch = await _reserve(session, request_id, 1, call=ResearchCall.FETCH)
    assert (first.call_seq, second.call_seq, fetch.call_seq) == (1, 2, 1)
    assert first.status is ResearchCallStatus.RESERVED


async def test_the_ceiling_stops_the_next_reservation_before_insert(session) -> None:
    request_id = await _running(session)
    await _reserve(session, request_id, 1)
    await _reserve(session, request_id, 2)
    with pytest.raises(ResearchBudgetExceededError):
        await _reserve(session, request_id, 3)
    count = await session.scalar(select(func.count()).select_from(ResearchCallRow))
    assert count == 2


async def test_abandoned_and_spent_calls_still_count_and_seq_is_never_reused(session) -> None:
    repo = ResearchCallRepository(session)
    request_id = await _running(session)
    first = await _reserve(session, request_id, 1)
    await repo.mark_abandoned(first.id, reason="cancelled before dispatch")
    second = await _reserve(session, request_id, 2)
    await repo.mark_dispatched(second.id)
    await repo.mark_spent(second.id)
    assert second.call_seq == 2
    with pytest.raises(ResearchBudgetExceededError):
        await _reserve(session, request_id, 3)


async def test_re_reserving_the_same_key_returns_the_same_call_and_uses_no_budget(
    session,
) -> None:
    request_id = await _running(session)
    first = await _reserve(session, request_id, 1)
    again = await _reserve(session, request_id, 1)
    assert again.id == first.id
    second = await _reserve(session, request_id, 2)
    assert second.call_seq == 2


async def test_same_key_for_a_different_call_is_a_conflict(session) -> None:
    request_id = await _running(session)
    await _reserve(session, request_id, 1)
    with pytest.raises(ResearchIdempotencyConflictError):
        await ResearchCallRepository(session).reserve(
            request_id=request_id,
            call=ResearchCall.SEARCH,
            idempotency_key=f"{request_id}:search:1",
            input_hash="f" * 64,
            provider="fake",
        )


async def test_calls_are_reserved_only_while_the_request_is_running(session) -> None:
    repo = ResearchRequestRepository(session)
    created = await repo.create_or_get(
        idempotency_key="evidence:queued",
        spec=parse_research_spec(evidence_payload()),
        provider_config_version="provider-config-1",
    )
    with pytest.raises(InvalidTransitionError):
        await _reserve(session, created.id, 1)
    with pytest.raises(InvalidTransitionError):
        await _reserve(session, str(uuid.uuid4()), 1)


async def test_a_dispatched_call_without_an_outcome_blocks_resending_the_same_input(
    session,
) -> None:
    repo = ResearchCallRepository(session)
    request_id = await _running(session)
    first = await _reserve(session, request_id, 1, input_hash="c" * 64)
    await repo.mark_dispatched(first.id)
    with pytest.raises(ResearchAmbiguousCallError):
        await _reserve(session, request_id, 2, input_hash="c" * 64)
    other = await _reserve(session, request_id, 3, input_hash="d" * 64)  # 別の入力は止めない
    assert other.call_seq == 2
    assert [c.id for c in await repo.find_ambiguous(request_id)] == [first.id]


async def test_a_dispatched_call_cannot_be_abandoned_and_is_not_dispatched_twice(
    session,
) -> None:
    repo = ResearchCallRepository(session)
    request_id = await _running(session)
    call = await _reserve(session, request_id, 1)
    await repo.mark_dispatched(call.id)
    with pytest.raises(InvalidTransitionError):
        await repo.mark_abandoned(call.id, reason="x")
    with pytest.raises(ResearchAmbiguousCallError):
        await repo.mark_dispatched(call.id)
    spent = await repo.mark_spent(call.id, error_summary="HTTP 500")
    assert spent.status is ResearchCallStatus.SPENT and spent.settled_at is not None
    with pytest.raises(InvalidTransitionError):
        await repo.mark_spent(call.id)


async def test_money_budget_counts_every_call_that_may_have_cost(session) -> None:
    request_id = await _running(
        session, limits={"max_searches": 5, "max_cost_usd": "0.0300", "max_youtube_units": 250}
    )
    repo = ResearchCallRepository(session)
    first = await _reserve(session, request_id, 1, estimated_cost_usd=Decimal("0.02"))
    await repo.mark_dispatched(first.id)
    await repo.mark_spent(first.id)
    with pytest.raises(ResearchBudgetExceededError):
        await _reserve(session, request_id, 2, estimated_cost_usd=Decimal("0.02"))
    await _reserve(session, request_id, 3, estimated_cost_usd=Decimal("0.01"))
    await _reserve(session, request_id, 4, quota_units=200)
    with pytest.raises(ResearchBudgetExceededError):
        await _reserve(session, request_id, 5, quota_units=100)


async def test_abandoned_calls_do_not_count_toward_money(session) -> None:
    request_id = await _running(session, limits={"max_searches": 5, "max_cost_usd": "0.0200"})
    repo = ResearchCallRepository(session)
    first = await _reserve(session, request_id, 1, estimated_cost_usd=Decimal("0.02"))
    await repo.mark_abandoned(first.id, reason="never sent")
    second = await _reserve(session, request_id, 2, estimated_cost_usd=Decimal("0.02"))
    assert second.call_seq == 2  # 件数は数える、金額は数えない


async def test_the_database_rejects_a_duplicate_call_seq(session) -> None:
    """アプリの採番にバグがあっても、同じ番号の2行目を DB が拒否する（INV-36 の最後の砦）。"""
    request_id = await _running(session)
    call = await _reserve(session, request_id, 1)
    session.add(
        ResearchCallRow(
            id=uuid.uuid4(),
            request_id=uuid.UUID(request_id),
            provider_call=ResearchCall.SEARCH.value,
            call_seq=call.call_seq,
            idempotency_key="another-key",
            input_hash="e" * 64,
            provider="fake",
            status=ResearchCallStatus.RESERVED.value,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_racing_writer_does_not_push_the_count_past_the_ceiling(session) -> None:
    """採番の読み取りと INSERT の間に別の書き手が同じ番号を取ったら、読み直して次の番号へ。

    別セッションの commit を、採番を読んだ直後に同じ番号の行を差し込むことで決定的に再現する
    （SQLite のインメモリ DB は1接続なので、本物の並行トランザクションは integration で見る）。
    """
    request_id = await _running(session)
    repo = ResearchCallRepository(session)
    original = repo._max_seq
    raced = False

    async def stale_max(request_uuid: uuid.UUID, call: ResearchCall) -> int:
        nonlocal raced
        seen = await original(request_uuid, call)
        if not raced:
            raced = True
            await session.execute(
                insert(ResearchCallRow).values(
                    id=uuid.uuid4(),
                    request_id=request_uuid,
                    provider_call=call.value,
                    call_seq=seen + 1,
                    idempotency_key="racer",
                    input_hash="9" * 64,
                    provider="fake",
                    status=ResearchCallStatus.RESERVED.value,
                )
            )
        return seen

    repo._max_seq = stale_max  # type: ignore[method-assign]
    mine = await repo.reserve(
        request_id=request_id,
        call=ResearchCall.SEARCH,
        idempotency_key="mine",
        input_hash="8" * 64,
        provider="fake",
    )
    assert raced and mine.call_seq == 2
    with pytest.raises(ResearchBudgetExceededError):
        await _reserve(session, request_id, 3)
    count = await session.scalar(select(func.count()).select_from(ResearchCallRow))
    assert count == 2
