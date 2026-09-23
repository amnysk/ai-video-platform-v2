"""Daily watchdog（ADR-0027 / ADR-0031）: 「今日の自動運転は始まったか」に加え、Episode の
進行・完成・投稿・Temporal結果とDB状態の整合性を Schedule とは別に確かめる。

Test D（pause のままなら検知・slot が無ければ DAILY_AUTOMATION_NOT_STARTED・slot があれば健全）と、
1日1回の通知・再発・通知失敗の再送・emergency pause を解除しないこと、
2026-09-22 事故型（completed + outcome=stopped が翌朝レポートを待たず検出されること）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from contracts.operations import OperationalSwitch
from contracts.pipeline import EpisodePipelineResult, PipelineOutcome
from contracts.schedule_guard import (
    AnomalyKind,
    DailyStartStatus,
    ScheduleHealth,
    WatchdogCheckRequest,
)
from contracts.states import EpisodeStatus
from infrastructure.db.models import EpisodeRow
from infrastructure.db.repositories import (
    DailyEpisodeSlotRepository,
    OperationalAnomalyRepository,
    OperationalSwitchRepository,
)
from infrastructure.observability.anomaly_notifier import AnomalyNotice
from infrastructure.temporal.schedule_guard import begin_maintenance
from infrastructure.temporal.watchdog import run_daily_watchdog
from tests.support.schedule_control import FakeScheduleControl

SCHEDULE = "avp-daily-episode"
#: 2026-09-21 06:35 JST。予定 06:00 + 猶予 30 分の後
AFTER_GRACE = datetime(2026, 9, 20, 21, 35, tzinfo=UTC)
#: 2026-09-21 06:10 JST。猶予の中
INSIDE_GRACE = datetime(2026, 9, 20, 21, 10, tzinfo=UTC)
SLOT_DATE = date(2026, 9, 21)


class RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.notices: list[AnomalyNotice] = []
        self.fail = fail

    async def notify(self, notice: AnomalyNotice) -> None:
        if self.fail:
            raise RuntimeError("notifier down")
        self.notices.append(notice)


class FakeCounter:
    def __init__(self, count: int = 0, *, error: bool = False) -> None:
        self.count = count
        self.error = error
        self.queries: list[tuple[str, datetime]] = []

    async def count_started_since(self, workflow_type: str, since: datetime) -> int:
        self.queries.append((workflow_type, since))
        if self.error:
            raise RuntimeError("visibility unavailable")
        return self.count


class FakeOutcomeChecker:
    """``PipelineOutcomeChecker`` の fake。あらかじめ与えた stopped 実行だけを返す。"""

    def __init__(
        self, stopped: list[EpisodePipelineResult] | None = None, *, error: bool = False
    ) -> None:
        self.stopped = stopped or []
        self.error = error
        self.queries: list[tuple[str, datetime]] = []

    async def list_stopped_since(
        self, workflow_type: str, since: datetime
    ) -> list[EpisodePipelineResult]:
        self.queries.append((workflow_type, since))
        if self.error:
            raise RuntimeError("visibility unavailable")
        return self.stopped


def _request(now: datetime, **overrides) -> WatchdogCheckRequest:  # type: ignore[no-untyped-def]
    return WatchdogCheckRequest(now=now.isoformat(), **overrides)


async def _run(  # type: ignore[no-untyped-def]
    session_factory, control, now, *, counter=None, notifier=None, outcome_checker=None, **overrides
):
    notifier = notifier or RecordingNotifier()
    result = await run_daily_watchdog(
        control=control,
        session_factory=session_factory,
        workflow_counter=counter or FakeCounter(),
        notifier=notifier,
        request=_request(now, **overrides),
        pipeline_outcome_checker=outcome_checker or FakeOutcomeChecker(),
    )
    return result, notifier


def _control(now: datetime, **kwargs) -> FakeScheduleControl:  # type: ignore[no-untyped-def]
    return FakeScheduleControl(clock=[now], **kwargs)


async def _open(session_factory, *, kinds=None):  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        return await OperationalAnomalyRepository(session).list_open(kinds)


async def _add_slot(session_factory, trigger: str = "daily-episode-x") -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await DailyEpisodeSlotRepository(session).claim(
            slot_date=SLOT_DATE, trigger_id=trigger, daily_limit=1, topic="t"
        )
        await session.commit()


async def _add_episode(  # type: ignore[no-untyped-def]
    session_factory,
    *,
    status: EpisodeStatus,
    status_changed_at: datetime,
    created_at: datetime | None = None,
    blocked_reason: str | None = None,
) -> uuid.UUID:
    """テスト用に任意の状態・時刻の Episode を直接作る（遷移表は経由しない。fixture 専用）。"""
    episode_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            EpisodeRow(
                id=episode_id,
                status=status.value,
                topic="t",
                created_at=created_at or status_changed_at,
                updated_at=status_changed_at,
                status_changed_at=status_changed_at,
                blocked_reason=blocked_reason,
            )
        )
        await session.commit()
    return episode_id


async def test_slot_present_after_grace_is_healthy_with_no_anomaly(session_factory) -> None:
    await _add_slot(session_factory)
    result, notifier = await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE)
    assert result.daily_start == DailyStartStatus.STARTED_SLOT.value
    assert result.schedule_health == ScheduleHealth.HEALTHY.value
    assert result.anomalies == []
    assert await _open(session_factory) == []
    assert notifier.notices == []


async def test_no_slot_and_no_workflow_records_daily_automation_not_started(
    session_factory,
) -> None:
    result, notifier = await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE)
    assert result.daily_start == DailyStartStatus.NOT_STARTED.value
    assert result.slot_date == "2026-09-21"
    assert result.anomalies == [AnomalyKind.DAILY_AUTOMATION_NOT_STARTED.value]
    (row,) = await _open(session_factory)
    assert row.kind == "DAILY_AUTOMATION_NOT_STARTED"
    assert row.anomaly_date == SLOT_DATE
    assert [n.kind for n in notifier.notices] == [AnomalyKind.DAILY_AUTOMATION_NOT_STARTED]


async def test_the_anomaly_is_recorded_and_notified_once_per_day(session_factory) -> None:
    notifier = RecordingNotifier()
    await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE, notifier=notifier)
    later = AFTER_GRACE + timedelta(hours=1)
    second, _ = await _run(session_factory, _control(later), later, notifier=notifier)
    (row,) = await _open(session_factory)
    assert row.occurrences == 2
    assert len(notifier.notices) == 1
    assert second.anomalies == []  # 新規ではない


async def test_a_workflow_without_a_slot_counts_as_started(session_factory) -> None:
    counter = FakeCounter(count=1)
    result, _ = await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE, counter=counter)
    assert result.daily_start == DailyStartStatus.STARTED_WORKFLOW.value
    assert result.anomalies == []
    # 予定時刻（06:00 JST = 前日 21:00Z）以降の起動を数える
    assert counter.queries == [("DailyEpisodeWorkflow", datetime(2026, 9, 20, 21, 0, tzinfo=UTC))]


async def test_inside_the_grace_period_nothing_is_judged(session_factory) -> None:
    result, _ = await _run(session_factory, _control(INSIDE_GRACE), INSIDE_GRACE)
    assert result.daily_start == DailyStartStatus.NOT_DUE.value
    assert await _open(session_factory) == []


async def test_a_paused_schedule_is_detected_and_never_unpaused(session_factory) -> None:
    """Test D: 今回の事故（pause のまま翌朝を迎える）。watchdog は検知するが解除はしない。"""
    control = _control(AFTER_GRACE, paused=True, note="paused for topic-planner deploy")
    result, notifier = await _run(session_factory, control, AFTER_GRACE)
    assert result.schedule_health == ScheduleHealth.PAUSED_UNEXPECTEDLY.value
    assert set(result.anomalies) == {
        AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY.value,
        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED.value,
    }
    assert control.paused is True
    assert control.calls == []
    assert result.released_maintenance is False
    assert {n.kind for n in notifier.notices} == {
        AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY,
        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED,
    }


async def test_the_paused_switch_state_is_reported_in_the_anomaly_detail(session_factory) -> None:
    async with session_factory() as session:
        await OperationalSwitchRepository(session).set(OperationalSwitch.PAUSED, True, reason="x")
        await session.commit()
    await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE)
    (row,) = await _open(session_factory)
    assert row.detail["switch_paused"] is True
    assert row.detail["switch_uploads_paused"] is False
    assert row.detail["schedule_health"] == "healthy"


async def test_an_expired_maintenance_pause_is_released_and_reported(session_factory) -> None:
    start = AFTER_GRACE - timedelta(hours=2)
    control = _control(start)

    async def _sleep(_: float) -> None:
        return None

    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=start, sleep=_sleep
    )
    control.clock[0] = AFTER_GRACE
    await _add_slot(session_factory)
    result, _ = await _run(session_factory, control, AFTER_GRACE)
    assert result.released_maintenance is True
    assert control.paused is False
    assert result.schedule_health == ScheduleHealth.HEALTHY.value
    kinds = {r.kind for r in await _open(session_factory)}
    assert kinds == {AnomalyKind.SCHEDULE_MAINTENANCE_OVERRUN.value}


async def test_a_maintenance_pause_inside_its_deadline_is_not_an_anomaly(session_factory) -> None:
    control = _control(INSIDE_GRACE)

    async def _sleep(_: float) -> None:
        return None

    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=INSIDE_GRACE, sleep=_sleep
    )
    result, _ = await _run(session_factory, control, INSIDE_GRACE)
    assert result.schedule_health == ScheduleHealth.MAINTENANCE_IN_PROGRESS.value
    assert result.anomalies == []
    assert control.paused is True


async def test_recovery_resolves_the_open_anomalies(session_factory) -> None:
    paused = _control(AFTER_GRACE, paused=True, note="operator stop")
    await _run(session_factory, paused, AFTER_GRACE)
    assert len(await _open(session_factory)) == 2

    later = AFTER_GRACE + timedelta(hours=1)
    await _add_slot(session_factory)
    healthy = _control(later)
    await _run(session_factory, healthy, later)
    assert await _open(session_factory) == []


async def test_a_missing_schedule_is_recorded(session_factory) -> None:
    result, _ = await _run(session_factory, _control(AFTER_GRACE, exists=False), AFTER_GRACE)
    assert result.schedule_health == ScheduleHealth.MISSING.value
    assert AnomalyKind.SCHEDULE_MISSING.value in result.anomalies


async def test_a_failing_notifier_is_retried_on_the_next_run(session_factory) -> None:
    down = RecordingNotifier(fail=True)
    await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE, notifier=down)
    async with session_factory() as session:
        assert len(await OperationalAnomalyRepository(session).pending_notifications()) == 1

    up = RecordingNotifier()
    later = AFTER_GRACE + timedelta(hours=1)
    await _run(session_factory, _control(later), later, notifier=up)
    assert [n.kind for n in up.notices] == [AnomalyKind.DAILY_AUTOMATION_NOT_STARTED]
    async with session_factory() as session:
        assert await OperationalAnomalyRepository(session).pending_notifications() == []


async def test_a_visibility_failure_does_not_hide_a_missing_slot(session_factory) -> None:
    result, _ = await _run(
        session_factory,
        _control(AFTER_GRACE),
        AFTER_GRACE,
        counter=FakeCounter(error=True),
    )
    assert result.daily_start == DailyStartStatus.NOT_STARTED.value
    (row,) = await _open(session_factory)
    assert row.detail["workflow_query"] == "failed"


async def test_a_cron_that_is_not_plain_daily_is_reported_but_not_judged(session_factory) -> None:
    request = WatchdogCheckRequest(now=AFTER_GRACE.isoformat(), cron="*/5 * * * *")
    result = await run_daily_watchdog(
        control=_control(AFTER_GRACE),
        session_factory=session_factory,
        workflow_counter=FakeCounter(),
        notifier=RecordingNotifier(),
        request=request,
        pipeline_outcome_checker=FakeOutcomeChecker(),
    )
    assert result.daily_start == DailyStartStatus.UNSUPPORTED_CRON.value
    assert await _open(session_factory) == []


# ------------------------------------------------------------------------------ ADR-0031: 進行


async def test_a_blocked_episode_past_the_stall_grace_is_flagged_and_resolves_on_recovery(
    session_factory,
) -> None:
    await _add_slot(session_factory)
    episode_id = await _add_episode(
        session_factory,
        status=EpisodeStatus.BLOCKED,
        status_changed_at=AFTER_GRACE - timedelta(minutes=90),
        blocked_reason="fal storage token refused: HTTP 403",
    )
    result, notifier = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, stage_stall_grace_minutes=60
    )
    assert AnomalyKind.EPISODE_STAGE_STALLED.value in result.anomalies
    assert result.episode_anomaly_count == 1
    (row,) = await _open(session_factory, kinds=[AnomalyKind.EPISODE_STAGE_STALLED])
    assert row.episode_id == episode_id
    assert row.detail["episode_id"] == str(episode_id)
    assert row.detail["reason"] == "fal storage token refused: HTTP 403"
    assert row.detail["resumable"] is True
    assert {n.kind for n in notifier.notices} >= {AnomalyKind.EPISODE_STAGE_STALLED}

    # 回復（再開して in_progress に進んだ）→ 次の検査で自動的に閉じる
    async with session_factory() as session:
        row_db = await session.get(EpisodeRow, episode_id)
        row_db.status = EpisodeStatus.IN_PROGRESS.value
        row_db.status_changed_at = AFTER_GRACE
        await session.commit()
    later = AFTER_GRACE + timedelta(minutes=5)
    await _run(session_factory, _control(later), later, stage_stall_grace_minutes=60)
    assert await _open(session_factory, kinds=[AnomalyKind.EPISODE_STAGE_STALLED]) == []


async def test_a_needs_work_stall_inside_the_grace_period_is_not_flagged(session_factory) -> None:
    """needs_work（retryable）は自動retryで自己解決しうるので猶予を置く。"""
    await _add_slot(session_factory)
    await _add_episode(
        session_factory,
        status=EpisodeStatus.NEEDS_WORK,
        status_changed_at=AFTER_GRACE - timedelta(minutes=10),
    )
    result, _ = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, stage_stall_grace_minutes=60
    )
    assert AnomalyKind.EPISODE_STAGE_STALLED.value not in result.anomalies


async def test_a_blocked_episode_is_flagged_almost_immediately_regardless_of_needs_work_grace(
    session_factory,
) -> None:
    """blocked（needs_input、自動修復経路が無い）は needs_work の猶予を共有しない（既定0分）。

    独立レビュー相当の監査で判明: 従来は blocked / needs_work が同じ
    ``stage_stall_grace_minutes`` を共有しており、needs_work 向けの正当な猶予
    （自動retryで自己解決しうる）が blocked（人間のsignal待ち、
    docs/domain/state-transitions.md）にも誤って適用されていた。
    """
    await _add_slot(session_factory)
    await _add_episode(
        session_factory,
        status=EpisodeStatus.BLOCKED,
        status_changed_at=AFTER_GRACE - timedelta(minutes=1),
    )
    result, _ = await _run(
        session_factory,
        _control(AFTER_GRACE),
        AFTER_GRACE,
        stage_stall_grace_minutes=60,  # needs_work 用の猶予。blocked には効かないことを検査する
    )
    assert AnomalyKind.EPISODE_STAGE_STALLED.value in result.anomalies


async def test_two_different_episodes_stalled_the_same_day_both_get_their_own_row(
    session_factory,
) -> None:
    """operational_anomalies の部分インデックス（ADR-0031）: 同日でも Episode ごとに1行。"""
    await _add_slot(session_factory)
    old = AFTER_GRACE - timedelta(hours=3)
    ep1 = await _add_episode(session_factory, status=EpisodeStatus.BLOCKED, status_changed_at=old)
    ep2 = await _add_episode(
        session_factory, status=EpisodeStatus.NEEDS_WORK, status_changed_at=old
    )
    await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE, stage_stall_grace_minutes=60)
    rows = await _open(session_factory, kinds=[AnomalyKind.EPISODE_STAGE_STALLED])
    assert {r.episode_id for r in rows} == {ep1, ep2}


# ------------------------------------------------------------------------------ ADR-0031: 完成


async def test_an_episode_past_the_completion_deadline_is_flagged(session_factory) -> None:
    await _add_slot(session_factory)
    episode_id = await _add_episode(
        session_factory,
        status=EpisodeStatus.IN_PROGRESS,
        status_changed_at=AFTER_GRACE - timedelta(hours=10),
        created_at=AFTER_GRACE - timedelta(hours=10),
    )
    result, _ = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, completion_deadline_hours=8.0
    )
    assert AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE.value in result.anomalies
    (row,) = await _open(session_factory, kinds=[AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE])
    assert row.episode_id == episode_id


async def test_render_ready_counts_as_completed_and_is_never_flagged(session_factory) -> None:
    await _add_slot(session_factory)
    await _add_episode(
        session_factory,
        status=EpisodeStatus.RENDER_READY,
        status_changed_at=AFTER_GRACE - timedelta(hours=10),
        created_at=AFTER_GRACE - timedelta(hours=10),
    )
    result, _ = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, completion_deadline_hours=8.0
    )
    assert AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE.value not in result.anomalies


# ------------------------------------------------------------------------------ ADR-0031: 投稿


async def test_an_episode_past_the_upload_deadline_is_flagged(session_factory) -> None:
    await _add_slot(session_factory)
    episode_id = await _add_episode(
        session_factory,
        status=EpisodeStatus.RENDER_READY,
        status_changed_at=AFTER_GRACE - timedelta(hours=3),
    )
    result, _ = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, upload_deadline_hours=2.0
    )
    assert AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE.value in result.anomalies
    (row,) = await _open(session_factory, kinds=[AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE])
    assert row.episode_id == episode_id


async def test_uploads_paused_suppresses_new_flags_and_resolves_existing_ones(
    session_factory,
) -> None:
    await _add_slot(session_factory)
    await _add_episode(
        session_factory,
        status=EpisodeStatus.RENDER_READY,
        status_changed_at=AFTER_GRACE - timedelta(hours=3),
    )
    result, _ = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, upload_deadline_hours=2.0
    )
    assert AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE.value in result.anomalies

    async with session_factory() as session:
        await OperationalSwitchRepository(session).set(OperationalSwitch.UPLOADS_PAUSED, True)
        await session.commit()
    later = AFTER_GRACE + timedelta(minutes=5)
    result2, _ = await _run(session_factory, _control(later), later, upload_deadline_hours=2.0)
    assert AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE.value not in result2.anomalies
    assert await _open(session_factory, kinds=[AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE]) == []


# ---------------------------------------------------------------------------- ADR-0031: 整合性


def _stopped_result(episode_id: uuid.UUID, *, stage: str = "production") -> EpisodePipelineResult:
    return EpisodePipelineResult(
        episode_id=str(episode_id),
        outcome=PipelineOutcome.STOPPED.value,
        status="blocked",
        stopped_stage=stage,
        reason=f"{stage} returned 'blocked', expected 'assets_ready'",
    )


async def test_completed_pipeline_with_outcome_stopped_is_flagged_when_uncovered(
    session_factory,
) -> None:
    """2026-09-22 型の事故: Temporal は completed でも outcome=stopped で他の検査に映らない。

    Episode は実在させる（``uuid.uuid4()`` の架空 id のままだと、独立の統合試験が発見した
    「存在しない Episode を指す stopped 実行はこの watchdog 全体を落とさず静かにスキップする」
    という安全策 [INV-13 と同じ「1つの失敗が他を止めない」思想] に、この検査ケース自体が
    引っかかってしまう。実在する Episode の「まだどの検査にも引っかからない」状態を使う）。
    """
    await _add_slot(session_factory)
    episode_id = await _add_episode(
        session_factory,
        status=EpisodeStatus.IN_PROGRESS,
        status_changed_at=AFTER_GRACE - timedelta(minutes=5),
    )
    checker = FakeOutcomeChecker([_stopped_result(episode_id)])
    result, notifier = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, outcome_checker=checker
    )
    assert AnomalyKind.PIPELINE_OUTCOME_MISMATCH.value in result.anomalies
    (row,) = await _open(session_factory, kinds=[AnomalyKind.PIPELINE_OUTCOME_MISMATCH])
    assert row.episode_id == episode_id
    assert row.detail["stopped_stage"] == "production"
    assert {n.kind for n in notifier.notices} >= {AnomalyKind.PIPELINE_OUTCOME_MISMATCH}


async def test_a_stopped_pipeline_already_covered_by_the_stall_check_is_not_double_reported(
    session_factory,
) -> None:
    await _add_slot(session_factory)
    episode_id = await _add_episode(
        session_factory,
        status=EpisodeStatus.BLOCKED,
        status_changed_at=AFTER_GRACE - timedelta(minutes=90),
    )
    checker = FakeOutcomeChecker([_stopped_result(episode_id)])
    result, _ = await _run(
        session_factory,
        _control(AFTER_GRACE),
        AFTER_GRACE,
        stage_stall_grace_minutes=60,
        outcome_checker=checker,
    )
    assert AnomalyKind.EPISODE_STAGE_STALLED.value in result.anomalies
    assert AnomalyKind.PIPELINE_OUTCOME_MISMATCH.value not in result.anomalies


async def test_an_outcome_checker_failure_does_not_crash_the_whole_watchdog_run(
    session_factory,
) -> None:
    await _add_slot(session_factory)
    result, _ = await _run(
        session_factory,
        _control(AFTER_GRACE),
        AFTER_GRACE,
        outcome_checker=FakeOutcomeChecker(error=True),
    )
    assert result.schedule_health == ScheduleHealth.HEALTHY.value
    assert AnomalyKind.PIPELINE_OUTCOME_MISMATCH.value not in result.anomalies


async def test_an_uncovered_outcome_mismatch_resolves_once_the_episode_completes(
    session_factory,
) -> None:
    """独立レビューが見つけた欠落: PIPELINE_OUTCOME_MISMATCH は他の3種と違い開いたままだった。

    「今日 close した実行」だけを見る検出範囲は同じ行を再検出できないので、回復は
    Episode の現在状態（完成状態に達したか）で判定する（実装コメント参照）。
    """
    await _add_slot(session_factory)
    episode_id = await _add_episode(
        session_factory,
        status=EpisodeStatus.IN_PROGRESS,
        status_changed_at=AFTER_GRACE - timedelta(minutes=5),
    )
    checker = FakeOutcomeChecker([_stopped_result(episode_id)])
    result, _ = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, outcome_checker=checker
    )
    assert AnomalyKind.PIPELINE_OUTCOME_MISMATCH.value in result.anomalies
    assert len(await _open(session_factory, kinds=[AnomalyKind.PIPELINE_OUTCOME_MISMATCH])) == 1

    # 回復（人間が再開し、最終的に uploaded に到達した）→ 次の検査で自動的に閉じる
    async with session_factory() as session:
        row_db = await session.get(EpisodeRow, episode_id)
        row_db.status = EpisodeStatus.UPLOADED.value
        row_db.status_changed_at = AFTER_GRACE
        await session.commit()
    later = AFTER_GRACE + timedelta(minutes=5)
    await _run(
        session_factory,
        _control(later),
        later,
        outcome_checker=FakeOutcomeChecker(),  # 今日はもう stopped な実行が無い
    )
    assert await _open(session_factory, kinds=[AnomalyKind.PIPELINE_OUTCOME_MISMATCH]) == []


async def test_a_stopped_execution_for_a_nonexistent_episode_does_not_crash_the_whole_run(
    session_factory,
) -> None:
    """独立の統合試験で発見: 存在しない Episode を指す stopped 実行があっても watchdog は
    落ちない（INV-13 と同じ「1つの失敗が他を止めない」思想を watchdog 自身にも適用する）。

    Temporal の実行履歴（保持期間内）と DB の Episode 行は別のライフサイクルを持ちうる
    （DB 側が先に消える経路が有り得る）。このケースは記録をスキップするだけで、他の
    Episode の異常検出・通知は影響を受けない。
    """
    await _add_slot(session_factory)
    real_episode_id = await _add_episode(
        session_factory,
        status=EpisodeStatus.IN_PROGRESS,
        status_changed_at=AFTER_GRACE - timedelta(minutes=5),
    )
    ghost_episode_id = uuid.uuid4()  # DB に行が無い（例: Temporal の履歴だけ残っている）
    checker = FakeOutcomeChecker(
        [_stopped_result(ghost_episode_id), _stopped_result(real_episode_id)]
    )

    result, notifier = await _run(
        session_factory, _control(AFTER_GRACE), AFTER_GRACE, outcome_checker=checker
    )

    # 存在しない方は記録されない。実在する方は正常に記録される（クラッシュしない）
    assert AnomalyKind.PIPELINE_OUTCOME_MISMATCH.value in result.anomalies
    open_rows = await _open(session_factory, kinds=[AnomalyKind.PIPELINE_OUTCOME_MISMATCH])
    assert [r.episode_id for r in open_rows] == [real_episode_id]
    assert {n.kind for n in notifier.notices} >= {AnomalyKind.PIPELINE_OUTCOME_MISMATCH}
