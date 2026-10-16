"""Research の外部呼び出し台帳の1行の状態（ADR-0037 §4 / INV-36）。純粋関数のみ。

- ``reserved → spent``: 呼んだ（成否にかかわらず。送った呼び出しは課金されたものとして数える）
- ``reserved → abandoned``: **送っていないことが確か**（``dispatched_at`` が無い）なときだけ
- ``spent`` / ``abandoned`` は終端。どの状態の行も枠を数え、``call_seq`` は再利用しない

送ったかもしれない行（dispatch 済みで結果が無い）を ``abandoned`` にしない。
予約台帳（ADR-0013 / INV-15）と同じ保守的な扱い。
"""

from __future__ import annotations

from enum import StrEnum

from contracts.research import ResearchCallStatus
from domain.episode.transitions import Rejected

__all__ = ["CALL_TRANSITIONS", "CallEvent", "transition_call"]


class CallEvent(StrEnum):
    SPENT = "spent"
    ABANDONED = "abandoned"


CALL_TRANSITIONS: dict[tuple[ResearchCallStatus, CallEvent], ResearchCallStatus] = {
    (ResearchCallStatus.RESERVED, CallEvent.SPENT): ResearchCallStatus.SPENT,
    (ResearchCallStatus.RESERVED, CallEvent.ABANDONED): ResearchCallStatus.ABANDONED,
}


def transition_call(
    current: ResearchCallStatus, event: CallEvent, *, dispatched: bool
) -> ResearchCallStatus | Rejected:
    target = CALL_TRANSITIONS.get((current, event))
    if target is None:
        return Rejected(
            reason=f"research call transition rejected: {current.value} + {event.value}"
        )
    if target is ResearchCallStatus.ABANDONED and dispatched:
        return Rejected(
            reason="a dispatched research call may have been sent; it cannot be abandoned"
        )
    return target
