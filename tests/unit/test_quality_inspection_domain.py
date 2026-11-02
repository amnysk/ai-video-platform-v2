"""検査系の障害と品質問題の区別（ADR-0087 §8 / INV-156）。純粋部分。

理由は docs/testing/bgm-auto-quality-rationale.md。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
from contracts.quality_records import CHECK_KEY_GATES, CheckKey, UsageFeature
from domain.quality.deterministic import QualityInputs
from domain.quality.inspection import (
    RETRY_POLICY,
    RetryPolicy,
    backoff_delays,
    refine_hold_reason,
)
from domain.quality.records import (
    CheckSubject,
    FreshCheckSets,
    UsageDeclaration,
    recorded_checks_provider,
)
from domain.quality.upload_decision import UploadDecision

SHA = "a" * 64
NOW = datetime(2026, 11, 2, tzinfo=UTC)
SUBJECT = CheckSubject("ep-1", SHA, "b" * 64)
INPUTS = QualityInputs("ep-1", SHA, "b" * 64)


def _check(verdict: QualityVerdict, reason: QualityReason, gate=QualityGate.AUDIO):
    return QualityCheck(gate, verdict, reason, "fixed detail", True)


def test_retry_policy_is_three_attempts_exponential_within_thirty_seconds() -> None:
    assert RETRY_POLICY.attempts == 3
    assert RETRY_POLICY.max_total_seconds <= 30
    delays = backoff_delays(RETRY_POLICY)
    assert len(delays) == 2  # 3 回試すので待ちは 2 回
    assert delays[1] == delays[0] * RETRY_POLICY.factor
    assert sum(delays) <= RETRY_POLICY.max_total_seconds


def test_policy_rejects_unbounded_values() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0, base_delay_seconds=1, factor=2, max_total_seconds=30)
    with pytest.raises(ValueError):
        RetryPolicy(attempts=3, base_delay_seconds=20, factor=4, max_total_seconds=30)


def _provider_checks(unavailable: frozenset[CheckKey]):
    usage = {f: UsageDeclaration(SUBJECT, f, False, "render", NOW) for f in UsageFeature}
    provider = recorded_checks_provider(FreshCheckSets({}), usage, unavailable=unavailable)
    return provider(INPUTS)


def test_missing_required_check_is_review_with_required_check_missing_by_default() -> None:
    checks = _provider_checks(frozenset())
    missing = [c for c in checks if c.verdict is QualityVerdict.REVIEW]
    assert missing and {c.reason_code for c in missing} == {QualityReason.REQUIRED_CHECK_MISSING}


def test_unavailable_required_check_is_still_review_but_distinct_reason() -> None:
    checks = _provider_checks(frozenset({CheckKey.TECHNICAL_QA}))
    by_gate = {c.gate: c for c in checks if c.verdict is QualityVerdict.REVIEW}
    for gate in CHECK_KEY_GATES[CheckKey.TECHNICAL_QA]:
        assert by_gate[gate].reason_code is QualityReason.INSPECTION_UNAVAILABLE
    # 隣の（原因が違う）欠落は区別されたまま
    assert by_gate[QualityGate.METADATA].reason_code is QualityReason.REQUIRED_CHECK_MISSING


def test_unavailable_never_downgrades_below_review() -> None:
    assert all(
        c.verdict is QualityVerdict.REVIEW
        for c in _provider_checks(frozenset(CheckKey))
        if c.reason_code is QualityReason.INSPECTION_UNAVAILABLE
    )


REFUSED = UploadDecision(False, QualityReason.REPORT_NEEDS_REVIEW)


def test_hold_is_inspection_unavailable_only_when_it_is_the_sole_cause() -> None:
    only = [
        _check(QualityVerdict.PASS, QualityReason.OK, QualityGate.VISUAL),
        _check(QualityVerdict.REVIEW, QualityReason.INSPECTION_UNAVAILABLE),
    ]
    assert refine_hold_reason(REFUSED, only).reason_code is QualityReason.INSPECTION_UNAVAILABLE
    assert not refine_hold_reason(REFUSED, only).allowed


@pytest.mark.parametrize(
    "other",
    [
        _check(QualityVerdict.REVIEW, QualityReason.REQUIRED_CHECK_MISSING, QualityGate.METADATA),
        _check(QualityVerdict.REVIEW, QualityReason.LICENSE_UNVERIFIED, QualityGate.LICENSE),
        _check(QualityVerdict.BLOCK, QualityReason.AUDIO_DEFECT, QualityGate.MUSIC),
    ],
)
def test_genuine_quality_cause_keeps_its_own_reason(other: QualityCheck) -> None:
    checks = [_check(QualityVerdict.REVIEW, QualityReason.INSPECTION_UNAVAILABLE), other]
    decision = UploadDecision(False, QualityReason.REPORT_BLOCKED)
    assert refine_hold_reason(decision, checks) == decision


def test_refine_never_allows_and_never_touches_allowed_or_other_refusals() -> None:
    checks = [_check(QualityVerdict.REVIEW, QualityReason.INSPECTION_UNAVAILABLE)]
    allowed = UploadDecision(True, QualityReason.UPLOAD_ALLOWED)
    assert refine_hold_reason(allowed, checks) == allowed
    stale = UploadDecision(False, QualityReason.REPORT_EXPIRED)
    assert refine_hold_reason(stale, checks) == stale
    assert not refine_hold_reason(REFUSED, []).allowed
