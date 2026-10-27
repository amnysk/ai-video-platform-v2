"""台帳の reserve が月間 cap を原子的に守る（INV-86 / ADR-0063）。SQLite（ロジック層）。

PostgreSQL の並行性は tests/integration/test_monthly_cap_concurrency.py。
理由は docs/testing/fal-monthly-cap-rationale.md。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from contracts.provider_health import PROVIDER_FAMILY, ProviderAccount, ProviderFamily
from contracts.states import ProviderCall, ReservationStatus
from domain.budget_gate import evaluate_cap
from domain.errors import ProviderBudgetHeldError, ProviderMonthlyBudgetExceededError
from domain.provider.cost import CostState, cost_state
from infrastructure.db.models import ProviderReservationRow
from infrastructure.db.repositories import ProviderReservationRepository, input_lock_key
from infrastructure.production.provider_credits import month_spend_usd
from tests.support.monthly_cap import (
    CAP,
    NOW,
    SEEDABLE_STATES,
    count_rows,
    denial,
    new_episode,
    reserve_capped,
    seed,
)

D = Decimal
FAL_PROVIDERS = [p for p, f in PROVIDER_FAMILY.items() if f is ProviderFamily.FAL]


async def test_a_reservation_within_the_cap_succeeds_and_keeps_its_estimate(session_factory):
    await seed(session_factory, "10")
    row = await reserve_capped(session_factory, "5")
    assert row.estimated_cost_usd == D("5")
    assert await count_rows(session_factory) == 2


async def test_exactly_at_the_cap_is_allowed_and_the_next_cent_is_denied(session_factory):
    await seed(session_factory, "55")
    await reserve_capped(session_factory, "5")  # 55 + 5 == 60
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "0.01")
    assert await count_rows(session_factory) == 2


async def test_over_the_cap_is_denied_without_creating_a_row(session_factory):
    await seed(session_factory, "55")
    with pytest.raises(ProviderMonthlyBudgetExceededError) as caught:
        await reserve_capped(session_factory, "5.01")
    assert isinstance(caught.value, ProviderBudgetHeldError)  # 既存の保留経路に乗る型
    assert await count_rows(session_factory) == 1  # 台帳に何も残らない
    numbers = denial(caught.value)
    assert numbers["reason"] == "monthly_cap_exceeded"
    assert (numbers["month_counted"], numbers["estimate"], numbers["cap"]) == (
        D("55"),
        D("5.01"),
        D("60"),
    )
    assert numbers["remaining"] == D("5")


async def test_a_single_estimate_above_the_cap_is_denied_on_an_empty_month(session_factory):
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "60.0001")
    assert await count_rows(session_factory) == 0


async def test_no_cap_argument_keeps_the_old_behaviour(session_factory):
    """cap を渡さない呼び出し（既存の back-compat）は今まで通り。無制限という意味ではなく
    「この関門を使わない」。本番側は contract test が paid_job だけと固定する。"""
    await seed(session_factory, "500")
    row = await reserve_capped(session_factory, "500", cap=None)
    assert row.estimated_cost_usd == D("500")


@pytest.mark.parametrize("state", SEEDABLE_STATES)
async def test_every_cost_state_counts_except_not_charged(session_factory, state):
    """数える: 全行（reserved / spent / abandoned）。数えない: 受理されなかった確定行だけ。"""
    await seed(session_factory, "55", state)
    counted = state != CostState.NOT_CHARGED.value
    if counted:
        with pytest.raises(ProviderMonthlyBudgetExceededError):
            await reserve_capped(session_factory, "5.01")
    else:
        await reserve_capped(session_factory, "5.01")
    # 境界ちょうどはどの区分でも通る
    await reserve_capped(session_factory, "5" if counted else "54.99")


async def test_seeded_rows_have_the_intended_cost_state(session_factory):
    """種まきの検証: 上の parametrize が意図した区分を作れていること（テスト自身のバグ防止）。"""
    seen: dict[str, CostState] = {}
    for state in SEEDABLE_STATES:
        rid = await seed(session_factory, "1", state)
        async with session_factory() as session:
            row = await session.get(ProviderReservationRow, uuid.UUID(rid))
            assert row is not None
            seen[state] = cost_state(
                status=ReservationStatus(row.status),
                dispatched=row.dispatched_at is not None,
                has_job_ref=row.provider_job_ref is not None,
                has_raw_output=row.raw_output_key is not None,
                reconciled_by=row.reconciled_by,
            )
    for state in SEEDABLE_STATES:
        if state != "abandoned":
            assert seen[state].value == state
    assert seen["abandoned"] is CostState.UNKNOWN


async def test_both_fal_providers_count_toward_the_one_family_cap(session_factory):
    assert set(FAL_PROVIDERS) == {ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO}
    await seed(session_factory, "30", provider=ProviderCall.FAL_IMAGE)
    await seed(session_factory, "30", provider=ProviderCall.FAL_VIDEO)
    for provider in FAL_PROVIDERS:
        with pytest.raises(ProviderMonthlyBudgetExceededError):
            await reserve_capped(session_factory, "0.01", provider=provider)


async def test_non_fal_providers_are_not_counted(session_factory):
    await seed(session_factory, "500", provider=ProviderCall.CODEX_SCRIPT)
    await reserve_capped(session_factory, "60")  # fal の月間 cap は codex の見積に影響されない


async def test_two_episodes_share_the_single_fal_cap(session_factory):
    """brand/account 列は台帳に無い。アカウント "default" の 1 本の cap を全 Episode が共有。"""
    ep_a, ep_b = await new_episode(session_factory), await new_episode(session_factory)
    await reserve_capped(session_factory, "40", episode_id=ep_a)
    await reserve_capped(session_factory, "20", episode_id=ep_b)
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "0.01", episode_id=ep_a)
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "0.01", episode_id=ep_b)


# ------------------------------------------------------------------ 不明な費用は fail closed


async def test_an_unknown_new_estimate_is_denied_with_unknown_cost(session_factory):
    with pytest.raises(ProviderMonthlyBudgetExceededError) as caught:
        await reserve_capped(session_factory, None)
    assert denial(caught.value)["reason"] == "unknown_cost"
    assert await count_rows(session_factory) == 0


@pytest.mark.parametrize("state", [s for s in SEEDABLE_STATES if s != CostState.NOT_CHARGED.value])
async def test_a_counted_row_with_a_null_estimate_makes_the_month_unknown(session_factory, state):
    await seed(session_factory, None, state)
    with pytest.raises(ProviderMonthlyBudgetExceededError) as caught:
        await reserve_capped(session_factory, "1")
    assert denial(caught.value)["reason"] == "unknown_cost"
    assert await count_rows(session_factory) == 1


async def test_a_not_charged_row_with_a_null_estimate_is_released_not_unknown(session_factory):
    await seed(session_factory, None, CostState.NOT_CHARGED.value)
    await reserve_capped(session_factory, "1")


# ------------------------------------------------------------------ 冪等（再実行を二重計上しない）


async def test_a_replayed_key_is_neither_double_counted_nor_cap_denied(session_factory):
    """exactly-at-cap の予約を同じ idempotency key で再実行しても cap 超過で拒否されない。

    既存の冪等性（一意制約 / find_by_key）に委ね、cap は「本当に新しい行」にだけ効く。
    """
    ep = await new_episode(session_factory)
    first = await reserve_capped(
        session_factory, "60", episode_id=ep, key="same", input_hash="a" * 64
    )
    try:
        again = await reserve_capped(
            session_factory, "60", episode_id=ep, key="same", input_hash="a" * 64
        )
    except IntegrityError:
        pass  # 既存の一意制約で弾かれる実装でもよい。cap の例外でなければよい
    else:
        assert again.id == first.id
    assert await count_rows(session_factory) == 1
    async with session_factory() as session:
        counted = await month_spend_usd(session, ProviderAccount(ProviderFamily.FAL), now=NOW)
    assert counted <= D("60")  # 二重計上されていない


# ------------------------------------------------------------------ 解放（estimate vs reconciled）


async def test_a_reconciled_not_charged_reservation_is_released_from_the_month(session_factory):
    rid = await seed(session_factory, "40", CostState.UNKNOWN.value)  # 曖昧: 安全側で数える
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "30")
    from domain.provider.cost import NOT_ACCEPTED_RECONCILER

    async with session_factory() as session:
        await ProviderReservationRepository(session).mark_spent(
            rid, raw_output_key=None, reconciled_by=NOT_ACCEPTED_RECONCILER
        )
        await session.commit()
    await reserve_capped(session_factory, "30")  # 受理されなかったことが確実 → 枠が戻る


async def test_an_operator_failure_resolution_stays_counted_as_assumed_charged(session_factory):
    """台帳は課金額を持たない。手動で failure と照合しても「課金前提」で確定する既存の意味論
    （resolve_manual は spent）。cap は見積額のまま数える＝安全側。"""
    await seed(session_factory, "40", CostState.ASSUMED_CHARGED.value)
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "30")


async def test_counting_uses_the_estimate_even_when_the_job_is_reconciled_as_charged(
    session_factory,
):
    """実額の列は無い。evidence 付きで確定しても数える額は見積のまま（過大計上は安全側）。"""
    await seed(session_factory, "59.99", CostState.CONFIRMED_CHARGED.value)
    with pytest.raises(ProviderMonthlyBudgetExceededError) as caught:
        await reserve_capped(session_factory, "0.02")
    assert denial(caught.value)["month_counted"] == D("59.99")


# ------------------------------------------------------------------ 月替わり（UTC）


async def test_rows_from_the_previous_utc_month_are_not_counted(session_factory):
    await seed(
        session_factory, "60", reserved_at=datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC)
    )
    await reserve_capped(session_factory, "60", now=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC))


async def test_a_row_at_exactly_the_month_start_is_counted(session_factory):
    await seed(session_factory, "60", reserved_at=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC))
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "0.01", now=datetime(2026, 10, 1, 0, 0, tzinfo=UTC))


async def test_the_month_boundary_is_utc_not_local_time(session_factory):
    """JST 2026-10-01 00:30 は UTC では 9 月 30 日。9 月の行は数え、10 月の行は数えない。"""
    jst = timezone(timedelta(hours=9))
    await seed(session_factory, "60", reserved_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC))
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await reserve_capped(session_factory, "0.01", now=datetime(2026, 10, 1, 0, 30, tzinfo=jst))


async def test_the_december_to_january_rollover_resets_the_count(session_factory):
    await seed(session_factory, "60", reserved_at=datetime(2026, 12, 31, 23, 0, tzinfo=UTC))
    await reserve_capped(session_factory, "60", now=datetime(2027, 1, 1, 0, 0, tzinfo=UTC))


# ------------------------------------------------------------------ resume との同一定義


async def test_provider_credits_month_spend_matches_what_the_gate_counts(session_factory):
    """status / resume の月間費用とゲートの month_counted は同じ定義（単一の情報源）。"""
    for state, est in zip(SEEDABLE_STATES, ("1", "2", "4", "8", "16", "32", "64"), strict=True):
        await seed(session_factory, est, state)
    async with session_factory() as session:
        shown = await month_spend_usd(session, ProviderAccount(ProviderFamily.FAL), now=NOW)
    with pytest.raises(ProviderMonthlyBudgetExceededError) as caught:
        await reserve_capped(session_factory, "1", cap=D("0.0001"))
    assert denial(caught.value)["month_counted"] == shown
    # 見積 1,2,4,8,16,32,64 のうち not_charged(8) だけを除いた全行（estimated も数える）
    assert shown == D("1") + D("2") + D("4") + D("16") + D("32") + D("64")


# ---------------------------------------------------- 原子性の作り（ロック順・SQL 集計）


class _PgLike:
    """dialect が postgresql の session。advisory lock の発行だけ記録し、他は本物へ委譲する。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.locks: list[int] = []
        self.statements: list[str] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, stmt, params=None, *args, **kwargs):
        sql = str(stmt)
        self.statements.append(sql.lower())
        if "pg_advisory_xact_lock" in sql:
            self.locks.append((params or {})["k"])
            return None
        return await self._inner.execute(stmt, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _month_key(now: datetime) -> int:
    material = f"fal-monthly-cap|{now.astimezone(UTC):%Y-%m}"
    return int.from_bytes(hashlib.sha256(material.encode()).digest()[:8], "big", signed=True)


async def test_the_month_lock_is_taken_before_the_input_lock(session):
    episode_id = await new_episode_via(session)
    pg = _PgLike(session)
    await ProviderReservationRepository(pg).reserve(  # type: ignore[arg-type]
        episode_id=episode_id,
        provider=ProviderCall.FAL_IMAGE,
        idempotency_key="k1",
        input_hash="h1",
        round=1,
        scene_id="sb1",
        estimated_cost_usd=D("1"),
        monthly_cap_usd=CAP,
        now=NOW,
    )
    assert pg.locks == [
        _month_key(NOW),
        input_lock_key(episode_id, ProviderCall.FAL_IMAGE, "sb1", "h1"),
    ]  # 固定順: 月 → 入力（デッドロック回避）


async def test_the_month_lock_key_differs_per_month(session):
    episode_id = await new_episode_via(session)
    keys = set()
    for i, month in enumerate((NOW, NOW.replace(month=11))):
        pg = _PgLike(session)
        await ProviderReservationRepository(pg).reserve(  # type: ignore[arg-type]
            episode_id=episode_id,
            provider=ProviderCall.FAL_IMAGE,
            idempotency_key=f"k{i}",
            input_hash=f"h{i}",
            round=1,
            scene_id="sb1",
            estimated_cost_usd=D("1"),
            monthly_cap_usd=CAP,
            now=month,
        )
        keys.add(pg.locks[0])
    assert len(keys) == 2 and all(-(2**63) <= k < 2**63 for k in keys)


async def test_without_a_cap_the_month_lock_is_not_taken(session):
    """cap 無しの back-compat 経路は従来通りロック 1 本（既存の locking test と整合）。"""
    episode_id = await new_episode_via(session)
    pg = _PgLike(session)
    await ProviderReservationRepository(pg).reserve(  # type: ignore[arg-type]
        episode_id=episode_id,
        provider=ProviderCall.FAL_IMAGE,
        idempotency_key="k1",
        input_hash="h1",
        round=1,
        scene_id="sb1",
    )
    assert len(pg.locks) == 1


async def test_the_month_is_summed_in_sql_not_in_python(session):
    episode_id = await new_episode_via(session)
    pg = _PgLike(session)
    await ProviderReservationRepository(pg).reserve(  # type: ignore[arg-type]
        episode_id=episode_id,
        provider=ProviderCall.FAL_IMAGE,
        idempotency_key="k1",
        input_hash="h1",
        round=1,
        scene_id="sb1",
        estimated_cost_usd=D("1"),
        monthly_cap_usd=CAP,
        now=NOW,
    )
    assert any("sum(" in s for s in pg.statements)


async def test_the_decision_function_is_the_one_in_the_domain(session_factory):
    """reserve の判定は domain.budget_gate.evaluate_cap と同じ結論（境界の取り違えを防ぐ）。"""
    await seed(session_factory, "55")
    for est in ("5", "5.0001"):
        expected = evaluate_cap(CAP, D("55"), D(est)).allowed
        try:
            await reserve_capped(session_factory, est)
            assert expected
        except ProviderMonthlyBudgetExceededError:
            assert not expected


async def new_episode_via(session) -> str:
    from infrastructure.db.repositories import EpisodeRepository

    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return str(episode.id)
