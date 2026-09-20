"""Daily Schedule の登録（ADR-0023）。

INV-2: スケジューラは **Temporal の Schedule / workflow start だけ**を行う。ここは Schedule を
作る・更新するだけで、Activity や worker 関数を呼ばない。cron のループをプロセス内に持たない。

登録は冪等（create-or-update）。同じ id の Schedule を二つ作らない。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from contracts.pipeline import (
    DAILY_EPISODE_WORKFLOW,
    DAILY_WORKFLOW_ID_PREFIX,
    DEFAULT_SCHEDULE_CATCHUP_WINDOW_SECONDS,
    DailyEpisodeInput,
    PipelineOptions,
    ProductionParameters,
)
from contracts.schedule_guard import (
    WATCHDOG_SCHEDULE_ID,
    WATCHDOG_WORKFLOW,
    WATCHDOG_WORKFLOW_ID_PREFIX,
    WatchdogRequest,
)
from infrastructure.config import Settings

SCHEDULE_NOTE = "avp: daily episode pipeline (ADR-0023)"


def daily_episode_input_from_settings(settings: Settings) -> DailyEpisodeInput:
    """設定から Schedule の入力を組む。production の枠は API 起動と同じ設定を使う。"""
    return DailyEpisodeInput(
        daily_limit=settings.daily_episode_limit,
        timezone=settings.schedule_timezone,
        strategy_profile_id=settings.topic_strategy_profile_id,
        content_profile_id=settings.topic_content_profile_id,
        options=PipelineOptions(
            render_profile_id=settings.pipeline_render_profile_id,
            production=ProductionParameters(
                image_concurrency=settings.image_concurrency,
                video_concurrency=settings.video_concurrency,
                voice_concurrency=settings.voice_concurrency,
                image_max_rounds=settings.production_image_max_rounds,
                video_max_rounds=settings.production_video_max_rounds,
                await_reexecutions=settings.production_await_reexecutions,
            ),
        ),
    )


def build_daily_episode_schedule(
    *,
    cron: str,
    timezone: str,
    workflow_input: DailyEpisodeInput,
    paused: bool = False,
    task_queue: str | None = None,
    workflow_id_prefix: str = DAILY_WORKFLOW_ID_PREFIX,
) -> Schedule:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {timezone!r}") from exc
    if workflow_input.timezone != timezone:
        # slot_date の日付と cron の解釈を同じタイムゾーンに揃える
        workflow_input.timezone = timezone
    workflow_name, default_queue = DAILY_EPISODE_WORKFLOW
    return Schedule(
        action=ScheduleActionStartWorkflow(
            workflow_name,
            workflow_input,
            id=workflow_id_prefix,
            task_queue=task_queue or default_queue,
        ),
        spec=ScheduleSpec(cron_expressions=[cron], time_zone_name=timezone),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=timedelta(seconds=DEFAULT_SCHEDULE_CATCHUP_WINDOW_SECONDS),
        ),
        state=ScheduleState(note=SCHEDULE_NOTE, paused=paused),
    )


async def ensure_daily_episode_schedule(
    client: Client,
    *,
    schedule_id: str,
    cron: str,
    timezone: str,
    daily_limit: int,
    workflow_input: DailyEpisodeInput,
    paused: bool = False,
    task_queue: str | None = None,
    workflow_id_prefix: str = DAILY_WORKFLOW_ID_PREFIX,
) -> str:
    """Schedule を作る。既にあれば定義を置き換える。``"created"`` / ``"updated"`` を返す。"""
    workflow_input.daily_limit = daily_limit
    schedule = build_daily_episode_schedule(
        cron=cron,
        timezone=timezone,
        workflow_input=workflow_input,
        paused=paused,
        task_queue=task_queue,
        workflow_id_prefix=workflow_id_prefix,
    )
    try:
        await client.create_schedule(schedule_id, schedule)
        return "created"
    except ScheduleAlreadyRunningError:
        pass

    def _replace(current: ScheduleUpdateInput) -> ScheduleUpdate:
        _keep_pause_state(schedule, current, requested_paused=paused)
        return ScheduleUpdate(schedule=schedule)

    await client.get_schedule_handle(schedule_id).update(_replace)
    return "updated"


def _keep_pause_state(
    schedule: Schedule, current: ScheduleUpdateInput, *, requested_paused: bool
) -> None:
    """定義の更新で、いまの pause（note を含む）を黙って外さない（ADR-0027）。

    ``--apply`` が emergency pause を解除してしまう事故を防ぐ。paused での登録を明示したときだけ
    新しい state を使う。
    """
    if not requested_paused:
        schedule.state = current.description.schedule.state


def describe_daily_schedule_spec(
    *,
    schedule_id: str,
    cron: str,
    timezone: str,
    workflow_input: DailyEpisodeInput,
    paused: bool,
    task_queue: str | None = None,
) -> str:
    """登録内容を人が読める JSON にする（dry-run 用。secret は入力に載らない / INV-20）。"""
    workflow_name, default_queue = DAILY_EPISODE_WORKFLOW
    return json.dumps(
        {
            "schedule_id": schedule_id,
            "cron": cron,
            "timezone": timezone,
            "paused": paused,
            "overlap": "SKIP",
            "catchup_window_seconds": DEFAULT_SCHEDULE_CATCHUP_WINDOW_SECONDS,
            "workflow": workflow_name,
            "task_queue": task_queue or default_queue,
            "workflow_id_prefix": DAILY_WORKFLOW_ID_PREFIX,
            "input": asdict(workflow_input),
        },
        ensure_ascii=False,
        indent=2,
    )


# --------------------------------------------------------------------- 状態の読み書き（ADR-0027）


@dataclass(frozen=True, slots=True)
class ScheduleSnapshot:
    """Schedule の状態のうちガードが見るもの。"""

    exists: bool
    paused: bool = False
    note: str | None = None
    #: 次回実行（UTC）。paused のときは Temporal が返さないので ``None``
    next_run: datetime | None = None


class TemporalScheduleControl:
    """Schedule の describe / pause / unpause。Schedule を触る API はここに閉じる（INV-2）。"""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def describe(self, schedule_id: str) -> ScheduleSnapshot:
        try:
            description = await self._client.get_schedule_handle(schedule_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return ScheduleSnapshot(exists=False)
            raise
        state = description.schedule.state
        upcoming = description.info.next_action_times
        return ScheduleSnapshot(
            exists=True,
            paused=state.paused,
            note=state.note,
            next_run=upcoming[0] if upcoming else None,
        )

    async def pause(self, schedule_id: str, note: str) -> None:
        await self._client.get_schedule_handle(schedule_id).pause(note=note)

    async def unpause(self, schedule_id: str, note: str) -> None:
        await self._client.get_schedule_handle(schedule_id).unpause(note=note)


# --------------------------------------------------------------------- watchdog Schedule


def build_watchdog_schedule(
    *,
    cron: str,
    timezone: str,
    request: WatchdogRequest,
    task_queue: str | None = None,
) -> Schedule:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {timezone!r}") from exc
    workflow_name, default_queue = WATCHDOG_WORKFLOW
    return Schedule(
        action=ScheduleActionStartWorkflow(
            workflow_name,
            request,
            id=WATCHDOG_WORKFLOW_ID_PREFIX,
            task_queue=task_queue or default_queue,
            execution_timeout=timedelta(minutes=30),
        ),
        spec=ScheduleSpec(cron_expressions=[cron], time_zone_name=timezone),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(minutes=30)
        ),
        state=ScheduleState(note="avp: daily watchdog (ADR-0027)", paused=False),
    )


async def ensure_watchdog_schedule(
    client: Client,
    *,
    cron: str,
    timezone: str,
    request: WatchdogRequest,
    schedule_id: str = WATCHDOG_SCHEDULE_ID,
) -> str:
    """watchdog の Schedule を create-or-update する。``"created"`` / ``"updated"``。"""
    schedule = build_watchdog_schedule(cron=cron, timezone=timezone, request=request)
    try:
        await client.create_schedule(schedule_id, schedule)
        return "created"
    except ScheduleAlreadyRunningError:
        pass

    def _replace(current: ScheduleUpdateInput) -> ScheduleUpdate:
        _keep_pause_state(schedule, current, requested_paused=False)
        return ScheduleUpdate(schedule=schedule)

    await client.get_schedule_handle(schedule_id).update(_replace)
    return "updated"
