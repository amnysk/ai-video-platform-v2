"""Pipeline Activity（ADR-0023）。状態の読み書きだけ。次の工程を決めない（INV-4）。

- ``check_paused``: PAUSED（env OR DB）。``include_uploads`` なら UPLOADS_PAUSED も
- ``claim_daily_slot``: 日次枠の claim を commit する（trigger id で冪等 / ADR-0021）
- ``upload_gate``: 自動投稿してよいか。拒否は理由を返す（例外にしない）
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client

from contracts.daily_gate import (
    PIPELINE_DAILY_GATE,
    PIPELINE_PRODUCTION_GATE,
    DailyGateRequest,
    DailyGateResult,
    ProductionGateRequest,
    ProductionGateResult,
)
from contracts.log_contract import EventName, LogStage, Outcome
from contracts.operations import OperationalSwitch
from contracts.pipeline import (
    PIPELINE_CHECK_PAUSED,
    PIPELINE_CLAIM_DAILY_SLOT,
    PIPELINE_UPLOAD_GATE,
    CheckPausedRequest,
    CheckPausedResult,
    ClaimDailySlotRequest,
    ClaimDailySlotResult,
    PipelineStage,
    UploadGateRequest,
    UploadGateResult,
)
from contracts.provider_health import MAX_HELD_EPISODES, BreakerState
from contracts.schedule_guard import WATCHDOG_CHECK_ACTIVITY, WatchdogCheckRequest, WatchdogResult
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from domain.budget_gate import BudgetDecision
from domain.daily_gate import decide_daily_mode, hold_reason_for, stage_accounts
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    DailyEpisodeSlotRepository,
    EpisodeRepository,
    OperationalSwitchRepository,
    ProviderReservationRepository,
)
from infrastructure.logging.emit import emit, log_guard
from infrastructure.observability.anomaly_notifier import LoggingAnomalyNotifier
from infrastructure.temporal.daily_runs import TemporalDailyRunReader
from infrastructure.temporal.schedules import TemporalScheduleControl
from infrastructure.temporal.watchdog import (
    TemporalPipelineOutcomeChecker,
    TemporalWorkflowStartCounter,
    run_daily_watchdog,
)
from workers.pipeline.gate_ports import GatePorts

logger = logging.getLogger(__name__)


_SEVERITY_RANK: dict[BreakerState, int] = {
    BreakerState.CLOSED: 0,
    BreakerState.HALF_OPEN: 1,
    BreakerState.OPEN: 2,
}


class PipelineActivities:
    #: 通知の唯一の差し替え口（ADR-0027 §運用）。既定はログのみ。Slack / メール等へ
    #: 差し替えるときはこの1箇所を変える（AGENTS.md §8）。``scripts/schedule-guard.py status``
    #: がこの属性を直接読んで「通知未設定」を診断する（ADR-0031 §3。新しい設定フラグは増やさない）。
    notifier_factory: Callable[[], LoggingAnomalyNotifier] = LoggingAnomalyNotifier

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        paused_env: bool,
        uploads_paused_env: bool,
        temporal_client: Client | None = None,
        gate_ports: GatePorts | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gate = gate_ports or GatePorts()
        if not self._gate.wired:
            # breaker・保留が効いていない（従来どおりの動き）ことを黙らせない（ADR-0058）
            logger.warning(
                "pipeline gates: provider health / credit hold ports are not wired; "
                "the daily gate treats the breaker as closed with no holds"
            )
        self._paused_env = paused_env
        self._uploads_paused_env = uploads_paused_env
        self._temporal_client = temporal_client

    def activities(self) -> Sequence[Callable[..., object]]:
        acts: list[Callable[..., object]] = [
            self.check_paused,
            self.claim_daily_slot,
            self.upload_gate,
            self.daily_gate,
            self.production_gate,
        ]
        if self._temporal_client is not None:
            # watchdog は Temporal を読む（Schedule / visibility）ので client がある worker だけ持つ
            acts.append(self.watchdog_check)
        return acts

    @activity.defn(name=WATCHDOG_CHECK_ACTIVITY)
    async def watchdog_check(self, request: WatchdogCheckRequest) -> WatchdogResult:
        """Daily の起動と Schedule の状態を検査し、異常を記録する（ADR-0027）。"""
        assert self._temporal_client is not None
        return await run_daily_watchdog(
            control=TemporalScheduleControl(self._temporal_client),
            session_factory=self._session_factory,
            workflow_counter=TemporalWorkflowStartCounter(self._temporal_client),
            notifier=self.notifier_factory(),
            request=request,
            pipeline_outcome_checker=TemporalPipelineOutcomeChecker(self._temporal_client),
            daily_run_reader=TemporalDailyRunReader(self._temporal_client),
            hold_reader=self._gate.open_holds,
        )

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
        # commit の後（log-contract §9）。LIMIT_REACHED は枠を取れていない（Workflow が
        # schedule.slot.skipped を出す）
        if claim.episode_id is not None:
            with log_guard():
                emit(
                    logging.getLogger(__name__),
                    EventName.SCHEDULE_SLOT_ACQUIRED,
                    logging.INFO,
                    "daily slot %s claim=%s episode=%s",
                    request.slot_date,
                    claim.outcome.value,
                    claim.episode_id,
                    episode_id=claim.episode_id,
                    stage=LogStage.SCHEDULE.value,
                    outcome=Outcome.SUCCEEDED.value,
                    attributes={
                        "slot_date": request.slot_date,
                        "claim": claim.outcome.value,
                        "slot_index": claim.slot_index,
                        "trigger_id": request.trigger_id,
                        "topic_plan_id": episode.topic_plan_id if episode is not None else None,
                    },
                )
        return ClaimDailySlotResult(
            outcome=claim.outcome.value,
            episode_id=claim.episode_id,
            topic_plan_id=episode.topic_plan_id if episode is not None else None,
        )

    @activity.defn(name=PIPELINE_DAILY_GATE)
    async def daily_gate(self, request: DailyGateRequest) -> DailyGateResult:
        """日次の入口の読み取り専用ゲート（ADR-0058）。モードを返すだけで何も書かない。

        breaker（production が使う課金アカウント）・未解放の保留数・未照合予約数を読む。
        DB を読めること自体が DB 健全性の確認（読めなければ再試行し、Daily は失敗する）。
        """
        budget = self._gate.budget
        health = self._gate.health
        states: list[BreakerState] = []
        if health is not None:
            for account in stage_accounts(PipelineStage.PRODUCTION):
                states.append((await health.snapshot(account)).state)
        holds = await health.open_hold_count() if health is not None else 0
        unreconciled = (
            await self._gate.unreconciled.count() if self._gate.unreconciled is not None else 0
        )
        mode, reasons = decide_daily_mode(
            breaker_states=states,
            open_hold_count=holds,
            max_held=MAX_HELD_EPISODES,
            budget=budget,
        )
        worst = max(states, key=_SEVERITY_RANK.__getitem__, default=BreakerState.CLOSED)
        if unreconciled:
            reasons.append(f"{unreconciled} unreconciled reservation(s): resume is never automatic")
        if budget is not None and not budget.submit_allowed:
            with log_guard():
                emit(
                    logger,
                    EventName.PROVIDER_CALL_SUPPRESSED,
                    logging.WARNING,
                    "daily gate slot=%s: paid generation is held (%s)",
                    request.slot_date,
                    budget.decision_reason,
                    stage=LogStage.SCHEDULE.value,
                    outcome=Outcome.BLOCKED.value,
                    attributes={
                        "slot_date": request.slot_date,
                        "mode": mode.value,
                        "budget_status": budget.status.value,
                        "hold_reason": budget.hold_reason.value if budget.hold_reason else None,
                        "submit_allowed": False,
                        "decision_reason": budget.decision_reason,
                        "open_hold_count": holds,
                    },
                )
        logger.info(
            "daily gate slot=%s mode=%s breaker=%s open_holds=%d unreconciled=%d",
            request.slot_date,
            mode.value,
            worst.value,
            holds,
            unreconciled,
        )
        return DailyGateResult(
            mode=mode.value,
            breaker_state=worst.value,
            open_hold_count=holds,
            unreconciled_reservations=unreconciled,
            budget_status=budget.status.value if budget is not None else "",
            reasons=reasons,
        )

    @activity.defn(name=PIPELINE_PRODUCTION_GATE)
    async def production_gate(self, request: ProductionGateRequest) -> ProductionGateResult:
        """production の直前: breaker が closed でなければ保留を記録して止める（ADR-0058）。

        ``half_open`` も止める（所有者承認の再開枠は日次が使わない）。保留の記録は冪等
        （``CreditHoldWriter`` の契約）。writer が無いのに breaker が開いているときは、有料の
        生成へ進めないよう例外にする（結線ミスを黙って通さない）。
        """
        budget = self._gate.budget
        if budget is not None and not budget.submit_allowed:
            return await self._hold_for_budget(request.episode_id, budget)
        health = self._gate.health
        if health is None:
            return ProductionGateResult(allowed=True)
        for account in stage_accounts(PipelineStage.PRODUCTION):
            snapshot = await health.snapshot(account)
            if snapshot.state is BreakerState.CLOSED:
                continue
            if self._gate.holds is None:
                raise RuntimeError(
                    "provider breaker is not closed but no CreditHoldWriter is wired"
                )
            reason = hold_reason_for(snapshot)
            await self._gate.holds.hold_episode(
                episode_id=request.episode_id,
                scene_id=None,
                account=account,
                reason=reason,
                resumable_stage=PipelineStage.PRODUCTION.value,
                generation_profile_id=None,
            )
            logger.warning(
                "production gate: episode=%s held (%s, breaker=%s)",
                request.episode_id,
                reason.value,
                snapshot.state.value,
            )
            return ProductionGateResult(
                allowed=False,
                reason=f"held: {reason.value} ({account.key} breaker {snapshot.state.value})",
            )
        return ProductionGateResult(allowed=True)

    async def _hold_for_budget(
        self, episode_id: str, budget: BudgetDecision
    ) -> ProductionGateResult:
        """ローカル予算が 0 / 未設定: fal へ何も送らず保留を記録して止める（ADR-0061）。"""
        reason = budget.hold_reason
        assert reason is not None
        if self._gate.holds is None:
            raise RuntimeError("budget holds production but no CreditHoldWriter is wired")
        for account in stage_accounts(PipelineStage.PRODUCTION):
            await self._gate.holds.hold_episode(
                episode_id=episode_id,
                scene_id=None,
                account=account,
                reason=reason,
                resumable_stage=PipelineStage.PRODUCTION.value,
                generation_profile_id=None,
                detail=budget.detail,
            )
        with log_guard():
            emit(
                logger,
                EventName.PROVIDER_CALL_SUPPRESSED,
                logging.WARNING,
                "production gate: episode=%s held (%s)",
                episode_id,
                budget.decision_reason,
                episode_id=episode_id,
                stage=LogStage.PRODUCTION.value,
                outcome=Outcome.BLOCKED.value,
                attributes={
                    "budget_status": budget.status.value,
                    "hold_reason": reason.value,
                    "submit_allowed": False,
                    "decision_reason": budget.decision_reason,
                },
            )
        return ProductionGateResult(
            allowed=False,
            reason=f"held: {reason.value} ({budget.decision_reason})",
            hold_reason=reason.value,
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
