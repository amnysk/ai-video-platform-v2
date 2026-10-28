"""INV-101: 無い・壊れた・結び付かない品質レポートは PASS ではない（design v3 T22）。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from contracts.quality import (
    CLOCK_SKEW_TOLERANCE,
    QUALITY_REPORT_SCHEMA_VERSION,
    QualityCheck,
    QualityGate,
    QualityModelIdentity,
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
RULE = "rules-1"
MODEL = QualityModelIdentity(provider="test", model_id="critic-1")


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
        "rule_version": RULE,
        "model": MODEL,
        "verdict": aggregate(checks),
        "checks": checks,
    }
    fields.update(over)
    stamp = fields["generated_at"]
    # 既定の期限は検査時刻の 1 時間後（時刻が naive な回帰テストでは今から 1 時間後）
    fields.setdefault(
        "expires_at",
        stamp + timedelta(hours=1)
        if isinstance(stamp, datetime) and stamp.tzinfo
        else NOW + timedelta(hours=1),
    )
    return QualityReport(**fields)


def _decide(report, sha: str = SHA, input_hash: str = "in-1", rule: str = RULE, now=NOW):
    return evaluate_for_upload(
        report,
        current_final_video_sha256=sha,
        current_input_hash=input_hash,
        current_rule_version=rule,
        now=now,
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


# --- 独立レビューの指摘（MAJOR 2、MINOR m6/m7）の再現を回帰テストにしたもの -------------------


def test_naive_generated_at_is_refused_not_raised() -> None:
    d = _decide(_report(generated_at=datetime(2026, 10, 28, 11)))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_TIMESTAMP_INVALID


def test_naive_now_is_refused_not_raised() -> None:
    d = evaluate_for_upload(
        _report(),
        current_final_video_sha256=SHA,
        current_input_hash="in-1",
        current_rule_version=RULE,
        now=datetime(2026, 10, 28, 12),
    )
    assert not d.allowed and d.reason_code is QualityReason.REPORT_TIMESTAMP_INVALID


def test_deeply_nested_json_is_refused_not_raised() -> None:
    d = _decide("[" * 100_000)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_UNPARSABLE


def test_clock_skew_within_tolerance_is_allowed_and_beyond_is_refused() -> None:
    ok = _decide(_report(generated_at=NOW + CLOCK_SKEW_TOLERANCE))
    assert ok.allowed
    late = _decide(_report(generated_at=NOW + CLOCK_SKEW_TOLERANCE + timedelta(seconds=1)))
    assert not late.allowed and late.reason_code is QualityReason.REPORT_FROM_THE_FUTURE


def test_checks_must_be_a_tuple_of_quality_checks() -> None:
    """list の checks は PASS 判定が通ってしまっていた（直接構築した report の再検証）。"""
    d = _decide(replace(_report(), checks=[_check()]))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_UNPARSABLE
    d = _decide(replace(_report(), checks=(object(),)))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_UNPARSABLE


def test_a_directly_constructed_report_with_bad_field_types_is_refused() -> None:
    for over in (
        {"verdict": "pass"},
        {"final_video_sha256": "NOT-A-SHA"},
        {"input_hash": ""},
        {"episode_id": 3},
        {"generated_at": "2026-10-28"},
    ):
        d = _decide(_report(**over))
        assert not d.allowed, over


def test_a_check_built_with_wrong_types_is_refused() -> None:
    bad = QualityCheck(
        gate="geo",  # type: ignore[arg-type]
        verdict=V.PASS,
        reason_code=QualityReason.OK,
        detail="",
        deterministic=True,
    )
    d = _decide(_report([bad]))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_UNPARSABLE


@pytest.mark.parametrize(
    "garbage",
    [
        object(),
        {"checks": None},
        _report().to_dict() | {"checks": [{"x": 1}]},
        _report().to_dict() | {"generated_at": 5},
        _report().to_dict() | {"input_hash": None},
    ],
)
def test_no_input_can_make_evaluate_raise(garbage) -> None:
    assert not _decide(garbage).allowed


def test_an_unexpected_internal_error_fails_closed(monkeypatch) -> None:
    import domain.quality.upload_decision as mod

    def boom(_checks):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "aggregate", boom)
    d = _decide(_report())
    assert not d.allowed and d.reason_code is QualityReason.REPORT_EVALUATION_ERROR
