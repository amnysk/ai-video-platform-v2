"""品質レポートの契約（contracts/quality.py）。ADR-0073。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from contracts.quality import (
    QUALITY_REPORT_SCHEMA_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityReport,
    QualityReportError,
    QualityVerdict,
    parse_quality_report,
)

SHA = "a" * 64


def _report_dict() -> dict:
    return {
        "schema_version": QUALITY_REPORT_SCHEMA_VERSION,
        "episode_id": "ep-1",
        "final_video_sha256": SHA,
        "input_hash": "in-1",
        "generated_at": "2026-10-28T00:00:00+00:00",
        "generator_version": "g1",
        "reviewer_version": "r1",
        "rule_version": "rules-1",
        "expires_at": "2026-10-28T06:00:00+00:00",
        "model": None,
        "verdict": "pass",
        "checks": [
            {
                "gate": "geo",
                "verdict": "pass",
                "reason_code": "ok",
                "detail": "all resolved",
                "scene_id": None,
                "deterministic": True,
            }
        ],
    }


def test_a_well_formed_report_round_trips() -> None:
    report = parse_quality_report(json.dumps(_report_dict()))
    assert isinstance(report, QualityReport)
    assert report.verdict is QualityVerdict.PASS
    assert report.checks[0].gate is QualityGate.GEO
    assert report.generated_at == datetime(2026, 10, 28, tzinfo=UTC)
    assert parse_quality_report(report.to_dict()) == report


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda d: d.update(verdict="great"), QualityReason.REPORT_UNKNOWN_VERDICT),
        (lambda d: d["checks"][0].update(verdict="maybe"), QualityReason.REPORT_UNKNOWN_VERDICT),
        (lambda d: d.update(schema_version=99), QualityReason.REPORT_SCHEMA_MISMATCH),
        (lambda d: d.pop("schema_version"), QualityReason.REPORT_SCHEMA_MISMATCH),
        (lambda d: d.update(final_video_sha256="xyz"), QualityReason.REPORT_UNPARSABLE),
        (lambda d: d.update(generated_at="2026-10-28T00:00:00"), QualityReason.REPORT_UNPARSABLE),
        (lambda d: d.update(checks="x"), QualityReason.REPORT_UNPARSABLE),
        (lambda d: d["checks"][0].update(gate="nope"), QualityReason.REPORT_UNPARSABLE),
        (lambda d: d["checks"][0].update(reason_code="nope"), QualityReason.REPORT_UNPARSABLE),
        (lambda d: d["checks"][0].update(deterministic="yes"), QualityReason.REPORT_UNPARSABLE),
        (lambda d: d.update(input_hash=""), QualityReason.REPORT_UNPARSABLE),
        (lambda d: d["checks"][0].update(verdict="block"), QualityReason.REPORT_UNPARSABLE),
    ],
)
def test_malformed_reports_are_rejected_with_a_reason(mutate, reason) -> None:
    data = _report_dict()
    mutate(data)
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(data)
    assert err.value.reason_code is reason


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", b"\xff\xfe", 42, None])
def test_non_report_input_is_unparsable(raw) -> None:
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(raw)
    assert err.value.reason_code is QualityReason.REPORT_UNPARSABLE


def test_a_non_pass_check_must_carry_a_real_reason_code() -> None:
    with pytest.raises(ValueError):
        QualityCheck(
            gate=QualityGate.GEO,
            verdict=QualityVerdict.BLOCK,
            reason_code=QualityReason.OK,
            detail="",
            deterministic=True,
        )


def test_reason_codes_are_unique_strings() -> None:
    values = [r.value for r in QualityReason]
    assert len(values) == len(set(values))


# --- 独立レビューの指摘（MAJOR 2/3、MINOR m5）の再現を回帰テストにしたもの -------------------

SECRET = "sk-" + "A1b2C3d4" * 625  # 5000 文字の秘密らしい値


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(verdict=SECRET),
        lambda d: d["checks"][0].update(verdict=SECRET),
        lambda d: d["checks"][0].update(gate=SECRET),
        lambda d: d["checks"][0].update(reason_code=SECRET),
        lambda d: d.update(schema_version=SECRET),
        lambda d: d.update(generated_at=SECRET),
        lambda d: d.update(final_video_sha256=SECRET),
        lambda d: d.update(episode_id=[SECRET]),
    ],
)
def test_error_messages_never_echo_untrusted_values(mutate) -> None:
    """INV-20/39: 取り込めなかった値（秘密を含みうる）を例外文に写さない。長さも有界。"""
    data = _report_dict()
    mutate(data)
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(data)
    message = str(err.value)
    assert "sk-" not in message and "A1b2C3d4" not in message
    assert len(message) < 200


def test_raw_text_is_never_echoed_either() -> None:
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(SECRET)
    assert "A1b2C3d4" not in str(err.value) and len(str(err.value)) < 200


@pytest.mark.parametrize("raw", ["[" * 100_000, '{"a":' * 100_000], ids=["brackets", "objects"])
def test_deeply_nested_json_is_unparsable_not_a_recursion_error(raw: str) -> None:
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(raw)
    assert err.value.reason_code is QualityReason.REPORT_UNPARSABLE


@pytest.mark.parametrize("version", [1.0, True, "1", None])
def test_schema_version_must_be_exactly_the_int(version) -> None:
    data = _report_dict()
    data["schema_version"] = version
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(data)
    assert err.value.reason_code is QualityReason.REPORT_SCHEMA_MISMATCH
