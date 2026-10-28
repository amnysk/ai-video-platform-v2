"""INV-101: 無い・壊れた・結び付かない品質レポートは PASS ではない（design v3 T22）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from contracts.quality import (
    QUALITY_REPORT_SCHEMA_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityReport,
    QualityReportFailure,
    QualityVerdict,
)
from contracts.states import EpisodeStatus
from domain.quality import aggregate, episode_status_for, evaluate_for_upload

V = QualityVerdict
SHA = "b" * 64
NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def _check(verdict: QualityVerdict = V.PASS, *, deterministic: bool = True) -> QualityCheck:
    return QualityCheck(
        gate=QualityGate.GEO if deterministic else QualityGate.CRITIC,
        verdict=verdict,
        reason_code=QualityReason.OK if verdict is V.PASS else QualityReason.GEO_MISMATCH,
        detail="",
        deterministic=deterministic,
    )


def _report(checks=None, **over) -> QualityReport:
    checks = tuple(checks) if checks is not None else (_check(),)
    fields = {
        "episode_id": "ep-1",
        "final_video_sha256": SHA,
        "input_hash": "in-1",
        "generated_at": NOW - timedelta(minutes=1),
        "generator_version": "g1",
        "reviewer_version": "r1",
        "verdict": aggregate(checks),
        "checks": checks,
    }
    fields.update(over)
    return QualityReport(**fields)


def _decide(report, sha: str = SHA, input_hash: str = "in-1"):
    return evaluate_for_upload(
        report, current_final_video_sha256=sha, current_input_hash=input_hash, now=NOW
    )


def test_a_verified_pass_is_allowed() -> None:
    d = _decide(_report())
    assert d.allowed and d.reason_code is QualityReason.UPLOAD_ALLOWED


def test_warn_is_allowed_and_reported() -> None:
    d = _decide(_report([_check(), _check(V.WARN)]))
    assert d.allowed and d.reason_code is QualityReason.UPLOAD_ALLOWED_WITH_WARNINGS


def test_a_missing_report_is_never_pass() -> None:
    d = _decide(None)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_MISSING


def test_a_failed_to_produce_report_is_refused() -> None:
    d = _decide(QualityReportFailure("critic timeout"))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_PRODUCTION_FAILED


@pytest.mark.parametrize("raw", ["", "{", "[]", 42, object(), b"\xff"])
def test_unparsable_input_is_refused(raw) -> None:
    d = _decide(raw)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_UNPARSABLE


def test_a_raw_json_report_is_parsed_then_judged() -> None:
    raw = json.dumps(_report().to_dict())
    assert _decide(raw).allowed
    data = _report().to_dict()
    data["schema_version"] = QUALITY_REPORT_SCHEMA_VERSION + 1
    d = _decide(json.dumps(data))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_SCHEMA_MISMATCH


def test_an_unknown_verdict_is_refused() -> None:
    data = _report().to_dict()
    data["verdict"] = "approved"
    d = _decide(data)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_UNKNOWN_VERDICT


def test_a_wrong_schema_version_object_is_refused() -> None:
    d = _decide(_report(schema_version=999))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_SCHEMA_MISMATCH


def test_empty_checks_are_refused() -> None:
    d = _decide(_report([], verdict=V.PASS))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_EMPTY_CHECKS


def test_a_report_for_another_final_video_is_refused() -> None:
    """再 render で final_video が変わったら、古い PASS は効かない。"""
    d = _decide(_report(), sha="c" * 64)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_FINAL_VIDEO_MISMATCH


def test_a_report_for_other_inputs_is_refused() -> None:
    d = _decide(_report(), input_hash="in-2")
    assert not d.allowed and d.reason_code is QualityReason.REPORT_INPUT_HASH_MISMATCH


def test_a_missing_current_sha_never_matches() -> None:
    d = _decide(_report(), sha="")
    assert not d.allowed and d.reason_code is QualityReason.REPORT_FINAL_VIDEO_MISMATCH


def test_a_pass_claim_that_contradicts_its_checks_is_refused() -> None:
    """保存された verdict を信用せず、検査から再計算する（改ざん・書き間違い）。"""
    d = _decide(_report([_check(V.BLOCK)], verdict=V.PASS))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_VERDICT_INCONSISTENT


def test_a_future_dated_report_is_refused() -> None:
    d = _decide(_report(generated_at=NOW + timedelta(hours=1)))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_FROM_THE_FUTURE


def test_block_and_review_are_refused_with_their_reason() -> None:
    assert _decide(_report([_check(V.BLOCK)])).reason_code is QualityReason.REPORT_BLOCKED
    assert _decide(_report([_check(V.REVIEW)])).reason_code is QualityReason.REPORT_NEEDS_REVIEW
    assert not _decide(_report([_check(V.BLOCK)])).allowed


def test_llm_pass_does_not_rescue_a_deterministic_block_at_the_gate() -> None:
    d = _decide(_report([_check(V.BLOCK), _check(V.PASS, deterministic=False)]))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_BLOCKED


def test_verdict_maps_onto_existing_episode_states_only() -> None:
    assert episode_status_for(V.PASS) is None
    assert episode_status_for(V.WARN) is None
    assert episode_status_for(V.REVIEW) is EpisodeStatus.BLOCKED
    assert episode_status_for(V.BLOCK) is EpisodeStatus.NEEDS_WORK
    assert {episode_status_for(v) for v in V} <= {None, *EpisodeStatus}
