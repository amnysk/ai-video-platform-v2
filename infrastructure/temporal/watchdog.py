"""Daily watchdog の本体（ADR-0027 / ADR-0031）。

毎日 06:00 の Schedule とは**別の Schedule**（``avp-daily-watchdog``、毎時）が起動する。
やること:

1. 期限切れの maintenance pause だけを解除する（emergency pause は触らない）
2. Schedule の状態を分類し、異常を DB へ記録する（``operational_anomalies``）
3. 予定時刻 + 猶予を過ぎても daily_episode_slot も DailyEpisodeWorkflow も無ければ
   ``DAILY_AUTOMATION_NOT_STARTED`` を記録する（起動）
4. Episode が blocked / needs_work のまま停滞猶予を超えていないか（進行、ADR-0031）
5. Episode が完成期限を超えても render_ready 以降に達していないか（完成、ADR-0031）
6. Episode が投稿期限を超えても uploaded に達していないか（投稿、ADR-0031。paused は除く）
7. 今日 close した ``EpisodePipelineWorkflow`` が outcome=stopped なのに 4〜6 のどれにも
   引っかからない食い違いが無いか（整合性、ADR-0031。Temporal の completed を成功と見なさない）
8. まだ通知していない異常を ``AnomalyNotifier`` へ渡す（既定はログ。失敗は次回再送）

watchdog は検出・記録するだけで、Episode の状態を変更しない。
Schedule が pause されていても watchdog は動く（別 Schedule だから）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from contracts.operations import OperationalSwitch
from contracts.pipeline import EpisodePipelineResult, PipelineOutcome
from contracts.schedule_guard import (
    HEALTH_ANOMALY,
    AnomalyKind,
    DailyStartStatus,
    ScheduleHealth,
    WatchdogCheckRequest,
    WatchdogResult,
)
from contracts.states import EpisodeStatus
from domain.schedule_guard import daily_start_status, latest_daily_fire
from infrastructure.db.repositories import (
    AnomalyRecord,
    DailyEpisodeSlotRepository,
    EpisodeRepository,
    OperationalAnomalyRepository,
    OperationalSwitchRepository,
)
from infrastructure.observability.anomaly_notifier import AnomalyNotice, AnomalyNotifier
from infrastructure.temporal.schedule_guard import ScheduleControl, reconcile_schedule

logger = logging.getLogger(__name__)

#: 健全なら閉じてよい Schedule 系の異常
_SCHEDULE_KINDS = (
    AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY,
    AnomalyKind.SCHEDULE_MISSING,
    AnomalyKind.SCHEDULE_NEXT_RUN_INVALID,
)

#: 進行監視の対象（ADR-0031 §進行）
_STALL_STATUSES = (EpisodeStatus.BLOCKED, EpisodeStatus.NEEDS_WORK)
#: 完成とみなす状態（ADR-0031 §完成）。これ以外は「未完成」
_COMPLETED_STATUSES = frozenset(
    {
        EpisodeStatus.RENDER_READY,
        EpisodeStatus.APPROVED,
        EpisodeStatus.UPLOADED,
        EpisodeStatus.ANALYZED,
    }
)
#: 完成監視の対象外（terminal。もう動かない）
_TERMINAL_EXCLUDED = frozenset(
    {EpisodeStatus.FAILED, EpisodeStatus.CANCELLED, EpisodeStatus.COMPLETED}
)
_INCOMPLETE_STATUSES = tuple(
    s for s in EpisodeStatus if s not in _COMPLETED_STATUSES and s not in _TERMINAL_EXCLUDED
)
#: 投稿監視の対象（ADR-0031 §投稿）
_AWAITING_UPLOAD_STATUSES = (EpisodeStatus.RENDER_READY, EpisodeStatus.APPROVED)


class WorkflowStartCounter(Protocol):
    async def count_started_since(self, workflow_type: str, since: datetime) -> int: ...


class TemporalWorkflowStartCounter:
    """Temporal の visibility で、``since`` 以降に始まった workflow の数を数える。"""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def count_started_since(self, workflow_type: str, since: datetime) -> int:
        stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = f'WorkflowType = "{workflow_type}" AND StartTime >= "{stamp}"'
        return (await self._client.count_workflows(query)).count


class PipelineOutcomeChecker(Protocol):
    """``EpisodePipelineResult`` をそのまま返す（別の最小構造体を増やさない）。"""

    async def list_stopped_since(
        self, workflow_type: str, since: datetime
    ) -> list[EpisodePipelineResult]:
        """``since`` 以降に close し、型付き結果が ``outcome=stopped`` だった実行だけを返す。"""
        ...


class TemporalPipelineOutcomeChecker:
    """Temporal の visibility + 各実行の型付き結果で outcome=stopped を拾う（ADR-0031 §整合性）。"""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def list_stopped_since(
        self, workflow_type: str, since: datetime
    ) -> list[EpisodePipelineResult]:
        stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = (
            f'WorkflowType = "{workflow_type}" AND CloseTime >= "{stamp}" '
            'AND ExecutionStatus = "Completed"'
        )
        stopped: list[EpisodePipelineResult] = []
        async for info in self._client.list_workflows(query):
            handle = self._client.get_workflow_handle(
                info.id, run_id=info.run_id, result_type=EpisodePipelineResult
            )
            try:
                result = await handle.result()
            except Exception as exc:  # 取得できないものは今回スキップ。次回の検査でまた見る
                logger.warning(
                    "watchdog: could not fetch pipeline result for %s: %s",
                    info.id,
                    type(exc).__name__,
                )
                continue
            if result.outcome == PipelineOutcome.STOPPED.value:
                stopped.append(result)
        return stopped


#: 人間が再開できる状態（ADR-0017 §8 / ADR-0019 / ADR-0020 の既存 admit 表が権威）。
#: TODO(ADR-0032): 統一再開エントリポイントの dry-run 判定関数が実装されたら、この局所判定を
#: それに置き換える（判定を2箇所に持たない。今は resumable のプレースホルダとして最小限だけ持つ）。
_RESUMABLE_STATUSES = frozenset(
    {
        EpisodeStatus.NEEDS_WORK,
        EpisodeStatus.BLOCKED,
        EpisodeStatus.SCRIPT_READY,
        EpisodeStatus.STORYBOARD_READY,
        EpisodeStatus.ASSETS_READY,
        EpisodeStatus.RENDER_READY,
    }
)


def _interim_resumable(status: str) -> bool:
    try:
        return EpisodeStatus(status) in _RESUMABLE_STATUSES
    except ValueError:
        return False


async def _check_stalled_episodes(
    episodes: EpisodeRepository,
    anomalies: OperationalAnomalyRepository,
    *,
    now: datetime,
    local_today: date,
    grace_minutes: int,
    blocked_grace_minutes: int,
) -> tuple[list[AnomalyRecord], set[uuid.UUID]]:
    """進行（ADR-0031）: blocked / needs_work のまま停滞猶予を超えた Episode。

    ``blocked``（needs_input、自動修復経路が無い）と ``needs_work``（retryable、自動retryで
    自己解決しうる）は猶予が違う。``blocked`` は既定0分（ほぼ即時）、``needs_work`` は既存の
    ``grace_minutes`` のまま（誤報を避ける）。
    """
    needs_work_threshold = now - timedelta(minutes=grace_minutes)
    blocked_threshold = now - timedelta(minutes=blocked_grace_minutes)
    thresholds = {
        EpisodeStatus.NEEDS_WORK: needs_work_threshold,
        EpisodeStatus.BLOCKED: blocked_threshold,
    }
    snapshots = await episodes.list_progress_snapshots(_STALL_STATUSES)
    stalled_ids: set[uuid.UUID] = set()
    records: list[AnomalyRecord] = []
    for snap in snapshots:
        threshold = thresholds[EpisodeStatus(snap.status)]
        if snap.status_changed_at > threshold:
            continue
        stalled_ids.add(snap.id)
        records.append(
            await anomalies.record(
                AnomalyKind.EPISODE_STAGE_STALLED,
                local_today,
                {
                    "episode_id": str(snap.id),
                    "current_status": snap.status,
                    "reason": snap.blocked_reason
                    or f"stuck in {snap.status} since {snap.status_changed_at.isoformat()}",
                    "resumable": _interim_resumable(snap.status),
                },
                now=now,
                episode_id=snap.id,
            )
        )
    open_stalled = await anomalies.list_open([AnomalyKind.EPISODE_STAGE_STALLED])
    recovered = [
        r.episode_id
        for r in open_stalled
        if r.episode_id is not None and r.episode_id not in stalled_ids
    ]
    if recovered:
        await anomalies.resolve_open(
            [AnomalyKind.EPISODE_STAGE_STALLED], now=now, episode_ids=recovered
        )
    return records, stalled_ids


async def _check_completion_deadline(
    episodes: EpisodeRepository,
    anomalies: OperationalAnomalyRepository,
    *,
    now: datetime,
    local_today: date,
    deadline_hours: float,
) -> tuple[list[AnomalyRecord], set[uuid.UUID]]:
    """完成（ADR-0031）: 作成から完成期限を超えても render_ready 以降に達していない Episode。"""
    threshold = timedelta(hours=deadline_hours)
    snapshots = await episodes.list_progress_snapshots(_INCOMPLETE_STATUSES)
    overdue_ids: set[uuid.UUID] = set()
    records: list[AnomalyRecord] = []
    for snap in snapshots:
        if now - snap.created_at < threshold:
            continue
        overdue_ids.add(snap.id)
        records.append(
            await anomalies.record(
                AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE,
                local_today,
                {
                    "episode_id": str(snap.id),
                    "current_status": snap.status,
                    "reason": f"created {snap.created_at.isoformat()}, still {snap.status} "
                    f"after the {deadline_hours}h completion deadline",
                    "resumable": _interim_resumable(snap.status),
                },
                now=now,
                episode_id=snap.id,
            )
        )
    open_overdue = await anomalies.list_open([AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE])
    recovered = [
        r.episode_id
        for r in open_overdue
        if r.episode_id is not None and r.episode_id not in overdue_ids
    ]
    if recovered:
        await anomalies.resolve_open(
            [AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE], now=now, episode_ids=recovered
        )
    return records, overdue_ids


async def _check_upload_deadline(
    episodes: EpisodeRepository,
    anomalies: OperationalAnomalyRepository,
    *,
    now: datetime,
    local_today: date,
    deadline_hours: float,
    uploads_paused: bool,
) -> tuple[list[AnomalyRecord], set[uuid.UUID]]:
    """投稿（ADR-0031）: render_ready/approved 到達から投稿期限を超えても uploaded に届かない。

    ``UPLOADS_PAUSED`` は意図した停止として扱い、新しい anomaly は作らない（既存スイッチの流用）。
    ただし既に記録済みの anomaly は、paused に変わった後もこの検査を通じて解決される
    （早期returnにすると、paused に切り替わった後は誰も resolve を呼ばず anomaly が残り続ける）。
    """
    threshold = timedelta(hours=deadline_hours)
    snapshots = await episodes.list_progress_snapshots(_AWAITING_UPLOAD_STATUSES)
    overdue_ids: set[uuid.UUID] = set()
    records: list[AnomalyRecord] = []
    for snap in snapshots:
        if uploads_paused or now - snap.status_changed_at < threshold:
            continue
        overdue_ids.add(snap.id)
        records.append(
            await anomalies.record(
                AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE,
                local_today,
                {
                    "episode_id": str(snap.id),
                    "current_status": snap.status,
                    "reason": f"reached {snap.status} at {snap.status_changed_at.isoformat()}, "
                    f"still not uploaded after the {deadline_hours}h upload deadline",
                    "resumable": _interim_resumable(snap.status),
                },
                now=now,
                episode_id=snap.id,
            )
        )
    open_overdue = await anomalies.list_open([AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE])
    recovered = [
        r.episode_id
        for r in open_overdue
        if r.episode_id is not None and r.episode_id not in overdue_ids
    ]
    if recovered:
        await anomalies.resolve_open(
            [AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE], now=now, episode_ids=recovered
        )
    return records, overdue_ids


async def _check_outcome_mismatch(
    outcome_checker: PipelineOutcomeChecker,
    anomalies: OperationalAnomalyRepository,
    episodes: EpisodeRepository,
    *,
    now: datetime,
    local_today: date,
    workflow_type: str,
    since: datetime,
    covered_episode_ids: set[uuid.UUID],
) -> list[AnomalyRecord]:
    """整合性（ADR-0031）: Temporal completed + outcome=stopped なのに他の検査に映らない食い違い。

    「Temporal の completed を成功と見なさない」ことの直接の機械検査。

    検出は「今日 local midnight 以降に close した実行」だけを見るため、他の3検査と違い
    同じ行を毎回見つけ直せない（前日に検出した行は翌日の検査範囲に入らない）。そのため回復は
    「今この時点の Episode の状態」で判定する: 今回 covered（他の検査が捕まえた = DB 側が
    追いついた）か、既に完成状態に達していれば、食い違いは解消したとみなして閉じる。
    """
    try:
        stopped = await outcome_checker.list_stopped_since(workflow_type, since)
    except Exception as exc:  # visibility 障害でこの回だけ検査できない。次回また見る
        logger.warning("watchdog: pipeline outcome check failed: %s", type(exc).__name__)
        stopped = []
    records: list[AnomalyRecord] = []
    for result in stopped:
        try:
            episode_uuid = uuid.UUID(result.episode_id)
        except ValueError:
            continue
        if episode_uuid in covered_episode_ids:
            continue
        # Temporal の実行履歴（保持期間内）と DB の Episode 行は別のライフサイクルを持つ
        # （DB 側が先に消える経路が有り得る）。存在しない Episode を指す古い実行は記録できない
        # （episode_id は外部キー）。1件のこの食い違いで watchdog 全体を落とさない（INV-13 と
        # 同じ「1つの失敗が他を止めない」思想を watchdog 自身にも適用する）
        if await episodes.get(episode_uuid) is None:
            logger.warning(
                "watchdog: outcome=stopped execution references an episode that no longer "
                "exists in the database (episode_id=%s); skipping this anomaly, not crashing "
                "the run",
                episode_uuid,
            )
            continue
        try:
            records.append(
                await anomalies.record(
                    AnomalyKind.PIPELINE_OUTCOME_MISMATCH,
                    local_today,
                    {
                        "episode_id": result.episode_id,
                        "current_status": result.status,
                        "stopped_stage": result.stopped_stage,
                        "reason": result.reason
                        or "pipeline workflow completed with outcome=stopped but no DB-side "
                        "check flagged this episode",
                        "resumable": _interim_resumable(result.status),
                    },
                    now=now,
                    episode_id=episode_uuid,
                )
            )
        except Exception:
            logger.exception(
                "watchdog: could not record PIPELINE_OUTCOME_MISMATCH for episode_id=%s; "
                "skipping this one, continuing with the rest of the batch",
                episode_uuid,
            )

    open_mismatches = await anomalies.list_open([AnomalyKind.PIPELINE_OUTCOME_MISMATCH])
    recovered: list[uuid.UUID] = []
    for row in open_mismatches:
        if row.episode_id is None:
            continue
        if row.episode_id in covered_episode_ids:
            recovered.append(row.episode_id)
            continue
        episode = await episodes.get(row.episode_id)
        if episode is not None and EpisodeStatus(episode.status) in _COMPLETED_STATUSES:
            recovered.append(row.episode_id)
    if recovered:
        await anomalies.resolve_open(
            [AnomalyKind.PIPELINE_OUTCOME_MISMATCH], now=now, episode_ids=recovered
        )
    return records


async def run_daily_watchdog(
    *,
    control: ScheduleControl,
    session_factory: async_sessionmaker[AsyncSession],
    workflow_counter: WorkflowStartCounter,
    notifier: AnomalyNotifier,
    request: WatchdogCheckRequest,
    pipeline_outcome_checker: PipelineOutcomeChecker,
) -> WatchdogResult:
    now = datetime.fromisoformat(request.now)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_today = _local_date(now, request.timezone)

    reconciled = await reconcile_schedule(control, request.schedule_id, now=now)
    health = reconciled.health
    detail_base: dict[str, object] = {
        "schedule_id": request.schedule_id,
        "schedule_health": health.value,
    }

    daily_status = DailyStartStatus.UNSUPPORTED_CRON
    slot_date: date | None = None
    workflow_query = "ok"
    try:
        slot_date, fire_time = latest_daily_fire(now, request.cron, request.timezone)
    except ValueError:
        logger.warning(
            "watchdog: cron %r is not a plain daily cron; start check skipped", request.cron
        )
    else:
        async with session_factory() as session:
            has_slot = bool(await DailyEpisodeSlotRepository(session).list_for_date(slot_date))
        has_workflow = False
        if not has_slot:
            try:
                has_workflow = (
                    await workflow_counter.count_started_since(request.workflow_type, fire_time) > 0
                )
            except Exception as exc:  # visibility 障害で「未起動」を隠さない（slot が主）
                workflow_query = "failed"
                logger.warning("watchdog: workflow visibility query failed: %s", type(exc).__name__)
        daily_status = daily_start_status(
            now=now,
            fire_time=fire_time,
            grace_seconds=request.grace_seconds,
            has_slot=has_slot,
            has_workflow=has_workflow,
        )
        detail_base["expected_fire_time"] = fire_time.isoformat()

    new_records: list[AnomalyRecord] = []
    episode_new_records: list[AnomalyRecord] = []
    async with session_factory() as session:
        anomalies = OperationalAnomalyRepository(session)
        switches = OperationalSwitchRepository(session)
        episodes = EpisodeRepository(session)
        uploads_paused = await switches.is_on(OperationalSwitch.UPLOADS_PAUSED)
        detail_base["switch_paused"] = await switches.is_on(OperationalSwitch.PAUSED)
        detail_base["switch_uploads_paused"] = uploads_paused

        if reconciled.released:
            new_records.append(
                await anomalies.record(
                    AnomalyKind.SCHEDULE_MAINTENANCE_OVERRUN,
                    local_today,
                    {**detail_base, "note": "expired maintenance pause released by the guard"},
                    now=now,
                )
            )
        kind = HEALTH_ANOMALY[health]
        if kind is not None:
            new_records.append(
                await anomalies.record(kind, local_today, dict(detail_base), now=now)
            )
        elif health is ScheduleHealth.HEALTHY:
            await anomalies.resolve_open(_SCHEDULE_KINDS, now=now)
            if not reconciled.released:
                await anomalies.resolve_open([AnomalyKind.SCHEDULE_MAINTENANCE_OVERRUN], now=now)

        if slot_date is not None:
            if daily_status is DailyStartStatus.NOT_STARTED:
                new_records.append(
                    await anomalies.record(
                        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED,
                        slot_date,
                        {
                            **detail_base,
                            "slot_date": slot_date.isoformat(),
                            "workflow_query": workflow_query,
                        },
                        now=now,
                    )
                )
            elif daily_status in (DailyStartStatus.STARTED_SLOT, DailyStartStatus.STARTED_WORKFLOW):
                await anomalies.resolve(
                    AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, slot_date, now=now
                )

        for record in new_records:
            if record.is_new:
                logger.error(
                    "anomaly=%s date=%s (recorded)", record.kind, record.anomaly_date.isoformat()
                )
        await session.commit()

        # --------------------------------------------------- Episode 進行・完成・投稿（ADR-0031）
        stalled_records, stalled_ids = await _check_stalled_episodes(
            episodes,
            anomalies,
            now=now,
            local_today=local_today,
            grace_minutes=request.stage_stall_grace_minutes,
            blocked_grace_minutes=request.blocked_grace_minutes,
        )
        completion_records, overdue_completion_ids = await _check_completion_deadline(
            episodes,
            anomalies,
            now=now,
            local_today=local_today,
            deadline_hours=request.completion_deadline_hours,
        )
        upload_records, overdue_upload_ids = await _check_upload_deadline(
            episodes,
            anomalies,
            now=now,
            local_today=local_today,
            deadline_hours=request.upload_deadline_hours,
            uploads_paused=uploads_paused,
        )
        covered_ids = stalled_ids | overdue_completion_ids | overdue_upload_ids
        local_midnight_utc = _local_midnight_utc(local_today, request.timezone)
        mismatch_records = await _check_outcome_mismatch(
            pipeline_outcome_checker,
            anomalies,
            episodes,
            now=now,
            local_today=local_today,
            workflow_type=request.pipeline_workflow_type,
            since=local_midnight_utc,
            covered_episode_ids=covered_ids,
        )
        episode_new_records = [
            *stalled_records,
            *completion_records,
            *upload_records,
            *mismatch_records,
        ]
        for record in episode_new_records:
            if record.is_new:
                logger.error(
                    "anomaly=%s date=%s episode_id=%s (recorded)",
                    record.kind,
                    record.anomaly_date.isoformat(),
                    record.episode_id,
                )
        await session.commit()

        for pending in await anomalies.pending_notifications():
            try:
                await notifier.notify(
                    AnomalyNotice(
                        kind=AnomalyKind(pending.kind),
                        anomaly_date=pending.anomaly_date,
                        occurrences=pending.occurrences,
                        detail=pending.detail,
                    )
                )
            except Exception as exc:  # 通知の失敗は検査を止めない。notified_at が空なので次回再送
                logger.warning("watchdog: notifier failed: %s", type(exc).__name__)
                continue
            await anomalies.mark_notified(pending.id, now=now)
        await session.commit()

    return WatchdogResult(
        schedule_health=health.value,
        daily_start=daily_status.value,
        anomalies=[r.kind for r in (*new_records, *episode_new_records) if r.is_new],
        released_maintenance=reconciled.released,
        slot_date=slot_date.isoformat() if slot_date else "",
        episode_anomaly_count=len(episode_new_records),
    )


def _local_date(now: datetime, timezone: str) -> date:
    from zoneinfo import ZoneInfo

    return now.astimezone(ZoneInfo(timezone)).date()


def _local_midnight_utc(local_date_value: date, timezone: str) -> datetime:
    from zoneinfo import ZoneInfo

    local_midnight = datetime(
        local_date_value.year,
        local_date_value.month,
        local_date_value.day,
        tzinfo=ZoneInfo(timezone),
    )
    return local_midnight.astimezone(UTC)
