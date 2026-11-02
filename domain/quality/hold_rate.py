"""品質ゲートの保留率の警報（ADR-0087 §11）。純粋: I/O・時計を持たない。

直近 ``window`` 件の gate の判定のうち ``threshold`` 件以上が保留（許可されなかった）なら警報。
保留の原因を「品質」と「検査系の故障」に分けて数える（INV-156）。警報は**通報だけ**で、
判定・Episode の状態には一切触れない（緩める経路を作らない）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from contracts.quality import QualityReason, QualityVerdict

DEFAULT_WINDOW = 10
DEFAULT_THRESHOLD = 5

#: 品質の判定ではなく、検査系・保存系の故障が原因の拒否の理由。**唯一の定義**。
INSPECTION_CAUSE_REASONS: frozenset[QualityReason] = frozenset(
    {
        QualityReason.INSPECTION_UNAVAILABLE,
        QualityReason.REPORT_EVALUATION_ERROR,
        QualityReason.REPORT_PRODUCTION_FAILED,
    }
)


class HoldCause(StrEnum):
    QUALITY = "quality"
    INSPECTION = "inspection"


@dataclass(frozen=True, slots=True)
class GateDecisionSample:
    """gate の 1 回の判定（許可されたか・理由）。"""

    allowed: bool
    reason: QualityReason


def cause_of(sample: GateDecisionSample) -> HoldCause | None:
    """保留の原因。許可なら ``None``。"""
    if sample.allowed:
        return None
    return HoldCause.INSPECTION if sample.reason in INSPECTION_CAUSE_REASONS else HoldCause.QUALITY


def sample_from_report(verdict: QualityVerdict, reason_codes: Iterable[str]) -> GateDecisionSample:
    """保存済みの報告（verdict と各検査の理由コード）から判定を復元する。

    ``QualityGateService`` と同じ区別: 保留の検査が全て ``inspection_unavailable`` のときだけ
    検査系。読めない理由コードは品質の保留として数える。
    """
    if verdict in (QualityVerdict.PASS, QualityVerdict.WARN):
        return GateDecisionSample(
            True,
            QualityReason.UPLOAD_ALLOWED
            if verdict is QualityVerdict.PASS
            else QualityReason.UPLOAD_ALLOWED_WITH_WARNINGS,
        )
    codes = list(reason_codes)
    non_ok = [c for c in codes if c != QualityReason.OK.value]
    if (
        verdict is QualityVerdict.REVIEW
        and non_ok
        and all(c == QualityReason.INSPECTION_UNAVAILABLE.value for c in non_ok)
    ):
        return GateDecisionSample(False, QualityReason.INSPECTION_UNAVAILABLE)
    return GateDecisionSample(
        False,
        QualityReason.REPORT_BLOCKED
        if verdict is QualityVerdict.BLOCK
        else QualityReason.REPORT_NEEDS_REVIEW,
    )


@dataclass(frozen=True, slots=True)
class HoldAlarm:
    window: int
    threshold: int
    holds: int
    quality_holds: int
    inspection_holds: int

    def to_detail(self) -> dict[str, int]:
        return {
            "window": self.window,
            "threshold": self.threshold,
            "holds": self.holds,
            "quality_holds": self.quality_holds,
            "inspection_holds": self.inspection_holds,
        }


def recent_hold_alarm(
    decisions: Sequence[GateDecisionSample],
    *,
    window: int = DEFAULT_WINDOW,
    threshold: int = DEFAULT_THRESHOLD,
) -> HoldAlarm | None:
    """``decisions`` は古い順。最後の ``window`` 件に ``threshold`` 件以上の保留があれば警報。"""
    if window < 1 or threshold < 1 or threshold > window:
        raise ValueError("need 1 <= threshold <= window")
    recent = list(decisions)[-window:]
    causes = [c for c in (cause_of(s) for s in recent) if c is not None]
    if len(causes) < threshold:
        return None
    return HoldAlarm(
        window=window,
        threshold=threshold,
        holds=len(causes),
        quality_holds=causes.count(HoldCause.QUALITY),
        inspection_holds=causes.count(HoldCause.INSPECTION),
    )
