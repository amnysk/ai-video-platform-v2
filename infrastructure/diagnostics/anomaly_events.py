"""日次異常レポートの材料を読む（SELECT と GET だけ。何も書かない）。

PostgreSQL ではトランザクションを ``READ ONLY`` にしてから読む。Temporal は UI の HTTP API を
GET するだけ（Schedule の describe と workflow の一覧・履歴）。
provider・MinIO・OpenSearch には触れない。
集計は ``domain/anomaly_report.py``。ここは行を ``AnomalyEvent`` に変えるだけ。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.anomaly_report import AnomalyCategory
from contracts.schedule_guard import AnomalyKind
from contracts.states import EpisodeStatus
from domain.anomaly_report import AnomalyEvent

#: 解決済みとみなす Episode の状態（完成点に達した、または意図して取り消された）
RESOLVED_EPISODE_STATUSES = frozenset(
    {
        EpisodeStatus.RENDER_READY.value,
        EpisodeStatus.APPROVED.value,
        EpisodeStatus.UPLOADED.value,
        EpisodeStatus.ANALYZED.value,
        EpisodeStatus.COMPLETED.value,
        EpisodeStatus.CANCELLED.value,
    }
)

WATCHDOG_CATEGORY: dict[str, AnomalyCategory] = {
    AnomalyKind.EPISODE_STAGE_STALLED.value: AnomalyCategory.STAGE_STALLED,
    AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE.value: AnomalyCategory.NOT_COMPLETED_BY_DEADLINE,
    AnomalyKind.EPISODE_NOT_UPLOADED_BY_DEADLINE.value: AnomalyCategory.NOT_UPLOADED_BY_DEADLINE,
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def normalize_cause(raw: str | None, limit: int = 110) -> str:
    """原因文を比較できる形に畳む（UUID・時刻・数を潰し、重複した例外名を1つにする）。"""
    if not raw:
        return "unknown"
    s = re.sub(r"\s+", " ", raw).strip()
    s = re.sub(r"^(needs_input|retryable|terminal|blocked): ", "", s)
    s = re.sub(r"\b(\w+Error): \1: ", r"\1: ", s)
    s = _UUID.sub("<id>", s)
    s = re.sub(r"(?<!HTTP )(?<![A-Za-z\d])\d+(\.\d+)?", "#", s)
    return s[:limit]


def _utc(value: datetime | str) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _detail(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


async def _read_only(session: AsyncSession) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(text("SET TRANSACTION READ ONLY"))


async def load_watchdog_events(session: AsyncSession) -> list[AnomalyEvent]:
    """``operational_anomalies`` の Episode 系3種。1行 = 1 Episode の1日分の検出。"""
    await _read_only(session)
    kinds = ", ".join(f"'{k}'" for k in WATCHDOG_CATEGORY)
    rows = (
        await session.execute(
            text(
                "SELECT kind, episode_id, first_detected_at, last_detected_at, occurrences, "
                "resolved_at, detail FROM operational_anomalies "
                f"WHERE episode_id IS NOT NULL AND kind IN ({kinds})"
            )
        )
    ).all()
    stall_cause: dict[str, str] = {}
    status_cause: dict[str, str] = {}
    for r in rows:
        d = _detail(r.detail)
        ep = _eid(r.episode_id)
        status_cause[ep] = f"status={d.get('current_status', 'unknown')}"
        if r.kind == AnomalyKind.EPISODE_STAGE_STALLED.value:
            stall_cause[ep] = normalize_cause(d.get("reason"))
    events: list[AnomalyEvent] = []
    for r in rows:
        ep = _eid(r.episode_id)
        # NOT_COMPLETED / NOT_UPLOADED は「停滞の結果」。原因は同じ Episode の停滞理由に寄せる
        cause = stall_cause.get(ep) or status_cause[ep]
        events.append(
            AnomalyEvent(
                category=WATCHDOG_CATEGORY[r.kind],
                incident_key=f"{r.kind}:{ep}",
                episode_id=ep,
                first_seen_at=_utc(r.first_detected_at),
                last_seen_at=_utc(r.last_detected_at),
                cause=cause,
                resolved=r.resolved_at is not None,
                detections=int(r.occurrences or 1),
            )
        )
    return events


async def load_job_failure_events(session: AsyncSession) -> list[AnomalyEvent]:
    """終端失敗した job を provider / render / upload に分ける。"""
    await _read_only(session)
    rows = (
        await session.execute(
            text(
                "SELECT j.episode_id, j.type, j.scene_id, j.error_summary, j.updated_at, "
                "e.status AS episode_status FROM jobs j JOIN episodes e ON e.id = j.episode_id "
                "WHERE j.status IN ('terminal_failed', 'failed')"
            )
        )
    ).all()
    events: list[AnomalyEvent] = []
    for r in rows:
        summary = r.error_summary or ""
        if r.type == "render_final_video":
            category = AnomalyCategory.RENDER_FAILURE
        elif r.type == "upload_final_video":
            category = AnomalyCategory.UPLOAD_FAILURE
        elif re.search(r"\bfal\b", summary):
            category = AnomalyCategory.PROVIDER_ERROR
        else:
            continue
        cause = normalize_cause(summary)
        at = _utc(r.updated_at)
        events.append(
            AnomalyEvent(
                category=category,
                incident_key=f"{_eid(r.episode_id)}:{r.type}:{r.scene_id or ''}:{cause}",
                episode_id=_eid(r.episode_id),
                first_seen_at=at,
                last_seen_at=at,
                cause=cause,
                resolved=r.episode_status in RESOLVED_EPISODE_STATUSES,
            )
        )
    return events


# ------------------------------------------------------------------ Temporal（GET だけ）


@dataclass(frozen=True)
class ScheduleRun:
    scheduled_at: datetime
    workflow_id: str
    status: str  # COMPLETED / FAILED / RUNNING ...
    failure: str | None = None


@dataclass(frozen=True)
class TemporalSnapshot:
    schedule_id: str
    paused: bool | None
    runs: Sequence[ScheduleRun] = ()
    next_runs: Sequence[datetime] = ()
    running_workflows: Sequence[str] = ()
    error: str | None = None


class TemporalReader(Protocol):
    def snapshot(self, schedule_id: str) -> TemporalSnapshot: ...


@dataclass
class TemporalHttpReader:
    """Temporal UI の HTTP API を GET するだけ。到達できなければ ``error`` 付きで返す。"""

    base_url: str = "http://localhost:8233"
    namespace: str = "default"
    timeout: float = 10.0
    opener: Any = field(default=urllib.request.urlopen, repr=False)

    def _get(self, path: str, **query: str) -> dict[str, Any]:
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        url = f"{self.base_url}/api/v1/namespaces/{self.namespace}{path}{qs}"
        req = urllib.request.Request(url, method="GET")
        with self.opener(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def _failure_message(self, workflow_id: str) -> str | None:
        try:
            body = self._get(
                f"/workflows/{urllib.parse.quote(workflow_id, safe='')}/history",
                maximumPageSize="200",
            )
        except Exception:  # 保持期間（既定1日）を過ぎた履歴は無い
            return None
        for event in body.get("history", {}).get("events", []):
            # 子 workflow の失敗（CHILD_WORKFLOW_EXECUTION_FAILED）ではなく親自身の失敗だけ
            if event.get("eventType") == "EVENT_TYPE_WORKFLOW_EXECUTION_FAILED":
                failure = event["workflowExecutionFailedEventAttributes"].get("failure", {})
                while failure.get("cause"):
                    failure = failure["cause"]
                return failure.get("message")
        return None

    def snapshot(self, schedule_id: str) -> TemporalSnapshot:
        try:
            body = self._get(f"/schedules/{schedule_id}")
            info = body.get("info", {})
            runs: list[ScheduleRun] = []
            for a in info.get("recentActions", []):
                status = str(a.get("startWorkflowStatus", "")).replace(
                    "WORKFLOW_EXECUTION_STATUS_", ""
                )
                wid = a.get("startWorkflowResult", {}).get("workflowId", "")
                msg = self._failure_message(wid) if status == "FAILED" else None
                runs.append(
                    ScheduleRun(_utc(a["scheduleTime"].replace("Z", "+00:00")), wid, status, msg)
                )
            nxt = [_utc(t.replace("Z", "+00:00")) for t in info.get("futureActionTimes", [])]
            running = self._get("/workflows", query="ExecutionStatus='Running'", pageSize="100")
            names = [w["execution"]["workflowId"] for w in running.get("executions", [])]
            return TemporalSnapshot(
                schedule_id,
                bool(body["schedule"]["state"].get("paused", False)),
                runs,
                nxt,
                names,
            )
        except Exception as exc:
            return TemporalSnapshot(schedule_id, None, error=f"{type(exc).__name__}: {exc}")


def workflow_failure_events(snapshot: TemporalSnapshot) -> list[AnomalyEvent]:
    """Schedule の直近の起動のうち failed で閉じたもの。1実行 = 1 incident。"""
    events = []
    for run in snapshot.runs:
        if run.status != "FAILED":
            continue
        events.append(
            AnomalyEvent(
                category=AnomalyCategory.WORKFLOW_FAILURE,
                incident_key=f"workflow:{run.workflow_id}",
                episode_id=None,
                first_seen_at=run.scheduled_at,
                last_seen_at=run.scheduled_at,
                cause=normalize_cause(run.failure) if run.failure else "unknown (history expired)",
                resolved=False,
            )
        )
    return events


async def load_missed_run_events(
    session: AsyncSession,
    now: datetime,
    snapshot: TemporalSnapshot,
    *,
    tz: str = "Asia/Tokyo",
    run_hour: int = 6,
    grace_minutes: int = 30,
) -> list[AnomalyEvent]:
    """最初の slot の日から今日まで、slot も Episode も無い日（Episode を作れなかった日）。"""
    await _read_only(session)
    zone = ZoneInfo(tz)
    slots = {
        _d(r[0])
        for r in (await session.execute(text("SELECT slot_date FROM daily_episode_slots"))).all()
    }
    created = {
        _utc(r[0]).astimezone(zone).date()
        for r in (await session.execute(text("SELECT created_at FROM episodes"))).all()
    }
    if not slots:
        return []
    local_now = now.astimezone(zone)
    last_day = local_now.date()
    if local_now.time() < time(run_hour, grace_minutes):
        last_day -= timedelta(days=1)
    failed: Mapping[date, ScheduleRun] = {
        r.scheduled_at.astimezone(zone).date(): r for r in snapshot.runs if r.status == "FAILED"
    }
    events: list[AnomalyEvent] = []
    day = min(slots)
    while day <= last_day:
        if day not in slots and day not in created:
            at = datetime.combine(day, time(run_hour), tzinfo=zone).astimezone(UTC)
            run = failed.get(day)
            if run is not None:
                cause = "workflow failed before slot: " + (
                    normalize_cause(run.failure) if run.failure else "unknown (history expired)"
                )
            else:
                cause = "no slot and no episode (cause not in retained history)"
            events.append(
                AnomalyEvent(
                    category=AnomalyCategory.MISSED_SCHEDULE_RUN,
                    incident_key=f"missed:{day.isoformat()}",
                    episode_id=None,
                    first_seen_at=at,
                    last_seen_at=at,
                    cause=cause,
                    resolved=False,
                )
            )
        day += timedelta(days=1)
    return events


def _eid(value: Any) -> str:
    return str(uuid.UUID(str(value)))


def _d(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


DAILY_SCHEDULE_ID = "avp-daily-episode"


async def collect_report(
    session: AsyncSession,
    now: datetime,
    reader: TemporalReader | None = None,
    *,
    schedule_id: str = DAILY_SCHEDULE_ID,
) -> dict[str, Any]:
    """全カテゴリを読んで窓付き・重複除去済みのレポートにする。Temporal に届かなくても返す。"""
    from domain.anomaly_report import build_report

    snapshot = (
        reader.snapshot(schedule_id)
        if reader is not None
        else TemporalSnapshot(schedule_id, None, error="temporal reader not configured")
    )
    events = [
        *await load_watchdog_events(session),
        *await load_job_failure_events(session),
        *workflow_failure_events(snapshot),
        *await load_missed_run_events(session, now, snapshot),
    ]
    report = build_report(events, now)
    report["temporal"] = {
        "schedule_id": snapshot.schedule_id,
        "paused": snapshot.paused,
        "error": snapshot.error,
        "recent_runs": [
            {
                "scheduled_at": r.scheduled_at.isoformat(),
                "status": r.status,
                "workflow_id": r.workflow_id,
            }
            for r in snapshot.runs
        ],
        "next_runs": [t.isoformat() for t in snapshot.next_runs[:3]],
        "running_workflows": list(snapshot.running_workflows),
    }
    return report


async def recurring_unresolved_by_category(
    session: AsyncSession, now: datetime, reader: TemporalReader | None = None
) -> dict[str, int]:
    """release preflight が使う入口: 直近 24h に再検出された未解決 incident 数（カテゴリ別）。"""
    from domain.anomaly_report import recurring_unresolved_count

    return recurring_unresolved_count(await collect_report(session, now, reader))
