"""日次の入口ゲートの純粋な判定（ADR-0058 / INV-76・INV-77）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from contracts.daily_gate import DailyRunMode, OpenHold
from contracts.pipeline import PipelineStage
from contracts.provider_health import (
    DEFAULT_ACCOUNT_LABEL,
    MAX_HELD_EPISODES,
    BreakerSnapshot,
    BreakerState,
    HoldReason,
    ProviderAccount,
    ProviderFamily,
    RefusalKind,
)
from domain.daily_gate import (
    classify_holds,
    decide_daily_mode,
    hold_reason_for,
    stage_accounts,
    worst_breaker_state,
)

NOW = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)


def test_closed_breaker_and_no_backlog_runs_normally() -> None:
    mode, reasons = decide_daily_mode(
        breaker_states=[BreakerState.CLOSED], open_hold_count=0, max_held=MAX_HELD_EPISODES
    )
    assert mode is DailyRunMode.NORMAL
    assert reasons == []


def test_open_and_half_open_breaker_hold_paid_work_but_keep_planning() -> None:
    for state in (BreakerState.OPEN, BreakerState.HALF_OPEN):
        mode, reasons = decide_daily_mode(
            breaker_states=[state], open_hold_count=0, max_held=MAX_HELD_EPISODES
        )
        assert mode is DailyRunMode.PAID_HELD, state
        assert any(state.value in r for r in reasons)


def test_backlog_at_the_cap_skips_even_when_the_breaker_is_closed() -> None:
    """保留が解放待ちの間は、breaker が閉じていても新しい Episode を積まない。"""
    mode, _ = decide_daily_mode(
        breaker_states=[BreakerState.CLOSED],
        open_hold_count=MAX_HELD_EPISODES,
        max_held=MAX_HELD_EPISODES,
    )
    assert mode is DailyRunMode.SKIP_BACKLOG


def test_one_below_the_cap_still_creates_an_episode() -> None:
    mode, _ = decide_daily_mode(
        breaker_states=[BreakerState.OPEN],
        open_hold_count=MAX_HELD_EPISODES - 1,
        max_held=MAX_HELD_EPISODES,
    )
    assert mode is DailyRunMode.PAID_HELD


def test_worst_breaker_state_wins_across_accounts() -> None:
    assert (
        worst_breaker_state([BreakerState.CLOSED, BreakerState.HALF_OPEN, BreakerState.OPEN])
        is BreakerState.OPEN
    )
    assert worst_breaker_state([BreakerState.CLOSED, BreakerState.HALF_OPEN]) is (
        BreakerState.HALF_OPEN
    )
    assert worst_breaker_state([]) is BreakerState.CLOSED


def test_only_the_production_stage_touches_a_breaker_account() -> None:
    fal = ProviderAccount(ProviderFamily.FAL, DEFAULT_ACCOUNT_LABEL)
    assert stage_accounts(PipelineStage.PRODUCTION) == (fal,)
    for stage in (PipelineStage.SCRIPT, PipelineStage.STORYBOARD, PipelineStage.RENDER):
        assert stage_accounts(stage) == ()


def _snap(reason: RefusalKind | None) -> BreakerSnapshot:
    return BreakerSnapshot(
        account_key="fal:default", state=BreakerState.OPEN, reason=reason, opened_at=NOW
    )


def test_only_a_confirmed_balance_exhaustion_is_called_waiting_for_credits() -> None:
    """fal の 403 のように原因が確定できないものを、残高不足と断定しない。"""
    assert hold_reason_for(_snap(RefusalKind.BALANCE_EXHAUSTED)) is HoldReason.WAITING_FOR_CREDITS
    for reason in (RefusalKind.UNKNOWN, RefusalKind.AUTH_INVALID, RefusalKind.PERMISSION_DENIED):
        assert hold_reason_for(_snap(reason)) is HoldReason.PROVIDER_REFUSAL_UNKNOWN
    assert hold_reason_for(_snap(None)) is HoldReason.PROVIDER_REFUSAL_UNKNOWN


def _hold(ep: str, age_days: float) -> OpenHold:
    return OpenHold(
        episode_id=ep,
        held_since=NOW - timedelta(days=age_days),
        reason=HoldReason.WAITING_FOR_CREDITS.value,
        account_key="fal:default",
        resumable_stage="production",
    )


def test_recent_holds_are_exempt_and_long_holds_are_escalated() -> None:
    result = classify_holds([_hold("a", 0.5), _hold("b", 3.0), _hold("c", 10)], NOW, 3)
    assert result.exempt_ids == {"a"}
    assert result.escalated_ids == {"b", "c"}
    assert result.oldest_days == 10
    assert result.count == 3


def test_no_holds_classify_to_nothing() -> None:
    result = classify_holds([], NOW, 3)
    assert result.count == 0
    assert result.exempt_ids == set()
    assert result.escalated_ids == set()
    assert result.oldest_days == 0
