"""Pipeline Activity（ADR-0023）。状態の読み書きだけ。次の工程を決めない（INV-4）。

- ``check_paused``: PAUSED（env OR DB）。``include_uploads`` なら UPLOADS_PAUSED も
- ``claim_daily_slot``: 日次枠の claim を commit する（trigger id で冪等 / ADR-0021）
- ``upload_gate``: 自動投稿してよいか。拒否は理由を返す（例外にしない）
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.operations import OperationalSwitch
from contracts.pipeline import (
    PIPELINE_CHECK_PAUSED,
    PIPELINE_CLAIM_DAILY_SLOT,
    PIPELINE_UPLOAD_GATE,
    CheckPausedRequest,
    CheckPausedResult,
    ClaimDailySlotRequest,
    ClaimDailySlotResult,
    UploadGateRequest,
    UploadGateResult,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    DailyEpisodeSlotRepository,
    EpisodeRepository,
    OperationalSwitchRepository,
    ProviderReservationRepository,
)


class PipelineActivities:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        paused_env: bool,
        uploads_paused_env: bool,
    ) -> None:
        self._session_factory = session_factory
        self._paused_env = paused_env
        self._uploads_paused_env = uploads_paused_env

    def activities(self) -> Sequence[Callable[..., object]]:
        return [self.check_paused, self.claim_daily_slot, self.upload_gate]

    async def _paused_reason(self, session: AsyncSession, *, include_uploads: bool) -> str | None:
        switches = OperationalSwitchRepository(session)
        if self._paused_env:
            return "PAUSED (env)"
        if await switches.is_on(OperationalSwitch.PAUSED):
            return "paused (db switch)"
        if include_uploads:
            if self._uploads_paused_env:
                return "UPLOADS_PAUSED (env)"
            if await switches.is_on(OperationalSwitch.UPLOADS_PAUSED):
                return "uploads_paused (db switch)"
        return None

    @activity.defn(name=PIPELINE_CHECK_PAUSED)
    async def check_paused(self, request: CheckPausedRequest) -> CheckPausedResult:
        async with self._session_factory() as session:
            reason = await self._paused_reason(session, include_uploads=request.include_uploads)
        return CheckPausedResult(paused=reason is not None, reason=reason)

    @activity.defn(name=PIPELINE_CLAIM_DAILY_SLOT)
    async def claim_daily_slot(self, request: ClaimDailySlotRequest) -> ClaimDailySlotResult:
        async with self._session_factory() as session:
            claim = await DailyEpisodeSlotRepository(session).claim(
                slot_date=date.fromisoformat(request.slot_date),
                trigger_id=request.trigger_id,
                daily_limit=request.daily_limit,
                topic=request.topic,
                topic_plan_id=request.topic_plan_id,
            )
            # 返す Episode に結び付いている plan（ADR-0025）。None なら pipeline は始めない
            episode = (
                await EpisodeRepository(session).get(claim.episode_id)
                if claim.episode_id is not None
                else None
            )
            # commit 後に応答を失っても、再試行は同じ trigger_id で EXISTING を引く
            await session.commit()
        return ClaimDailySlotResult(
            outcome=claim.outcome.value,
            episode_id=claim.episode_id,
            topic_plan_id=episode.topic_plan_id if episode is not None else None,
        )

    @activity.defn(name=PIPELINE_UPLOAD_GATE)
    async def upload_gate(self, request: UploadGateRequest) -> UploadGateResult:
        ep = request.episode_id
        async with self._session_factory() as session:
            episode = await EpisodeRepository(session).get(ep)
            status = episode.status.value if episode is not None else ""
            reason = await self._paused_reason(session, include_uploads=True)
            if reason is None:
                reason = await self._refusal(session, ep, episode is not None, status)
        return UploadGateResult(allowed=reason is None, reason=reason, status=status)

    async def _refusal(
        self, session: AsyncSession, ep: str, exists: bool, status: str
    ) -> str | None:
        if not exists:
            return "episode not found"
        if status != EpisodeStatus.RENDER_READY:
            return f"episode is {status}, not render_ready"
        final = await ArtifactMetadataRepository(session).find_current_by_type(
            ep, ArtifactType.FINAL_VIDEO
        )
        if final is None:
            return "no current final_video artifact"
        reservations = await ProviderReservationRepository(session).list_for_episode_provider(
            ep, ProviderCall.YOUTUBE_UPLOAD
        )
        for r in reservations:
            if r.status is ReservationStatus.SPENT:
                return f"upload reservation {r.id} is spent (already uploaded?)"
            if r.status is ReservationStatus.RESERVED and r.dispatched_at is not None:
                return f"upload reservation {r.id} is dispatched (outcome unknown)"
        return None
