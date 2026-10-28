"""BGM の純粋ドメイン（ADR-0072）。contracts/ だけを import する。I/O・時計を持たない。"""

from domain.music.credits import CreditsError, CreditsFit, credits_for, fit_credits
from domain.music.ingest import IngestDecision, IngestOutcome, IngestRejectReason, ingest_check
from domain.music.selection import (
    Rejection,
    RejectReason,
    SelectionResult,
    reject_reasons,
    select_approved,
)

__all__ = [
    "CreditsError",
    "CreditsFit",
    "IngestDecision",
    "IngestOutcome",
    "IngestRejectReason",
    "RejectReason",
    "Rejection",
    "SelectionResult",
    "credits_for",
    "fit_credits",
    "ingest_check",
    "reject_reasons",
    "select_approved",
]
