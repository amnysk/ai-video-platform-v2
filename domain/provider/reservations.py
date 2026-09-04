"""予約台帳の状態機械（ADR-0013）。純粋関数のみ。I/O をここに書かない。

``domain/job/transitions.py`` と同じ形で書く。表は1つだけ置き、
リポジトリは必ず ``transition_reservation`` を通す。

**``reserved`` から自動事象で出る辺を表に載せない。**
これ自体が INV-15 の「未照合の予約を自動で再送も解放もしない」の機械的表現であり、
``OPERATOR_*`` は人手照合専用の事象である。
"""

from __future__ import annotations

from enum import StrEnum

from contracts.states import RESERVATION_TERMINAL_STATUSES, ReservationStatus
from domain.episode.transitions import Rejected

__all__ = [
    "RESERVATION_TERMINAL_STATUSES",
    "RESERVATION_TRANSITIONS",
    "ReservationEvent",
    "transition_reservation",
]


class ReservationEvent(StrEnum):
    """予約の状態を進める事象。

    ``EVIDENCE_RECONCILED`` だけが自動で起こりうるが、これは
    「生出力（``provider-raw/``）が実際に見つかった」という**証拠**が
    前提であり、証拠なしに ``reserved`` を出る事象は存在しない。
    """

    EVIDENCE_RECONCILED = "evidence_reconciled"
    OPERATOR_CONFIRMED_SPENT = "operator_confirmed_spent"
    OPERATOR_ABANDONED = "operator_abandoned"


RESERVATION_TRANSITIONS: dict[tuple[ReservationStatus, ReservationEvent], ReservationStatus] = {
    (ReservationStatus.RESERVED, ReservationEvent.EVIDENCE_RECONCILED): ReservationStatus.SPENT,
    (
        ReservationStatus.RESERVED,
        ReservationEvent.OPERATOR_CONFIRMED_SPENT,
    ): ReservationStatus.SPENT,
    (ReservationStatus.RESERVED, ReservationEvent.OPERATOR_ABANDONED): ReservationStatus.ABANDONED,
    # 終端（spent / abandoned）から出る辺は無い。台帳は課金の履歴なので巻き戻さない。
}


def transition_reservation(
    current: ReservationStatus, event: ReservationEvent
) -> ReservationStatus | Rejected:
    target = RESERVATION_TRANSITIONS.get((current, event))
    if target is None:
        return Rejected(reason=f"reservation transition rejected: {current.value} + {event.value}")
    return target
