"""provider アカウントの breaker と保留の永続化（ADR-0057 / INV-72 / INV-73）。

``contracts.provider_health`` の ``ProviderHealthReader`` / ``CreditHoldWriter`` の実装。
どの操作も**アプリ側のロックを作らない**: 並行安全は一意制約と、``UPDATE ... WHERE state = ...``
の条件付き更新（行数で勝敗が決まる）だけに頼る。方言中立（PostgreSQL / SQLite）。

breaker は時間では閉じない。``half_open`` / ``closed`` への遷移は承認つきのメソッドだけ。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import distinct, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.provider_health import (
    PROVIDER_FAMILY,
    BreakerSnapshot,
    BreakerState,
    HoldReason,
    ProviderAccount,
    RefusalKind,
)
from contracts.states import ArtifactType
from infrastructure.db.models import (
    ArtifactMetadataRow,
    ProviderBreakerRow,
    ProviderCreditHoldRow,
    ProviderReservationRow,
)

#: 保留の写しに残す成功済み成果物の型（再開で再利用される単位）
_SCENE_OUTPUT_TYPES = (
    ArtifactType.SCENE_IMAGE.value,
    ArtifactType.SCENE_VIDEO.value,
    ArtifactType.SCENE_VOICE.value,
)


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


@dataclass(frozen=True, slots=True)
class BreakerDetail:
    """CLI の status 用。``BreakerSnapshot`` より詳しい（reason / evidence / 件数）。"""

    snapshot: BreakerSnapshot
    http_status: int | None
    evidence: str | None
    refusal_count: int
    approval_fingerprint: str | None
    approved_by: str | None


@dataclass(frozen=True, slots=True)
class CreditHold:
    id: str
    episode_id: str
    scene_id: str | None
    account_key: str
    reason: HoldReason
    resumable_stage: str
    generation_profile_id: str | None
    snapshot: dict
    held_at: datetime
    released_at: datetime | None


def _to_hold(row: ProviderCreditHoldRow) -> CreditHold:
    return CreditHold(
        id=str(row.id),
        episode_id=str(row.episode_id),
        scene_id=row.scene_id,
        account_key=row.account_key,
        reason=HoldReason(row.reason),
        resumable_stage=row.resumable_stage,
        generation_profile_id=row.generation_profile_id,
        snapshot=dict(row.snapshot or {}),
        held_at=row.held_at,
        released_at=row.released_at,
    )


def _closed(account: ProviderAccount) -> BreakerSnapshot:
    return BreakerSnapshot(
        account_key=account.key, state=BreakerState.CLOSED, reason=None, opened_at=None
    )


class PostgresProviderHealth:
    """``ProviderHealthReader`` + ``CreditHoldWriter`` + 承認つき遷移。名前は歴史的（方言中立）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------- breaker: read

    async def snapshot(self, account: ProviderAccount) -> BreakerSnapshot:
        detail = await self.detail(account)
        return detail.snapshot if detail is not None else _closed(account)

    async def detail(self, account: ProviderAccount) -> BreakerDetail | None:
        async with self._session_factory() as session:
            row = await session.get(ProviderBreakerRow, account.key)
            if row is None:
                return None
            state = BreakerState(row.state)
            snapshot = BreakerSnapshot(
                account_key=row.account_key,
                state=state,
                reason=RefusalKind(row.reason)
                if row.reason and state is not BreakerState.CLOSED
                else None,
                opened_at=row.opened_at if state is not BreakerState.CLOSED else None,
            )
            return BreakerDetail(
                snapshot=snapshot,
                http_status=row.http_status,
                evidence=row.evidence,
                refusal_count=row.refusal_count,
                approval_fingerprint=row.approval_fingerprint,
                approved_by=row.approved_by,
            )

    # ------------------------------------------------------------ breaker: open

    async def open_breaker(
        self,
        account: ProviderAccount,
        *,
        reason: RefusalKind,
        http_status: int | None,
        evidence: str | None,
        episode_id: uuid.UUID | str | None,
        now: datetime | None = None,
    ) -> bool:
        """breaker を開く。**冪等・並行安全**。この呼び出しが開いたら True（既に open なら False）。

        行が無ければ INSERT（一意制約が同時の2件の片方を落とす）。あれば ``closed`` /
        ``half_open`` のときだけ条件付きで ``open`` にする。既に ``open`` なら観測した拒否の数と
        最後の時刻だけを原子的に進める（理由・開いた時刻は最初のものを保つ）。
        """
        moment = now or datetime.now(UTC)
        ep = _as_uuid(episode_id) if episode_id is not None else None
        async with self._session_factory() as session:
            try:
                async with session.begin_nested():
                    session.add(
                        ProviderBreakerRow(
                            account_key=account.key,
                            state=BreakerState.OPEN.value,
                            reason=reason.value,
                            http_status=http_status,
                            evidence=evidence,
                            opened_at=moment,
                            opened_by_episode_id=ep,
                            refusal_count=1,
                            last_refusal_at=moment,
                            state_changed_at=moment,
                        )
                    )
                await session.commit()
                return True
            except IntegrityError:
                pass  # 行は既にある。下の条件付き更新で決める
            reopened = await session.execute(
                update(ProviderBreakerRow)
                .where(
                    ProviderBreakerRow.account_key == account.key,
                    ProviderBreakerRow.state.in_(
                        (BreakerState.CLOSED.value, BreakerState.HALF_OPEN.value)
                    ),
                )
                .values(
                    state=BreakerState.OPEN.value,
                    reason=reason.value,
                    http_status=http_status,
                    evidence=evidence,
                    opened_at=moment,
                    opened_by_episode_id=ep,
                    refusal_count=ProviderBreakerRow.refusal_count + 1,
                    last_refusal_at=moment,
                    state_changed_at=moment,
                )
            )
            if reopened.rowcount:  # type: ignore[attr-defined]
                await session.commit()
                return True
            await session.execute(
                update(ProviderBreakerRow)
                .where(ProviderBreakerRow.account_key == account.key)
                .values(refusal_count=ProviderBreakerRow.refusal_count + 1, last_refusal_at=moment)
            )
            await session.commit()
            return False

    # ------------------------------------------------- breaker: approved transitions

    async def _transition(
        self,
        account: ProviderAccount,
        *,
        from_states: tuple[BreakerState, ...],
        to_state: BreakerState,
        approval_fingerprint: str | None,
        approved_by: str | None,
        approval_evidence: str | None,
        now: datetime | None,
    ) -> bool:
        moment = now or datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                update(ProviderBreakerRow)
                .where(
                    ProviderBreakerRow.account_key == account.key,
                    ProviderBreakerRow.state.in_([s.value for s in from_states]),
                )
                .values(
                    state=to_state.value,
                    state_changed_at=moment,
                    approval_fingerprint=approval_fingerprint,
                    approved_by=approved_by,
                    approval_evidence=approval_evidence,
                )
            )
            await session.commit()
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def half_open(
        self,
        account: ProviderAccount,
        *,
        approval_fingerprint: str,
        approved_by: str,
        approval_evidence: str,
        now: datetime | None = None,
    ) -> bool:
        """所有者の承認の下で ``open`` → ``half_open``（上限つき再開を許す）。"""
        return await self._transition(
            account,
            from_states=(BreakerState.OPEN,),
            to_state=BreakerState.HALF_OPEN,
            approval_fingerprint=approval_fingerprint,
            approved_by=approved_by,
            approval_evidence=approval_evidence,
            now=now,
        )

    async def close(
        self,
        account: ProviderAccount,
        *,
        approval_fingerprint: str,
        approved_by: str,
        approval_evidence: str,
        now: datetime | None = None,
    ) -> bool:
        """所有者の承認の下で ``open`` / ``half_open`` → ``closed``。"""
        return await self._transition(
            account,
            from_states=(BreakerState.OPEN, BreakerState.HALF_OPEN),
            to_state=BreakerState.CLOSED,
            approval_fingerprint=approval_fingerprint,
            approved_by=approved_by,
            approval_evidence=approval_evidence,
            now=now,
        )

    async def record_accepted(
        self, account: ProviderAccount, *, now: datetime | None = None
    ) -> bool:
        """``half_open`` 中に provider が submit を受理した: 承認済み再開が通った証拠なので閉じる。

        時間では閉じない（INV-72）。``open`` は閉じない（承認が先）。
        """
        return await self._transition(
            account,
            from_states=(BreakerState.HALF_OPEN,),
            to_state=BreakerState.CLOSED,
            approval_fingerprint=None,
            approved_by=None,
            approval_evidence=None,
            now=now,
        )

    # ---------------------------------------------------------------- holds: write

    async def hold_episode(
        self,
        *,
        episode_id: str,
        scene_id: str | None,
        account: ProviderAccount,
        reason: HoldReason,
        resumable_stage: str,
        generation_profile_id: str | None,
    ) -> None:
        """保留を記録する（``CreditHoldWriter``）。

        同じ (episode, scene, account) の未解放があれば何もしない。
        """
        ep = _as_uuid(episode_id)
        async with self._session_factory() as session:
            snapshot = await self._build_snapshot(session, ep, account)
            try:
                async with session.begin_nested():
                    session.add(
                        ProviderCreditHoldRow(
                            id=uuid.uuid4(),
                            episode_id=ep,
                            scene_id=scene_id,
                            scene_key=scene_id or "",
                            account_key=account.key,
                            reason=reason.value,
                            resumable_stage=resumable_stage,
                            generation_profile_id=generation_profile_id,
                            snapshot=snapshot,
                            held_at=datetime.now(UTC),
                        )
                    )
                await session.commit()
            except IntegrityError:
                await session.rollback()

    @staticmethod
    async def _build_snapshot(
        session: AsyncSession, episode_id: uuid.UUID, account: ProviderAccount
    ) -> dict:
        providers = [p.value for p, family in PROVIDER_FAMILY.items() if family is account.family]
        reservations = (
            await session.scalars(
                select(ProviderReservationRow.id)
                .where(
                    ProviderReservationRow.episode_id == episode_id,
                    ProviderReservationRow.provider.in_(providers),
                )
                .order_by(ProviderReservationRow.reserved_at, ProviderReservationRow.id)
            )
        ).all()
        artifacts = (
            await session.scalars(
                select(ArtifactMetadataRow.id)
                .where(
                    ArtifactMetadataRow.episode_id == episode_id,
                    ArtifactMetadataRow.artifact_type.in_(_SCENE_OUTPUT_TYPES),
                    ArtifactMetadataRow.superseded_at.is_(None),
                )
                .order_by(ArtifactMetadataRow.created_at, ArtifactMetadataRow.id)
            )
        ).all()
        return {
            "reservation_ids": [str(r) for r in reservations],
            "succeeded_artifact_ids": [str(a) for a in artifacts],
        }

    async def release_holds(
        self,
        episode_id: uuid.UUID | str,
        *,
        account: ProviderAccount,
        approval_fingerprint: str,
        now: datetime | None = None,
    ) -> int:
        """Episode の未解放の保留をすべて解放する（再開の承認つき）。解放した行数。"""
        moment = now or datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                update(ProviderCreditHoldRow)
                .where(
                    ProviderCreditHoldRow.episode_id == _as_uuid(episode_id),
                    ProviderCreditHoldRow.account_key == account.key,
                    ProviderCreditHoldRow.released_at.is_(None),
                )
                .values(released_at=moment, released_approval_fingerprint=approval_fingerprint)
            )
            await session.commit()
            return int(result.rowcount)  # type: ignore[attr-defined]

    # ----------------------------------------------------------------- holds: read

    async def open_hold_count(self) -> int:
        """未解放の保留を持つ Episode の数（``ProviderHealthReader``）。"""
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(distinct(ProviderCreditHoldRow.episode_id))).where(
                    ProviderCreditHoldRow.released_at.is_(None)
                )
            )
            return int(count or 0)

    async def list_open_holds(self, *, account: ProviderAccount | None = None) -> list[CreditHold]:
        async with self._session_factory() as session:
            stmt = select(ProviderCreditHoldRow).where(ProviderCreditHoldRow.released_at.is_(None))
            if account is not None:
                stmt = stmt.where(ProviderCreditHoldRow.account_key == account.key)
            rows = await session.scalars(
                stmt.order_by(ProviderCreditHoldRow.held_at, ProviderCreditHoldRow.id)
            )
            return [_to_hold(r) for r in rows]

    async def held_episode_ids_oldest_first(
        self, *, account: ProviderAccount, limit: int
    ) -> Sequence[str]:
        """未解放の保留を持つ Episode を、最初に保留した順に ``limit`` 件。"""
        async with self._session_factory() as session:
            rows = await session.execute(
                select(ProviderCreditHoldRow.episode_id, func.min(ProviderCreditHoldRow.held_at))
                .where(
                    ProviderCreditHoldRow.released_at.is_(None),
                    ProviderCreditHoldRow.account_key == account.key,
                )
                .group_by(ProviderCreditHoldRow.episode_id)
                .order_by(func.min(ProviderCreditHoldRow.held_at), ProviderCreditHoldRow.episode_id)
                .limit(limit)
            )
            return [str(r[0]) for r in rows]


__all__ = ["BreakerDetail", "CreditHold", "PostgresProviderHealth"]
