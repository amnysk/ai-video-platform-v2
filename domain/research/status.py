"""ResearchRequest の状態機械（ADR-0037 §3）。純粋関数のみ。表はここに 1 つだけ置く。

Episode の表（``domain/episode/transitions.py``、INV-8）とは**別**。リポジトリは必ず
``transition_research`` を通す。

- ``queued → running``、``running → completed | partial | blocked | failed``
- ``blocked → queued``（人が原因を直して再開）
- 終端は ``completed`` / ``partial`` / ``failed``。``partial`` は**合格ではない**
- 呼び出し前に止める ``blocked``（Provider 未設定・予算未設定）も ``running`` を経由する
  （開始した記録を残す）。外部呼び出しは ``running`` の間にしか予約できない
"""

from __future__ import annotations

from enum import StrEnum

from contracts.research import ResearchStatus
from domain.episode.transitions import Rejected

__all__ = [
    "RESEARCH_TERMINAL_STATUSES",
    "RESEARCH_TRANSITIONS",
    "ResearchEvent",
    "transition_research",
]


class ResearchEvent(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    #: 人が原因を直して再開した（blocked → queued）
    RESUMED = "resumed"


RESEARCH_TRANSITIONS: dict[tuple[ResearchStatus, ResearchEvent], ResearchStatus] = {
    (ResearchStatus.QUEUED, ResearchEvent.STARTED): ResearchStatus.RUNNING,
    (ResearchStatus.RUNNING, ResearchEvent.COMPLETED): ResearchStatus.COMPLETED,
    (ResearchStatus.RUNNING, ResearchEvent.PARTIAL): ResearchStatus.PARTIAL,
    (ResearchStatus.RUNNING, ResearchEvent.BLOCKED): ResearchStatus.BLOCKED,
    (ResearchStatus.RUNNING, ResearchEvent.FAILED): ResearchStatus.FAILED,
    (ResearchStatus.BLOCKED, ResearchEvent.RESUMED): ResearchStatus.QUEUED,
}

#: 終端。ここから出る辺は表に無い。
RESEARCH_TERMINAL_STATUSES: frozenset[ResearchStatus] = frozenset(
    {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL, ResearchStatus.FAILED}
)


def transition_research(current: ResearchStatus, event: ResearchEvent) -> ResearchStatus | Rejected:
    target = RESEARCH_TRANSITIONS.get((current, event))
    if target is None:
        return Rejected(reason=f"research transition rejected: {current.value} + {event.value}")
    return target
