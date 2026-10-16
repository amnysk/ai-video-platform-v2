#!/usr/bin/env python
"""Daily Schedule のガード（ADR-0027）。maintenance pause の開始・終了・reconcile・状態表示。

    python scripts/schedule-guard.py status [--json]
    python scripts/schedule-guard.py maintenance begin --reason "deploy-workers" --ttl 45m
    python scripts/schedule-guard.py maintenance end
    python scripts/schedule-guard.py reconcile

終了コード: 0 = 正常 / 1 = 失敗（describe で期待の状態を確認できない） /
3 = 運用者の pause（印なし）が有効なので何もしなかった。

**解除してよいのはガードの印がある pause だけ**。運用者が pause した Schedule は
begin / end / reconcile のどれでも触らない。deploy からは ``scripts/with-maintenance-pause.sh``
経由で使う（途中で失敗しても trap で end を呼ぶ）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from temporalio.client import Client  # noqa: E402

from contracts.schedule_guard import (  # noqa: E402
    DEFAULT_MAINTENANCE_TTL_SECONDS,
    WATCHDOG_SCHEDULE_ID,
    ScheduleHealth,
)
from domain.schedule_guard import parse_maintenance_note  # noqa: E402
from infrastructure.config import Settings  # noqa: E402
from infrastructure.temporal.schedule_guard import (  # noqa: E402
    EXIT_EMERGENCY_PAUSED,
    EXIT_FAILED,
    EXIT_OK,
    begin_maintenance,
    end_maintenance,
    reconcile_schedule,
)
from infrastructure.temporal.schedules import TemporalScheduleControl  # noqa: E402

_TTL = re.compile(r"^(\d+)([smh]?)$")


def parse_ttl(value: str) -> int:
    match = _TTL.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid ttl: {value!r} (例: 45m, 2h, 600)")
    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


async def _watchdog_line(control: TemporalScheduleControl) -> str:
    snap = await control.describe(WATCHDOG_SCHEDULE_ID)
    if not snap.exists:
        return (
            f"WARN: watchdog schedule {WATCHDOG_SCHEDULE_ID} is not registered "
            "(ensure-daily-schedule.py --watchdog --apply)"
        )
    if snap.paused:
        return f"WARN: watchdog schedule {WATCHDOG_SCHEDULE_ID} is paused; automation unmonitored"
    return f"watchdog schedule {WATCHDOG_SCHEDULE_ID}: running"


async def _status(settings: Settings, control: TemporalScheduleControl, as_json: bool) -> int:
    from infrastructure.observability.anomaly_notifier import LoggingAnomalyNotifier
    from infrastructure.temporal.schedule_guard import classify_snapshot
    from workers.pipeline.activities import PipelineActivities

    now = datetime.now(UTC)
    report: dict[str, object] = {"now": now.isoformat()}
    # ADR-0031 §3: ログのみが唯一の通知経路であることを診断で明示する（新フラグは増やさない）
    is_log_only = PipelineActivities.notifier_factory is LoggingAnomalyNotifier
    report["notifier"] = "log_only" if is_log_only else "configured"
    exit_code = EXIT_OK
    for key, schedule_id in (
        ("daily", settings.daily_schedule_id),
        ("watchdog", WATCHDOG_SCHEDULE_ID),
    ):
        snap = await control.describe(schedule_id)
        health = classify_snapshot(snap, now)
        marker = parse_maintenance_note(snap.note)
        report[key] = {
            "schedule_id": schedule_id,
            "exists": snap.exists,
            "paused": snap.paused,
            "health": health.value,
            "maintenance": None
            if marker is None
            else {"reason": marker.reason, "deadline": marker.deadline},
            "next_run": snap.next_run.isoformat() if snap.next_run else None,
        }
        if key == "daily" and health not in (
            ScheduleHealth.HEALTHY,
            ScheduleHealth.MAINTENANCE_IN_PROGRESS,
        ):
            exit_code = EXIT_FAILED
    try:
        from contracts.operations import OperationalSwitch
        from infrastructure.db.repositories import (
            OperationalAnomalyRepository,
            OperationalSwitchRepository,
        )
        from infrastructure.db.session import session_factory_from_settings

        async with session_factory_from_settings(settings)() as session:
            switches = OperationalSwitchRepository(session)
            report["switches"] = {
                "paused": {
                    "db": await switches.is_on(OperationalSwitch.PAUSED),
                    "env": settings.paused,
                },
                "uploads_paused": {
                    "db": await switches.is_on(OperationalSwitch.UPLOADS_PAUSED),
                    "env": settings.uploads_paused,
                },
            }
            open_rows = await OperationalAnomalyRepository(session).list_open()
            report["open_anomalies"] = [
                {
                    "kind": r.kind,
                    "date": r.anomaly_date.isoformat(),
                    "occurrences": r.occurrences,
                    "episode_id": str(r.episode_id) if r.episode_id else None,
                }
                for r in open_rows
            ]
            if open_rows:
                exit_code = EXIT_FAILED
    except Exception as exc:  # DB に届かなくても Schedule の状態は出す
        report["db"] = f"unavailable ({type(exc).__name__})"
        exit_code = EXIT_FAILED
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for key, value in report.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return exit_code


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    maint = sub.add_parser("maintenance")
    maint_sub = maint.add_subparsers(dest="action", required=True)
    begin = maint_sub.add_parser("begin")
    begin.add_argument("--reason", required=True)
    begin.add_argument("--ttl", type=parse_ttl, default=DEFAULT_MAINTENANCE_TTL_SECONDS)
    maint_sub.add_parser("end")
    sub.add_parser("reconcile")
    args = parser.parse_args(argv)

    settings = Settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    control = TemporalScheduleControl(client)
    schedule_id = settings.daily_schedule_id
    now = datetime.now(UTC)

    if args.command == "status":
        return await _status(settings, control, args.json)
    if args.command == "reconcile":
        outcome = await reconcile_schedule(control, schedule_id, now=now)
        print(
            f"reconcile {schedule_id}: before={outcome.health_before.value} "
            f"after={outcome.health.value} released={outcome.released}"
        )
        if outcome.health is ScheduleHealth.PAUSED_UNEXPECTEDLY:
            print("NOTE: paused without a maintenance marker (emergency pause); left untouched")
            return EXIT_EMERGENCY_PAUSED
        if outcome.health in (ScheduleHealth.MISSING, ScheduleHealth.NEXT_RUN_INVALID):
            return EXIT_FAILED
        return EXIT_OK
    if args.action == "begin":
        try:
            result = await begin_maintenance(
                control, schedule_id, reason=args.reason, ttl_seconds=args.ttl, now=now
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_FAILED
    else:
        result = await end_maintenance(control, schedule_id, now=now)
        if result.exit_code == EXIT_OK:
            print(await _watchdog_line(control))
    print(
        f"{result.message} [{result.health.value}]",
        file=sys.stderr if result.exit_code else sys.stdout,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
