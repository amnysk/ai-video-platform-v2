#!/usr/bin/env python
"""Daily Schedule（ADR-0023）を表示・登録・一時停止する。

    python scripts/ensure-daily-schedule.py            # 既定は dry-run（登録内容を表示するだけ）
    python scripts/ensure-daily-schedule.py --apply    # create-or-update（冪等）
    python scripts/ensure-daily-schedule.py --pause    # Schedule を一時停止（登録済みのもの）
    python scripts/ensure-daily-schedule.py --unpause   # 運用者の判断で解除する
    python scripts/ensure-daily-schedule.py --watchdog [--apply]   # watchdog（ADR-0027）

``--apply`` は既存 Schedule の pause（note を含む）を外さない（ADR-0027）。

設定は ``Settings``（DAILY_EPISODE_LIMIT / DAILY_SCHEDULE_CRON / SCHEDULE_TIMEZONE /
DAILY_SCHEDULE_ID / PIPELINE_RENDER_PROFILE_ID / TEMPORAL_ADDRESS）。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from temporalio.client import Client  # noqa: E402

from contracts.schedule_guard import WATCHDOG_SCHEDULE_ID, WatchdogRequest  # noqa: E402
from infrastructure.config import Settings  # noqa: E402
from infrastructure.temporal.schedules import (  # noqa: E402
    build_watchdog_schedule,
    daily_episode_input_from_settings,
    describe_daily_schedule_spec,
    ensure_daily_episode_schedule,
    ensure_watchdog_schedule,
)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="表示だけ（既定）")
    mode.add_argument("--apply", action="store_true", help="create-or-update")
    mode.add_argument("--pause", action="store_true")
    mode.add_argument("--unpause", action="store_true")
    parser.add_argument("--paused", action="store_true", help="--apply で paused のまま登録する")
    parser.add_argument(
        "--watchdog", action="store_true", help="daily の代わりに watchdog の Schedule を扱う"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if args.watchdog:
        return await _watchdog(settings, apply=args.apply)
    wf_input = daily_episode_input_from_settings(settings)
    schedule_id = settings.daily_schedule_id
    print(
        describe_daily_schedule_spec(
            schedule_id=schedule_id,
            cron=settings.daily_schedule_cron,
            timezone=settings.schedule_timezone,
            workflow_input=wf_input,
            paused=args.paused,
        )
    )
    if not (args.apply or args.pause or args.unpause):
        print("dry-run: nothing registered (use --apply)")
        return 0

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    if args.apply:
        outcome = await ensure_daily_episode_schedule(
            client,
            schedule_id=schedule_id,
            cron=settings.daily_schedule_cron,
            timezone=settings.schedule_timezone,
            daily_limit=settings.daily_episode_limit,
            workflow_input=wf_input,
            paused=args.paused,
        )
        print(f"schedule {schedule_id}: {outcome}")
    elif args.pause:
        await client.get_schedule_handle(schedule_id).pause(note="paused by operator")
        print(f"schedule {schedule_id}: paused")
    else:
        await client.get_schedule_handle(schedule_id).unpause(note="unpaused by operator")
        print(f"schedule {schedule_id}: unpaused")
    return 0


async def _watchdog(settings: Settings, *, apply: bool) -> int:
    request = WatchdogRequest(
        schedule_id=settings.daily_schedule_id,
        cron=settings.daily_schedule_cron,
        timezone=settings.schedule_timezone,
        grace_seconds=settings.watchdog_grace_seconds,
        stage_stall_grace_minutes=settings.stage_stall_grace_minutes,
        completion_deadline_hours=settings.completion_deadline_hours,
        upload_deadline_hours=settings.upload_deadline_hours,
    )
    schedule = build_watchdog_schedule(
        cron=settings.watchdog_cron, timezone=settings.schedule_timezone, request=request
    )
    print(
        f"schedule {WATCHDOG_SCHEDULE_ID}: cron={settings.watchdog_cron} "
        f"tz={settings.schedule_timezone} watches={request.schedule_id} "
        f"(daily cron {request.cron}, grace {request.grace_seconds}s) "
        f"overlap={schedule.policy.overlap.name}"
    )
    if not apply:
        print("dry-run: nothing registered (use --watchdog --apply)")
        return 0
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    outcome = await ensure_watchdog_schedule(
        client,
        cron=settings.watchdog_cron,
        timezone=settings.schedule_timezone,
        request=request,
    )
    print(f"schedule {WATCHDOG_SCHEDULE_ID}: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
