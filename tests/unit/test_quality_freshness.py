"""INV-107: 品質レポートの鮮度。今の artifact・規則版・時刻（注入）と合わないレポートは拒否。

理由は docs/testing/verdict-rationale.md。時計は注入する（実時刻を読まない）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from contracts.quality import (
    MAX_REPORT_VALIDITY,
    QUALITY_REPORT_SCHEMA_VERSION,
    QualityCheck,
    QualityGate,
    QualityModelIdentity,
    QualityReason,
    QualityReport,
    QualityReportError,
    QualityVerdict,
    parse_quality_report,
)
from domain.quality import check_freshness, evaluate_for_upload

SHA = "a" * 64
NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
RULE = "rules-7"


def _check(*, deterministic: bool = True) -> QualityCheck:
    return QualityCheck(
        gate=QualityGate.GEO if deterministic else QualityGate.CRITIC,
        verdict=QualityVerdict.PASS,
        reason_code=QualityReason.OK,
        detail="ok",
        deterministic=deterministic,
    )


def _report(**over: object) -> QualityReport:
    fields: dict[str, object] = {
        "episode_id": "ep-1",
        "final_video_sha256": SHA,
        "input_hash": "in-1",
        "generated_at": NOW - timedelta(minutes=10),
        "generator_version": "g1",
        "reviewer_version": "r1",
        "rule_version": RULE,
        "expires_at": NOW + timedelta(hours=1),
        "verdict": QualityVerdict.PASS,
        "checks": (_check(),),
        "model": None,
    }
    fields.update(over)
    return QualityReport(**fields)  # type: ignore[arg-type]


def _fresh(report: QualityReport, **over: object) -> QualityReason | None:
    args: dict[str, object] = {
        "current_final_video_sha256": SHA,
        "current_input_hash": "in-1",
        "current_rule_version": RULE,
        "now": NOW,
    }
    args.update(over)
    return check_freshness(report, **args)  # type: ignore[arg-type]


def _decide(report: object, **over: object):
    args: dict[str, object] = {
        "current_final_video_sha256": SHA,
        "current_input_hash": "in-1",
        "current_rule_version": RULE,
        "now": NOW,
    }
    args.update(over)
    return evaluate_for_upload(report, **args)  # type: ignore[arg-type]


def test_a_fresh_report_is_allowed_and_a_deterministic_one_needs_no_model() -> None:
    assert _fresh(_report()) is None
    d = _decide(_report())
    assert d.allowed and d.reason_code is QualityReason.UPLOAD_ALLOWED


def test_a_report_for_another_artifact_hash_is_refused_even_if_pass() -> None:
    assert _fresh(_report(), current_final_video_sha256="b" * 64) is (
        QualityReason.REPORT_FINAL_VIDEO_MISMATCH
    )
    d = _decide(_report(), current_final_video_sha256="b" * 64)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_FINAL_VIDEO_MISMATCH


def test_a_report_for_an_older_rule_version_is_refused() -> None:
    d = _decide(_report(), current_rule_version="rules-8")
    assert not d.allowed and d.reason_code is QualityReason.REPORT_RULE_VERSION_MISMATCH


def test_an_empty_current_rule_version_matches_nothing() -> None:
    d = _decide(_report(), current_rule_version="")
    assert not d.allowed and d.reason_code is QualityReason.REPORT_RULE_VERSION_MISMATCH


def test_expiry_is_judged_against_the_injected_clock() -> None:
    report = _report()
    assert _decide(report, now=report.expires_at - timedelta(seconds=1)).allowed
    for later in (report.expires_at, report.expires_at + timedelta(days=30)):
        d = _decide(report, now=later)
        assert not d.allowed and d.reason_code is QualityReason.REPORT_EXPIRED


def test_the_decision_does_not_read_the_real_clock() -> None:
    """実時刻では 2026-10-29 は過去/未来どちらにもなりうる。結果は注入した now だけで決まる。"""
    far_past = datetime(2001, 1, 1, tzinfo=UTC)
    report = _report(generated_at=far_past, expires_at=far_past + timedelta(hours=1))
    assert _decide(report, now=far_past + timedelta(minutes=1)).allowed
    assert _decide(report, now=far_past + timedelta(hours=2)).reason_code is (
        QualityReason.REPORT_EXPIRED
    )


@pytest.mark.parametrize(
    "over",
    [
        {"expires_at": NOW - timedelta(minutes=10)},  # generated_at 以前
        {"expires_at": NOW - timedelta(minutes=10) + MAX_REPORT_VALIDITY + timedelta(seconds=1)},
        {"expires_at": datetime(2026, 10, 29, 13)},  # naive
    ],
)
def test_an_incoherent_expiry_is_refused_not_trusted(over: dict[str, object]) -> None:
    d = _decide(_report(**over))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_FRESHNESS_INVALID


def test_the_maximum_validity_is_allowed_exactly() -> None:
    generated = NOW - timedelta(minutes=10)
    report = _report(generated_at=generated, expires_at=generated + MAX_REPORT_VALIDITY)
    assert _decide(report).allowed


def test_an_ai_check_without_a_model_identity_is_refused() -> None:
    report = _report(checks=(_check(), _check(deterministic=False)))
    d = _decide(report)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_MODEL_IDENTITY_MISSING
    with_model = _report(
        checks=(_check(), _check(deterministic=False)),
        model=QualityModelIdentity(provider="openai", model_id="gpt-5.4"),
    )
    assert _decide(with_model).allowed


def test_a_model_identity_cannot_carry_free_text() -> None:
    for provider, model_id in (("p\nq", "m"), ("p", "sk-" + "a" * 500), ("", "m")):
        with pytest.raises(ValueError):
            QualityModelIdentity(provider=provider, model_id=model_id)


def test_the_report_round_trips_with_its_freshness_fields() -> None:
    report = _report(
        checks=(_check(), _check(deterministic=False)),
        model=QualityModelIdentity(provider="anthropic", model_id="claude-sonnet-5-5"),
    )
    assert parse_quality_report(json.dumps(report.to_dict())) == report
    assert _decide(json.dumps(report.to_dict())).allowed


@pytest.mark.parametrize("missing", ["rule_version", "expires_at"])
def test_a_report_without_a_freshness_field_cannot_be_parsed(missing: str) -> None:
    data = _report().to_dict()
    del data[missing]
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(data)
    assert err.value.reason_code is QualityReason.REPORT_UNPARSABLE
    assert _decide(data).allowed is False


def test_a_schema_version_1_report_is_refused_as_a_schema_mismatch() -> None:
    data = _report().to_dict()
    data["schema_version"] = 1
    for key in ("rule_version", "expires_at", "model"):
        del data[key]
    d = _decide(data)
    assert not d.allowed and d.reason_code is QualityReason.REPORT_SCHEMA_MISMATCH
    assert QUALITY_REPORT_SCHEMA_VERSION == 2


def test_a_naive_clock_is_refused_not_raised() -> None:
    d = _decide(_report(), now=datetime(2026, 10, 29, 12))
    assert not d.allowed and d.reason_code is QualityReason.REPORT_TIMESTAMP_INVALID


def test_staleness_beats_a_pass_verdict_but_not_a_block() -> None:
    """期限切れの PASS は拒否。BLOCK は期限切れでも拒否（どちらも通さない）。"""
    blocked = replace(
        _report(),
        checks=(
            replace(_check(), verdict=QualityVerdict.BLOCK, reason_code=QualityReason.GEO_MISMATCH),
        ),
        verdict=QualityVerdict.BLOCK,
    )
    assert not _decide(blocked).allowed
    assert not _decide(blocked, now=NOW + timedelta(days=2)).allowed


def test_a_hash_with_a_trailing_newline_is_refused_even_when_both_sides_match() -> None:
    # 独立レビュー（F, MINOR）: ``re.match`` の ``$`` は末尾の改行を許す。
    # 呼び出し側が同じ不正値を渡すと一致してしまうので、形式そのものを拒否する（fail closed）。
    bad = SHA + "\n"
    decision = _decide(_report(final_video_sha256=bad), current_final_video_sha256=bad)
    assert decision.allowed is False


def test_an_unbounded_rule_version_is_refused_even_when_both_sides_match() -> None:
    # 独立レビュー（F, MINOR）: rule_version は識別子であり、
    # 長さ上限（SAFE_ID_PATTERN）を超えたら拒否する。
    huge = "r" * 1_000_000
    decision = _decide(_report(rule_version=huge), current_rule_version=huge)
    assert decision.allowed is False


def test_an_unbounded_episode_id_is_refused() -> None:
    decision = _decide(_report(episode_id="e" * 1_000_000))
    assert decision.allowed is False
