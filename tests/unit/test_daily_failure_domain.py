"""日次 workflow 失敗の判定・連続数・要約（純粋ロジック。ADR-0054 / INV-68）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from contracts.schedule_guard import (
    DAILY_FAILURE_MESSAGE_LIMIT,
    DAILY_FAILURE_TYPE_LIMIT,
    DailyRunStatus,
)
from domain.daily_failure import (
    DailyRunObservation,
    DayVerdict,
    failure_streaks,
    judge_day,
    lookback_dates,
    slot_date_of,
    summarize_failure,
)

D = date(2026, 10, 20)


def _obs(status: DailyRunStatus, minute: int = 0) -> DailyRunObservation:
    return DailyRunObservation(
        slot_date=D,
        workflow_id=f"daily-episode-{minute}",
        status=status,
        started_at=datetime(2026, 10, 19, 21, minute, tzinfo=UTC),
        failure_type="ApplicationError",
        failure_message="boom",
    )


def _judge(runs, *, has_slot=False, readable=True, latest=False):  # type: ignore[no-untyped-def]
    return judge_day(has_slot=has_slot, runs=runs, readable=readable, is_latest_slot_day=latest)


def test_failed_without_slot_is_failed() -> None:
    for status in (
        DailyRunStatus.FAILED,
        DailyRunStatus.TIMED_OUT,
        DailyRunStatus.TERMINATED,
        DailyRunStatus.CANCELED,
    ):
        verdict, run = _judge([_obs(status)])
        assert verdict is DayVerdict.FAILED
        assert run is not None and run.status is status


def test_completed_or_running_is_ok() -> None:
    for status in (DailyRunStatus.COMPLETED, DailyRunStatus.RUNNING):
        assert _judge([_obs(status)])[0] is DayVerdict.OK
    # 失敗の後に成功した再実行がある日は失敗日ではない
    assert _judge([_obs(DailyRunStatus.FAILED, 0), _obs(DailyRunStatus.COMPLETED, 5)])[0] is (
        DayVerdict.OK
    )


def test_slot_means_episode_checks_own_the_day() -> None:
    assert _judge([_obs(DailyRunStatus.FAILED)], has_slot=True)[0] is DayVerdict.OK


def test_latest_failed_run_is_the_one_reported() -> None:
    _, run = _judge([_obs(DailyRunStatus.TERMINATED, 0), _obs(DailyRunStatus.FAILED, 9)])
    assert run is not None and run.workflow_id == "daily-episode-9"


def test_unreadable_is_unobservable_never_ok() -> None:
    assert _judge([], readable=False)[0] is DayVerdict.UNOBSERVABLE
    assert _judge([], readable=False, latest=True)[0] is DayVerdict.UNOBSERVABLE


def test_no_run_found_defers_for_latest_day_and_is_unobservable_for_older_days() -> None:
    assert _judge([], latest=True)[0] is DayVerdict.DEFERRED  # NOT_STARTED の担当
    assert _judge([], latest=False)[0] is DayVerdict.UNOBSERVABLE  # 保持期間切れかもしれない


def test_unknown_status_only_is_unobservable() -> None:
    assert _judge([_obs(DailyRunStatus.OTHER)])[0] is DayVerdict.UNOBSERVABLE


def test_lookback_window_is_ascending_and_clamped() -> None:
    assert lookback_dates(D, 3) == [D - timedelta(days=2), D - timedelta(days=1), D]
    assert lookback_dates(D, 0) == [D]
    assert len(lookback_dates(D, 999)) == 7


def test_streaks_count_consecutive_failed_days_and_seed_from_prior() -> None:
    days = lookback_dates(D, 3)
    assert failure_streaks(days, set(days), prior_streak=0) == dict(
        zip(days, (1, 2, 3), strict=True)
    )
    # 窓の直前の日が既に 4 連続で記録されていれば続ける
    assert failure_streaks(days, set(days), prior_streak=4)[D] == 7
    # 途中に健全な日があれば切れる
    assert failure_streaks(days, {days[0], days[2]}, prior_streak=0) == {
        days[0]: 1,
        days[1]: 0,
        days[2]: 1,
    }


def test_slot_date_uses_the_operating_timezone() -> None:
    started = datetime(2026, 10, 19, 21, 0, 9, tzinfo=UTC)  # 06:00:09 JST
    assert slot_date_of(started, "Asia/Tokyo") == date(2026, 10, 20)


def test_summary_redacts_secrets_and_truncates() -> None:
    raw = (
        "call failed api_key=sk-live-ABCDEF1234567890 Authorization: Bearer abc.def.ghi "
        "url=https://u:pw@host.example/p?token=SECRET123&x=1 "
        "id=6f1c2a3e-1111-4222-8333-444455556666 blob=" + "A" * 80 + " " + "tail " * 100
    )
    kind, message = summarize_failure("ApplicationError" + "X" * 200, raw)
    assert kind is not None and len(kind) <= DAILY_FAILURE_TYPE_LIMIT
    assert message is not None and len(message) <= DAILY_FAILURE_MESSAGE_LIMIT
    for leaked in ("sk-live", "ABCDEF123", "Bearer abc", "SECRET123", "pw@", "6f1c2a3e", "A" * 40):
        assert leaked not in message
    assert "call failed" in message


def test_summary_of_nothing_is_nothing() -> None:
    assert summarize_failure(None, None) == (None, None)
    assert summarize_failure("", "   ") == (None, None)
