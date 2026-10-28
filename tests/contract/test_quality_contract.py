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
