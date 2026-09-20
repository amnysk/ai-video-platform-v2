"""ガードを**実 Temporal** の使い捨て Schedule に対して確かめる（ADR-0027）。

fake では言い切れないこと: 実サーバの paused Schedule が next_action_times を返さないこと、
pause / unpause の note が describe に出ること、update が pause を保つこと。

本番 namespace（``default`` とアプリの ``TEMPORAL_NAMESPACE``）では動かさない。
``TEMPORAL_ADDRESS`` と ``TEST_TEMPORAL_NAMESPACE``（``default`` 以外）が必要。
Schedule はテスト内で作り、最後に削除する（存在しない task queue を指すので何も起動しない）。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from temporalio.client import Client

from contracts.pipeline import DailyEpisodeInput
from contracts.schedule_guard import ScheduleHealth
from domain.schedule_guard import parse_maintenance_note
from infrastructure.temporal.schedule_guard import (
    EXIT_EMERGENCY_PAUSED,
    EXIT_OK,
    begin_maintenance,
    end_maintenance,
    reconcile_schedule,
)
from infrastructure.temporal.schedules import (
    TemporalScheduleControl,
    ensure_daily_episode_schedule,
)

ADDRESS = os.environ.get("TEMPORAL_ADDRESS")
NAMESPACE = os.environ.get("TEST_TEMPORAL_NAMESPACE")
APP_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")

pytestmark = pytest.mark.skipif(
    not ADDRESS or not NAMESPACE or NAMESPACE in {"default", APP_NAMESPACE},
    reason="TEMPORAL_ADDRESS and TEST_TEMPORAL_NAMESPACE (not default/app namespace) required",
)


async def test_guard_round_trip_against_a_real_schedule() -> None:
    client = await Client.connect(ADDRESS or "", namespace=NAMESPACE or "")
    schedule_id = f"guard-test-{uuid.uuid4().hex[:8]}"
    queue = f"nobody-listens-{uuid.uuid4().hex[:8]}"
    control = TemporalScheduleControl(client)
    try:
        await ensure_daily_episode_schedule(
            client,
            schedule_id=schedule_id,
            cron="0 6 * * *",
            timezone="Asia/Tokyo",
            daily_limit=1,
            workflow_input=DailyEpisodeInput(),
            task_queue=queue,
        )
        now = datetime.now(UTC)
        healthy = await control.describe(schedule_id)
        assert healthy.exists and not healthy.paused and healthy.next_run is not None

        begun = await begin_maintenance(
            control, schedule_id, reason="integration", ttl_seconds=600, now=now
        )
        assert begun.exit_code == EXIT_OK
        paused = await control.describe(schedule_id)
        assert paused.paused is True
        assert parse_maintenance_note(paused.note) is not None

        # 定義の更新（ensure --apply）は pause を外さない
        await ensure_daily_episode_schedule(
            client,
            schedule_id=schedule_id,
            cron="0 6 * * *",
            timezone="Asia/Tokyo",
            daily_limit=1,
            workflow_input=DailyEpisodeInput(),
            task_queue=queue,
        )
        assert (await control.describe(schedule_id)).paused is True

        ended = await end_maintenance(control, schedule_id, now=datetime.now(UTC))
        assert ended.exit_code == EXIT_OK
        assert ended.health is ScheduleHealth.HEALTHY

        # 運用者の pause は begin / end / reconcile のどれでも動かない
        await control.pause(schedule_id, "operator: stop")
        assert (
            await begin_maintenance(
                control, schedule_id, reason="x", ttl_seconds=60, now=datetime.now(UTC)
            )
        ).exit_code == EXIT_EMERGENCY_PAUSED
        assert (
            await end_maintenance(control, schedule_id, now=datetime.now(UTC))
        ).exit_code == EXIT_EMERGENCY_PAUSED
        rec = await reconcile_schedule(control, schedule_id, now=datetime.now(UTC))
        assert rec.health is ScheduleHealth.PAUSED_UNEXPECTEDLY and not rec.released
        assert (await control.describe(schedule_id)).paused is True
    finally:
        await client.get_schedule_handle(schedule_id).delete()
