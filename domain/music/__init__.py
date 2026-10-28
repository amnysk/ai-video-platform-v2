"""BGM の純粋ドメイン（ADR-0072）。contracts/ だけを import する。I/O・時計を持たない。"""

from domain.music.selection import (
    RejectReason,
    Rejection,
    SelectionResult,
    reject_reasons,
    select_approved,
)

__all__ = ["RejectReason", "Rejection", "SelectionResult", "reject_reasons", "select_approved"]
