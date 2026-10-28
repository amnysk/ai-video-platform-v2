"""投稿前の品質レポートの検証（INV-101、設計 v3 T22）。

fail-closed: 許可は「今の final_video・入力に結び付いた、検査から再計算しても一致する、
PASS（または WARN）のレポート」だけ。それ以外はすべて拒否で、理由コードを返す。
``None`` は PASS ではない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from contracts.quality import (
    QUALITY_REPORT_SCHEMA_VERSION,
    QualityReason,
    QualityReport,
    QualityReportError,
    QualityReportFailure,
    QualityVerdict,
    parse_quality_report,
)
from domain.quality.aggregate import aggregate


@dataclass(frozen=True, slots=True)
class UploadDecision:
    allowed: bool
    reason_code: QualityReason


def _refuse(reason: QualityReason) -> UploadDecision:
    return UploadDecision(allowed=False, reason_code=reason)


def evaluate_for_upload(
    report: QualityReport | QualityReportFailure | object | None,
    *,
    current_final_video_sha256: str,
    current_input_hash: str,
    now: datetime,
) -> UploadDecision:
    if report is None:
        return _refuse(QualityReason.REPORT_MISSING)
    if isinstance(report, QualityReportFailure):
        return _refuse(QualityReason.REPORT_PRODUCTION_FAILED)
    if not isinstance(report, QualityReport):
        try:
            report = parse_quality_report(report)
        except QualityReportError as err:
            return _refuse(err.reason_code)
    if report.schema_version != QUALITY_REPORT_SCHEMA_VERSION:
        return _refuse(QualityReason.REPORT_SCHEMA_MISMATCH)
    if not isinstance(report.verdict, QualityVerdict):
        return _refuse(QualityReason.REPORT_UNKNOWN_VERDICT)
    if not report.checks:
        return _refuse(QualityReason.REPORT_EMPTY_CHECKS)
    if not current_final_video_sha256 or report.final_video_sha256 != current_final_video_sha256:
        return _refuse(QualityReason.REPORT_FINAL_VIDEO_MISMATCH)
    if not current_input_hash or report.input_hash != current_input_hash:
        return _refuse(QualityReason.REPORT_INPUT_HASH_MISMATCH)
    if report.generated_at > now:
        return _refuse(QualityReason.REPORT_FROM_THE_FUTURE)
    if aggregate(report.checks) is not report.verdict:
        return _refuse(QualityReason.REPORT_VERDICT_INCONSISTENT)
    if report.verdict is QualityVerdict.BLOCK:
        return _refuse(QualityReason.REPORT_BLOCKED)
    if report.verdict is QualityVerdict.REVIEW:
        return _refuse(QualityReason.REPORT_NEEDS_REVIEW)
    if report.verdict is QualityVerdict.WARN:
        return UploadDecision(True, QualityReason.UPLOAD_ALLOWED_WITH_WARNINGS)
    return UploadDecision(True, QualityReason.UPLOAD_ALLOWED)
