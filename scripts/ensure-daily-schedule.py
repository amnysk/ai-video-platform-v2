#!/usr/bin/env python
"""Daily Schedule（ADR-0023）を表示・登録・一時停止する。

    python scripts/ensure-daily-schedule.py            # 既定は dry-run（登録内容を表示するだけ）
    python scripts/ensure-daily-schedule.py --apply    # create-or-update（冪等）
    python scripts/ensure-daily-schedule.py --pause    # Schedule を一時停止（登録済みのもの）
    python scripts/ensure-daily-schedule.py --unpause

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

from infrastructure.config import Settings  # noqa: E402
from infrastructure.temporal.schedules import (  # noqa: E402
    daily_episode_input_from_settings,
    describe_daily_schedule_spec,
    ensure_daily_episode_schedule,
)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="表示だけ（既定）")
    mode.add_argument("--apply", action="store_true", help="create-or-update")
    mode.add_argument("--pause", action="store_true")
    mode.add_argument("--unpause", action="store_true")
    parser.add_argument("--paused", action="store_true", help="--apply で paused のまま登録する")
    args = parser.parse_args(argv)

    settings = Settings()
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


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
