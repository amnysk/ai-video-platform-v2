"""``anomaly_events.py``: 行 -> AnomalyEvent の変換（sqlite の固定行）。"""

from __future__ import annotations

import io
import json
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from contracts.anomaly_report import AnomalyCategory
from infrastructure.diagnostics.anomaly_events import (
    ScheduleRun,
    TemporalHttpReader,
    TemporalSnapshot,
    collect_report,
    load_job_failure_events,
    load_missed_run_events,
    load_watchdog_events,
    normalize_cause,
    recurring_unresolved_by_category,
    workflow_failure_events,
)

SLOT_SQL = (
    "INSERT INTO daily_episode_slots (slot_date, slot_index, trigger_id, episode_id, created_at) "
    "VALUES (:d, 0, 't1', :ep, :c)"
)
NOW = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)  # 2026-10-21 09:00 JST


async def _episode(session, status="blocked", created=NOW - timedelta(days=3)) -> str:
    eid = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO episodes (id, topic, status, created_at, updated_at, status_changed_at) "
            "VALUES (:id, 't', :s, :c, :c, :c)"
        ),
        {"id": eid.hex, "s": status, "c": created},
    )
    return eid.hex


async def _anomaly(session, kind, ep, *, reason="", occ=3, resolved=False, day=0):
    at = NOW - timedelta(days=day)
    await session.execute(
        text(
            "INSERT INTO operational_anomalies (id, kind, anomaly_date, detail, first_detected_at, "
            "last_detected_at, occurrences, resolved_at, episode_id) VALUES "
            "(:id, :k, :d, :det, :at, :at, :o, :r, :ep)"
        ),
        {
            "id": uuid.uuid4().hex,
            "k": kind,
            "d": at.date(),
            "det": json.dumps({"current_status": "blocked", "reason": reason}),
            "at": at,
            "o": occ,
            "r": at if resolved else None,
            "ep": ep,
        },
    )


async def _job(session, ep, typ, summary, status="terminal_failed"):
    await session.execute(
        text(
            "INSERT INTO jobs (id, episode_id, type, status, attempts, max_attempts, scene_id, "
            "error_summary, "
            "created_at, updated_at) VALUES (:id, :ep, :t, :s, 1, 3, :sc, :e, :at, :at)"
        ),
        {
            "id": uuid.uuid4().hex,
            "ep": ep,
            "t": typ,
            "s": status,
            "e": summary,
            "at": NOW,
            "sc": "s1" if typ.startswith("produce_scene") else None,
        },
    )


def test_normalize_cause_collapses_repeated_error_names_ids_and_numbers() -> None:
    a = normalize_cause(
        "needs_input: ProviderUnavailableError: ProviderUnavailableError: "
        "fal submit refused: HTTP 403 (credentials)"
    )
    assert a == "ProviderUnavailableError: fal submit refused: HTTP 403 (credentials)"
    b = normalize_cause("narration exceeds 1.9 words/s: s6: 12 words > budget 11 for 6000 ms")
    assert "12" not in b and "6000" not in b
    assert normalize_cause(None) == "unknown"


async def test_watchdog_rows_become_events_and_deadline_rows_take_the_stall_cause(session) -> None:
    ep = await _episode(session)
    await _anomaly(
        session, "EPISODE_STAGE_STALLED", ep, reason="needs_input: X: fal submit refused: HTTP 403"
    )
    await _anomaly(
        session,
        "EPISODE_NOT_COMPLETED_BY_DEADLINE",
        ep,
        reason="created 2026-10-01T00:00:00 still blocked",
    )
    events = await load_watchdog_events(session)
    by_cat = {e.category: e for e in events}
    assert (
        by_cat[AnomalyCategory.STAGE_STALLED].cause
        == by_cat[AnomalyCategory.NOT_COMPLETED_BY_DEADLINE].cause
    )
    assert "HTTP 403" in by_cat[AnomalyCategory.STAGE_STALLED].cause
    assert {e.incident_key for e in events} == {
        f"EPISODE_STAGE_STALLED:{uuid.UUID(ep)}",
        f"EPISODE_NOT_COMPLETED_BY_DEADLINE:{uuid.UUID(ep)}",
    }


async def test_resolved_at_marks_the_event_resolved(session) -> None:
    ep = await _episode(session)
    await _anomaly(session, "EPISODE_STAGE_STALLED", ep, resolved=True)
    assert [e.resolved for e in await load_watchdog_events(session)] == [True]


async def test_job_failures_are_split_by_type_and_non_fal_script_errors_are_ignored(
    session,
) -> None:
    ep = await _episode(session)
    done = await _episode(session, status="uploaded")
    await _job(
        session,
        ep,
        "produce_scene_image",
        "ProviderUnavailableError: fal submit refused: HTTP 403 (credentials)",
    )
    await _job(
        session,
        done,
        "produce_scene_image",
        "ProviderUnavailableError: fal submit refused: HTTP 403 (credentials)",
    )
    await _job(session, ep, "render_final_video", "VoiceTimelineOverflowError: x")
    await _job(session, ep, "upload_final_video", "UploadError: y")
    await _job(session, ep, "write_script", "ScriptNarrationOverBudgetError: z")
    await _job(session, ep, "produce_scene_video", "ok", status="succeeded")
    events = await load_job_failure_events(session)
    cats = sorted(e.category.value for e in events)
    assert cats == ["provider_error", "provider_error", "render_failure", "upload_failure"]
    provider = [e for e in events if e.category == AnomalyCategory.PROVIDER_ERROR]
    assert sorted(e.resolved for e in provider) == [False, True]  # 完成済みの Episode は解決済み


