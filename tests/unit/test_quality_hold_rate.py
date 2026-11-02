"""品質ゲートの保留率の警報（ADR-0087 §11 / INV-156）。純粋部分。"""

from __future__ import annotations

import pytest

from contracts.quality import QualityReason, QualityVerdict
from domain.quality.hold_rate import (
    GateDecisionSample,
    HoldCause,
    cause_of,
    recent_hold_alarm,
    sample_from_report,
)

OK = GateDecisionSample(allowed=True, reason=QualityReason.UPLOAD_ALLOWED)
WARN_OK = GateDecisionSample(allowed=True, reason=QualityReason.UPLOAD_ALLOWED_WITH_WARNINGS)
REVIEW = GateDecisionSample(allowed=False, reason=QualityReason.REPORT_NEEDS_REVIEW)
BLOCK = GateDecisionSample(allowed=False, reason=QualityReason.REPORT_BLOCKED)
UNAVAIL = GateDecisionSample(allowed=False, reason=QualityReason.INSPECTION_UNAVAILABLE)


def test_five_holds_in_the_last_ten_raise_an_alarm_with_a_breakdown() -> None:
    decisions = [OK] * 5 + [REVIEW, BLOCK, UNAVAIL, UNAVAIL, REVIEW]
    alarm = recent_hold_alarm(decisions)
    assert alarm is not None
    assert (alarm.window, alarm.holds, alarm.threshold) == (10, 5, 5)
    assert (alarm.quality_holds, alarm.inspection_holds) == (3, 2)


def test_four_holds_do_not_alarm() -> None:
    assert recent_hold_alarm([OK] * 6 + [REVIEW, BLOCK, UNAVAIL, REVIEW]) is None


def test_only_the_last_ten_count_oldest_first() -> None:
    old_holds = [REVIEW] * 8
    assert recent_hold_alarm(old_holds + [OK] * 10) is None
    assert recent_hold_alarm([OK] * 10 + [REVIEW] * 5) is not None  # 直近 10 件 = OK*5 + REVIEW*5


def test_fewer_than_threshold_decisions_cannot_alarm() -> None:
    assert recent_hold_alarm([REVIEW] * 4) is None
    assert recent_hold_alarm([]) is None


def test_warn_is_an_allowance_not_a_hold() -> None:
    assert recent_hold_alarm([WARN_OK] * 10) is None


def test_custom_window_and_threshold() -> None:
    alarm = recent_hold_alarm([REVIEW, REVIEW, OK], window=3, threshold=2)
    assert alarm is not None and alarm.holds == 2


@pytest.mark.parametrize(("window", "threshold"), [(0, 1), (5, 0), (5, 6)])
def test_invalid_parameters_are_rejected(window: int, threshold: int) -> None:
    with pytest.raises(ValueError):
        recent_hold_alarm([], window=window, threshold=threshold)


@pytest.mark.parametrize(
    ("reason", "cause"),
    [
        (QualityReason.REPORT_NEEDS_REVIEW, HoldCause.QUALITY),
        (QualityReason.REPORT_BLOCKED, HoldCause.QUALITY),
        (QualityReason.REQUIRED_CHECK_MISSING, HoldCause.QUALITY),
        (QualityReason.INSPECTION_UNAVAILABLE, HoldCause.INSPECTION),
        (QualityReason.REPORT_EVALUATION_ERROR, HoldCause.INSPECTION),
        (QualityReason.REPORT_PRODUCTION_FAILED, HoldCause.INSPECTION),
    ],
)
def test_cause_classification(reason: QualityReason, cause: HoldCause) -> None:
    assert cause_of(GateDecisionSample(False, reason)) is cause


def test_an_allowance_has_no_cause() -> None:
    assert cause_of(OK) is None


def test_sample_from_a_stored_report_follows_the_same_distinction_as_the_gate() -> None:
    only_unavailable = sample_from_report(
        QualityVerdict.REVIEW, ["ok", "inspection_unavailable", "inspection_unavailable"]
    )
    assert only_unavailable == UNAVAIL
    mixed = sample_from_report(
        QualityVerdict.REVIEW, ["inspection_unavailable", "license_unverified"]
    )
    assert mixed == REVIEW
    assert (
        sample_from_report(QualityVerdict.BLOCK, ["inspection_unavailable", "audio_defect"])
        == BLOCK
    )
    assert sample_from_report(QualityVerdict.PASS, ["ok"]).allowed
    assert sample_from_report(QualityVerdict.WARN, ["ok", "minor_wording"]) == WARN_OK
    # 読めない理由コードは品質の保留として数える（検査系に押し付けない）
    assert sample_from_report(QualityVerdict.REVIEW, ["nonsense"]) == REVIEW
