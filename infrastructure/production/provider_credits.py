"""残高不足の保留の状態確認・breaker の承認つき遷移・保留 Episode の再開（ADR-0057 / INV-73）。

``scripts/provider-credits.py`` の本体。provider（fal）へは**接続しない**: 残高を読む公式の
無課金・読み取り専用 endpoint が無いので、入金の確認は所有者の attestation
（``--evidence`` と構造化された承認 token）で行う。再開は新しい workflow を作らず、既存の
Episode 再開 API（``POST /episodes/{id}/resume`` = ``build_resume_plan`` → pipeline workflow）を
呼ぶだけ（``ResumeClient``）。

再開を**拒否する**条件（どれか1つでも）:

1. breaker が ``open``（先に承認つきで ``half_open`` にする）
2. 対象が `RESUME_MAX_CONCURRENCY` を超える（再開済みで実行中の Episode も数える）
3. 対象に未解放の保留が無い / Episode が ``blocked`` でない
4. 対象に未照合の予約がある（``reserved`` で evidence なし。dispatched で provider 参照がある
   ものも含む。自動で再送しない / INV-15・INV-67）。既存の再開計画も同じ述語で拒否する
5. 当月の費用（台帳の見積額。受理されなかった拒否は 0。月間ハードキャップと同じ定義 / ADR-0063）
   + 追加の見積が予算の上限を超える。
   上限が未設定なら拒否（課金を伴う再開を上限なしで通さない）
6. 承認 token（``OWNER-<yyyymmdd>-<id>``。認証ではない）または根拠（evidence）が無い

dry-run が既定。``apply`` でだけ書く。承認 token は指紋だけを保存する。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.pipeline import PipelineStage
from contracts.provider_health import (
    HELD_EPISODE_STATUSES,
    RESUME_MAX_CONCURRENCY,
    BreakerState,
    ProviderAccount,
    ProviderFamily,
)
from contracts.states import EpisodeStatus, ProviderCall, ReservationStatus
from domain.budget_gate import month_start_utc
from domain.pipeline.resume_plan import ReservationSummary, build_resume_plan
from domain.provider.reconciliation import is_owner_approval_reference
from infrastructure.db.models import EpisodeRow, ProviderReservationRow
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import (
    EpisodeRepository,
    ProviderReservationRepository,
    month_counted,
)

DEFAULT_ACCOUNT = ProviderAccount(ProviderFamily.FAL)

#: 保留中の Episode の2つの形（ADR-0057）。どちらも既存の再開 API が ``production`` から始める:
#: - ``blocked``: production の最中に残高拒否 / breaker open で止まった
#: - ``storyboard_ready``: 日次の入口ゲートが production の前で保留した（``storyboard_ready →
#:   blocked`` は遷移表に無いので状態は変わらず、``scene_id=NULL`` の保留行だけが残る / ADR-0058）
RESUMABLE_HELD_STATUSES = HELD_EPISODE_STATUSES


class CreditsRefused(Exception):
    """承認・予算・未照合予約などの条件を満たさず、操作を拒否した。理由は人が読む文。"""


@dataclass(frozen=True, slots=True)
class Approval:
    fingerprint: str
    actor: str
    evidence: str


def validate_approval(*, owner_approval: str, evidence: str, actor: str) -> Approval:
    """所有者承認 token（構造化・指紋だけ保存）と、入金確認の根拠（attestation）を要求する。"""
    token = owner_approval.strip()
    if not token:
        raise CreditsRefused("--owner-approval is required (OWNER-<yyyymmdd>-<id>)")
    if not is_owner_approval_reference(token):
        raise CreditsRefused(
            "--owner-approval must be an owner approval reference like OWNER-<yyyymmdd>-<id>"
        )
    if not evidence.strip():
        raise CreditsRefused(
            "--evidence is required: the owner's confirmation that the provider balance is "
            "topped up (there is no free read-only balance endpoint to check automatically)"
        )
    if not actor.strip():
        raise CreditsRefused("--actor is required")
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16]
    return Approval(fingerprint, actor.strip(), evidence.strip())


@dataclass(frozen=True, slots=True)
class ResumeStarted:
    workflow_id: str
    target_stage: str


class ResumeRejected(Exception):
    """既存の再開経路が拒否した（409 など）。"""


class ResumeClient(Protocol):
    async def resume(self, episode_id: str) -> ResumeStarted: ...


class ApiResumeClient:
    """既存の ``POST /episodes/{id}/resume`` を呼ぶ。新しい workflow 起動経路は持たない。"""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._transport = transport

    async def resume(self, episode_id: str) -> ResumeStarted:
        async with httpx.AsyncClient(transport=self._transport, timeout=30.0) as client:
            response = await client.post(f"{self._base}/episodes/{episode_id}/resume")
        if response.status_code != 202:
            raise ResumeRejected(
                f"resume refused: HTTP {response.status_code}: {response.text[:300]}"
            )
        body = response.json()
        return ResumeStarted(str(body.get("workflow_id", "")), str(body.get("target_stage", "")))


#: 月初の境界は ``domain.budget_gate.month_start_utc`` の 1 箇所（上限検査と共有 / ADR-0063）
month_start = month_start_utc


async def month_spend_usd(
    session: AsyncSession, account: ProviderAccount, *, now: datetime
) -> Decimal:
    """当月（UTC）にこのアカウントで数える費用。月間ハードキャップと**同じ定義**（ADR-0063）。

    ``infrastructure.db.repositories.month_counted`` を呼ぶだけ（定義を 2 箇所に書かない）。
    受理されなかったと確定した拒否だけ 0、それ以外は見積額（未 dispatch の予約・曖昧を含む）。
    """
    return (await month_counted(session, now=now, family=account.family)).total


async def unreconciled_for_episode(
    session: AsyncSession, episode_id: str
) -> list[ProviderReservationRow]:
    """evidence の無い ``reserved``（dispatched / provider 参照ありを含む）。resume を止める。"""
    rows = await session.scalars(
        select(ProviderReservationRow).where(
            ProviderReservationRow.episode_id == uuid.UUID(episode_id),
            ProviderReservationRow.status == ReservationStatus.RESERVED.value,
            ProviderReservationRow.raw_output_key.is_(None),
            # 呼んでいない予約（dispatched_at なし）は未照合ではなく、そのまま進められる
            ProviderReservationRow.dispatched_at.is_not(None),
        )
    )
    return list(rows)


# --------------------------------------------------------------------------- status


async def status(
    session_factory: async_sessionmaker[AsyncSession],
    health: PostgresProviderHealth,
    *,
    account: ProviderAccount = DEFAULT_ACCOUNT,
    budget_cap: Decimal | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """読み取りだけ（何も書かない・provider に接続しない）。"""
    moment = now or datetime.now(UTC)
    detail = await health.detail(account)
    holds = await health.list_holds(account=account)
    held: dict[str, dict[str, Any]] = {}
    for hold in holds:
        entry = held.setdefault(
            hold.episode_id,
            {"episode_id": hold.episode_id, "held_at": hold.held_at, "scenes": [], "reasons": []},
        )
        entry["scenes"].append(hold.scene_id)
        if hold.reason.value not in entry["reasons"]:
            entry["reasons"].append(hold.reason.value)
    async with session_factory() as session:
        spend = await month_spend_usd(session, account, now=moment)
        for episode_id, entry in held.items():
            entry["unreconciled_reservation_ids"] = [
                str(r.id) for r in await unreconciled_for_episode(session, episode_id)
            ]
        await session.rollback()
    snapshot = detail.snapshot if detail is not None else await health.snapshot(account)
    return {
        "command": "status",
        "account": account.key,
        "breaker": {
            "state": snapshot.state.value,
            "reason": snapshot.reason.value if snapshot.reason else None,
            "opened_at": snapshot.opened_at,
            "http_status": detail.http_status if detail else None,
            "evidence": detail.evidence if detail else None,
            "refusal_count": detail.refusal_count if detail else 0,
            "approved_by": detail.approved_by if detail else None,
        },
        "held_episodes": list(held.values()),
        "month_spend_usd": str(spend),
        "monthly_budget_usd": None if budget_cap is None else str(budget_cap),
        "resume_max_concurrency": RESUME_MAX_CONCURRENCY,
    }


# ------------------------------------------------------------------ breaker transitions


async def set_breaker(
    health: PostgresProviderHealth,
    *,
    to: str,
    approval: Approval,
    apply: bool,
    account: ProviderAccount = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """所有者承認つきの ``half-open`` / ``close``。既定は dry-run。"""
    if to not in ("half-open", "close"):
        raise CreditsRefused(f"unknown transition {to!r}")
    snapshot = await health.snapshot(account)
    allowed = (
        (BreakerState.OPEN,) if to == "half-open" else (BreakerState.OPEN, BreakerState.HALF_OPEN)
    )
    if snapshot.state not in allowed:
        raise CreditsRefused(f"breaker is {snapshot.state.value}; nothing to {to}")
    result: dict[str, Any] = {
        "command": to,
        "dry_run": not apply,
        "account": account.key,
        "from": snapshot.state.value,
        "approval_fingerprint": approval.fingerprint,
    }
    if not apply:
        return result
    transition = health.half_open if to == "half-open" else health.close
    changed = await transition(
        account,
        approval_fingerprint=approval.fingerprint,
        approved_by=approval.actor,
        approval_evidence=approval.evidence,
    )
    result["changed"] = changed
    return result


# ------------------------------------------------------------------------------ resume


async def _active_resumed_episodes(session: AsyncSession, health: PostgresProviderHealth) -> int:
    """保留を解放して再開した後、まだ ``in_progress`` の Episode の数（同時再開の上限に数える）。"""
    from infrastructure.db.models import ProviderCreditHoldRow

    released = (
        await session.scalars(
            select(ProviderCreditHoldRow.episode_id)
            .where(ProviderCreditHoldRow.released_at.is_not(None))
            .distinct()
        )
    ).all()
    if not released:
        return 0
    running = await session.scalars(
        select(EpisodeRow.id).where(
            EpisodeRow.id.in_(list(released)), EpisodeRow.status == EpisodeStatus.IN_PROGRESS.value
        )
    )
    return len(running.all())


async def resume_episodes(
    session_factory: async_sessionmaker[AsyncSession],
    health: PostgresProviderHealth,
    client: ResumeClient | None,
    *,
    episode_ids: Sequence[str] = (),
    oldest: int | None = None,
    approval: Approval,
    estimated_additional_usd: Decimal,
    budget_cap: Decimal | None,
    apply: bool,
    account: ProviderAccount = DEFAULT_ACCOUNT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """保留中の Episode を再開する。条件を満たさなければ ``CreditsRefused``（何も書かない）。"""
    moment = now or datetime.now(UTC)
    if bool(episode_ids) == (oldest is not None):
        raise CreditsRefused(
            "give either explicit --episode-id values or --oldest N, not both/none"
        )
    snapshot = await health.snapshot(account)
    if snapshot.state is BreakerState.OPEN:
        raise CreditsRefused(
            "breaker is open: approve half-open first (owner approval + balance evidence)"
        )
    if oldest is not None:
        if oldest < 1:
            raise CreditsRefused("--oldest must be >= 1")
        targets = list(await health.held_episode_ids_oldest_first(account=account, limit=oldest))
    else:
        targets = list(dict.fromkeys(str(e) for e in episode_ids))
    if not targets:
        raise CreditsRefused("no held episodes to resume")

    async with session_factory() as session:
        active = await _active_resumed_episodes(session, health)
        if len(targets) + active > RESUME_MAX_CONCURRENCY:
            raise CreditsRefused(
                f"resume concurrency is capped at {RESUME_MAX_CONCURRENCY} "
                f"(requested {len(targets)}, already running {active})"
            )
        open_holds = {h.episode_id for h in await health.list_holds(account=account)}
        plans: list[dict[str, Any]] = []
        for episode_id in targets:
            if episode_id not in open_holds:
                raise CreditsRefused(f"episode {episode_id} has no open credit hold")
            episode = await EpisodeRepository(session).get(uuid.UUID(episode_id))
            if episode is None or episode.status not in RESUMABLE_HELD_STATUSES:
                raise CreditsRefused(
                    f"episode {episode_id} is not blocked (production-time hold) or "
                    "storyboard_ready (daily-gate hold before production)"
                )
            pending = await unreconciled_for_episode(session, episode_id)
            if pending:
                raise CreditsRefused(
                    f"episode {episode_id} has {len(pending)} unreconciled reservation(s) "
                    f"({', '.join(str(r.id) for r in pending)}): reconcile them first "
                    "(scripts/reconcile-reservations.py); they are never auto-resent"
                )
            plan = await _resume_plan(session, episode_id, episode.status)
            if not plan.resumable or plan.target_stage != PipelineStage.PRODUCTION.value:
                raise CreditsRefused(
                    f"episode {episode_id} is not resumable at production: "
                    f"{plan.reason or plan.target_stage}"
                )
            plans.append({"episode_id": episode_id, "target_stage": plan.target_stage})
        spend = await month_spend_usd(session, account, now=moment)
        await session.rollback()

    if budget_cap is None:
        raise CreditsRefused(
            "no monthly budget cap configured (FAL_MONTHLY_BUDGET_USD): refusing a resume that "
            "can create new charges"
        )
    if estimated_additional_usd < 0:
        raise CreditsRefused("--estimated-additional-usd must be >= 0")
    if spend + estimated_additional_usd > budget_cap:
        raise CreditsRefused(
            f"monthly budget would be exceeded: spent {spend} + "
            f"estimated {estimated_additional_usd} "
            f"> cap {budget_cap}"
        )

    result: dict[str, Any] = {
        "command": "resume",
        "dry_run": not apply,
        "account": account.key,
        "episodes": plans,
        "month_spend_usd": str(spend),
        "estimated_additional_usd": str(estimated_additional_usd),
        "monthly_budget_usd": str(budget_cap),
        "approval_fingerprint": approval.fingerprint,
    }
    if not apply:
        return result
    if client is None:
        raise CreditsRefused("--api-url is required with --apply (the existing resume API is used)")

    started: list[dict[str, str]] = []
    for episode_id in targets:
        # 先に解放する: 再開した workflow が直ちに再び止まったとき、保留を新しく記録できるように
        # （未解放の行が残っていると冪等で何も書かれず、保留の無い blocked になる）
        held = [h for h in await health.list_holds(account=account) if h.episode_id == episode_id]
        await health.release_holds(
            episode_id, account=account, approval_fingerprint=approval.fingerprint
        )
        try:
            outcome = await client.resume(episode_id)
        except Exception:
            for hold in held:  # 再開できなかった: 保留を元に戻す
                await health.hold_episode(
                    episode_id=hold.episode_id,
                    scene_id=hold.scene_id,
                    account=account,
                    reason=hold.reason,
                    resumable_stage=hold.resumable_stage,
                    generation_profile_id=hold.generation_profile_id,
                )
            raise
        started.append({"episode_id": episode_id, "workflow_id": outcome.workflow_id})
    result["started"] = started
    return result


async def _resume_plan(session: AsyncSession, episode_id: str, status_: EpisodeStatus):
    """API の ``_load_resume_plan`` と同じ入力から既存の純粋関数で計画を組む（読み取り専用）。"""
    owner = await EpisodeRepository(session).get_workflow_id(uuid.UUID(episode_id))
    reservations: list[ReservationSummary] = []
    repo = ProviderReservationRepository(session)
    for provider in ProviderCall:
        for row in await repo.list_for_episode_provider(episode_id, provider):
            reservations.append(
                ReservationSummary(
                    id=row.id,
                    provider=row.provider,
                    status=row.status,
                    raw_output_key=row.raw_output_key,
                )
            )
    return build_resume_plan(
        episode_id=episode_id,
        status=status_,
        owner_workflow_id=owner,
        unreconciled_reservations=reservations,
    )


__all__ = [
    "DEFAULT_ACCOUNT",
    "ApiResumeClient",
    "Approval",
    "CreditsRefused",
    "ResumeClient",
    "ResumeRejected",
    "ResumeStarted",
    "month_spend_usd",
    "resume_episodes",
    "set_breaker",
    "status",
    "unreconciled_for_episode",
    "validate_approval",
]