def test_failed_schedule_runs_are_workflow_failures_with_history_expiry_marked() -> None:
    snap = TemporalSnapshot(
        "s",
        False,
        [
            ScheduleRun(
                NOW - timedelta(hours=3), "daily-1", "FAILED", "codex exec unavailable: 400"
            ),
            ScheduleRun(NOW - timedelta(days=3), "daily-0", "FAILED", None),
            ScheduleRun(NOW - timedelta(days=4), "daily-x", "COMPLETED", None),
        ],
    )
    events = workflow_failure_events(snap)
    assert [e.cause for e in events] == ["codex exec unavailable: #", "unknown (history expired)"]


async def test_missed_runs_are_days_without_slot_or_episode_after_the_first_slot(session) -> None:
    ep = await _episode(session, created=datetime(2026, 10, 14, 21, 0, tzinfo=UTC))  # 10/15 JST
    await session.execute(
        text(SLOT_SQL),
        {"d": date(2026, 10, 14), "ep": ep, "c": NOW},
    )
    snap = TemporalSnapshot(
        "s", False, [ScheduleRun(datetime(2026, 10, 19, 21, 0, tzinfo=UTC), "d", "FAILED", "boom")]
    )
    events = await load_missed_run_events(session, NOW, snap)
    # 10/14 は slot、10/15 は Episode あり。10/16..10/21 JST が欠け
    assert [e.incident_key for e in events] == [f"missed:2026-10-{d}" for d in range(16, 22)]
    assert any("workflow failed before slot: boom" in e.cause for e in events)
    assert sum("workflow failed" in e.cause for e in events) == 1


async def test_a_missed_run_is_not_declared_before_todays_run_time(session) -> None:
    ep = await _episode(session)
    await session.execute(
        text(SLOT_SQL),
        {"d": date(2026, 10, 19), "ep": ep, "c": NOW},
    )
    early = datetime(2026, 10, 20, 20, 0, tzinfo=UTC)  # 10/21 05:00 JST
    keys = [
        e.incident_key
        for e in await load_missed_run_events(session, early, TemporalSnapshot("s", False))
    ]
    assert keys == ["missed:2026-10-20"]


async def test_collect_report_works_without_temporal_and_exposes_the_preflight_entry(
    session,
) -> None:
    ep = await _episode(session)
    await _anomaly(session, "EPISODE_STAGE_STALLED", ep, reason="x", day=0)
    report = await collect_report(session, NOW, None)
    assert report["temporal"]["error"]
    assert report["categories"]["stage_stalled"]["recurring_unresolved"] == 1
    counts = await recurring_unresolved_by_category(session, NOW, None)
    assert counts["stage_stalled"] == 1 and counts["render_failure"] == 0


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        self.close()


def test_http_reader_only_issues_get_requests_and_reads_schedule_state() -> None:
    seen: list[tuple[str, str]] = []

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url))
        if "/schedules/" in req.full_url:
            body = {
                "schedule": {"state": {"paused": False}},
                "info": {
                    "recentActions": [
                        {
                            "scheduleTime": "2026-10-20T21:00:00Z",
                            "startWorkflowStatus": "WORKFLOW_EXECUTION_STATUS_FAILED",
                            "startWorkflowResult": {"workflowId": "daily-1"},
                        }
                    ],
                    "futureActionTimes": ["2026-10-21T21:00:00Z"],
                },
            }
        elif "/history" in req.full_url:
            body = {
                "history": {
                    "events": [
                        {"eventType": "EVENT_TYPE_CHILD_WORKFLOW_EXECUTION_FAILED"},
                        {
                            "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED",
                            "workflowExecutionFailedEventAttributes": {
                                "failure": {"message": "outer", "cause": {"message": "root cause"}}
                            },
                        },
                    ]
                }
            }
        else:
            body = {"executions": [{"execution": {"workflowId": "running-1"}}]}
        return _Resp(json.dumps(body).encode())

    snap = TemporalHttpReader(opener=opener).snapshot("avp-daily-episode")
    assert snap.error is None and snap.paused is False
    assert snap.runs[0].failure == "root cause" and snap.runs[0].status == "FAILED"
    assert snap.next_runs and snap.running_workflows == ["running-1"]
    assert {m for m, _ in seen} == {"GET"}


def test_http_reader_reports_unreachable_temporal_instead_of_raising() -> None:
    def opener(req, timeout):
        raise OSError("connection refused")

    snap = TemporalHttpReader(opener=opener).snapshot("s")
    assert snap.paused is None and "connection refused" in (snap.error or "")
