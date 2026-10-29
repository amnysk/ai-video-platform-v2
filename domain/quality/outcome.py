"""verdict を既存の Episode 状態の語彙に対応づける（状態は足さない / ADR-0073 §4）。

None は「状態を変えない」。遷移の可否（render_ready からの遷移）は state-transitions.md の
範囲で、配線時の別 ADR で扱う。
"""

from __future__ import annotations

from contracts.quality import QualityVerdict
from contracts.states import EpisodeStatus


def episode_status_for(verdict: QualityVerdict) -> EpisodeStatus | None:
    match verdict:
        case QualityVerdict.PASS | QualityVerdict.WARN:
            return None
        case QualityVerdict.REVIEW:
            return EpisodeStatus.BLOCKED
        case QualityVerdict.BLOCK:
            return EpisodeStatus.NEEDS_WORK
