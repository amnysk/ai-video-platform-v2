"""日次の入口ゲートの純粋な判定（ADR-0058）。I/O をしない。

breaker・保留数を読むのは Activity。ここは「読んだ値から何を決めるか」だけを持つ。
provider の名前は書かず、``contracts`` の対応表（``STAGE_PROVIDERS`` / ``PROVIDER_FAMILY``）から
導く（ADR-0017: domain は provider の名前を知らない）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from contracts.daily_gate import DailyRunMode, OpenHold
from contracts.pipeline import STAGE_PROVIDERS, PipelineStage
from contracts.provider_health import (
    DEFAULT_ACCOUNT_LABEL,
    PROVIDER_FAMILY,
    BreakerSnapshot,
    BreakerState,
    HoldReason,
    ProviderAccount,
    RefusalKind,
)

#: 厳しい順（先頭が最も厳しい）
_SEVERITY: tuple[BreakerState, ...] = (
    BreakerState.OPEN,
    BreakerState.HALF_OPEN,
    BreakerState.CLOSED,
)


def worst_breaker_state(states: Iterable[BreakerState]) -> BreakerState:
    present = set(states)
    for state in _SEVERITY:
        if state in present:
            return state
    return BreakerState.CLOSED


def decide_daily_mode(
    *, breaker_states: Sequence[BreakerState], open_hold_count: int, max_held: int
) -> tuple[DailyRunMode, list[str]]:
    """保留の上限が先（解放待ちが溜まっている間は breaker が閉じていても積まない）。

    ``half_open`` は所有者の承認の下で上限付きの再開を許している状態で、日次はその枠を使わない
    （日次は再開しない / INV-78）。``open`` と同じく新しい有料生成を始めない。
    """
    if open_hold_count >= max_held:
        return DailyRunMode.SKIP_BACKLOG, [
            f"open holds {open_hold_count} >= MAX_HELD_EPISODES {max_held}"
        ]
    worst = worst_breaker_state(breaker_states)
    if worst is not BreakerState.CLOSED:
        return DailyRunMode.PAID_HELD, [f"provider breaker is {worst.value}"]
    return DailyRunMode.NORMAL, []


def stage_accounts(stage: PipelineStage) -> tuple[ProviderAccount, ...]:
    """工程が使う有料 provider の課金アカウント（重複なし、順序保存）。"""
    accounts: list[ProviderAccount] = []
    for provider in STAGE_PROVIDERS[stage]:
        family = PROVIDER_FAMILY.get(provider)
        if family is None:
            continue
        account = ProviderAccount(family, DEFAULT_ACCOUNT_LABEL)
        if account not in accounts:
            accounts.append(account)
    return tuple(accounts)


def hold_reason_for(snapshot: BreakerSnapshot) -> HoldReason:
    """残高不足と確定できたときだけ ``WAITING_FOR_CREDITS``。それ以外は人の判断を待つ。"""
    if snapshot.reason is RefusalKind.BALANCE_EXHAUSTED:
        return HoldReason.WAITING_FOR_CREDITS
    return HoldReason.PROVIDER_REFUSAL_UNKNOWN


@dataclass(frozen=True)
class HoldClassification:
    count: int = 0
    oldest_days: float = 0.0
    #: 保留日数が猶予内: 停滞・完成期限の検査から除く（意図した停止）
    exempt_ids: set[str] = field(default_factory=set)
    #: 猶予を超えた: 通報する
    escalated_ids: set[str] = field(default_factory=set)


def classify_holds(
    holds: Sequence[OpenHold], now: datetime, escalation_days: int
) -> HoldClassification:
    exempt: set[str] = set()
    escalated: set[str] = set()
    oldest = 0.0
    for hold in holds:
        age_days = max(0.0, (now - hold.held_since).total_seconds() / 86400)
        oldest = max(oldest, age_days)
        (escalated if age_days >= escalation_days else exempt).add(hold.episode_id)
    return HoldClassification(
        count=len(holds), oldest_days=oldest, exempt_ids=exempt, escalated_ids=escalated
    )
