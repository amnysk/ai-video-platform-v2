"""Schedule 操作の fake（Temporal に繋がない）。実際の Temporal と同じく、paused の Schedule は
next_run を返さず、pause / unpause は note を置き換える。"""

from __future__ import annotations

from datetime import datetime, timedelta

from infrastructure.temporal.schedules import ScheduleSnapshot


class FakeScheduleControl:
    def __init__(
        self,
        *,
        exists: bool = True,
        paused: bool = False,
        note: str | None = "avp: daily episode pipeline (ADR-0023)",
        clock: list[datetime],
        next_run_after: timedelta = timedelta(hours=10),
    ) -> None:
        self.exists = exists
        self.paused = paused
        self.note = note
        self.clock = clock  # 1要素のリスト。テストが時刻を進める
        self.next_run_after = next_run_after
        self.calls: list[tuple[str, str]] = []
        #: unpause しても paused のままになる故障を模す
        self.unpause_is_ignored = False

    async def describe(self, schedule_id: str) -> ScheduleSnapshot:
        if not self.exists:
            return ScheduleSnapshot(exists=False)
        return ScheduleSnapshot(
            exists=True,
            paused=self.paused,
            note=self.note,
            next_run=None if self.paused else self.clock[0] + self.next_run_after,
        )

    async def pause(self, schedule_id: str, note: str) -> None:
        self.calls.append(("pause", note))
        self.paused = True
        self.note = note

    async def unpause(self, schedule_id: str, note: str) -> None:
        self.calls.append(("unpause", note))
        if self.unpause_is_ignored:
            return
        self.paused = False
        self.note = note
