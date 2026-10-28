"""品質レポート（QualityReport）の契約。**唯一の定義**（AGENTS.md §8 / ADR-0073）。

投稿前の品質ゲートが読む判定の語彙・型・取り込み（parse）をここに置く。純粋: I/O も
provider も持たない。集計（verdict の導出）と投稿可否の判定は ``domain/quality``。

不変条件（INV-101）: レポートが無い・壊れている・今の final_video に結び付かない、は
**PASS ではない**。取り込みの失敗は例外（``QualityReportError``）で、理由コードを運ぶ。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

QUALITY_REPORT_SCHEMA_VERSION = 1

#: レポートの ``generated_at`` が ``now`` より未来でも許す時計のずれ（**ここだけで定義**）。
#: これを超える未来日付は取り込み元の時計異常か改ざんとして拒否する（INV-101）。
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class QualityVerdict(StrEnum):
    """判定。重さの順は PASS < WARN < REVIEW < BLOCK（``VERDICT_SEVERITY``）。"""

    PASS = "pass"
    WARN = "warn"
    REVIEW = "review"
    BLOCK = "block"


#: 重さの順位。集計はこの順位だけで比べる（別の場所に並べ直さない）。
VERDICT_SEVERITY: dict[QualityVerdict, int] = {
    QualityVerdict.PASS: 0,
    QualityVerdict.WARN: 1,
    QualityVerdict.REVIEW: 2,
    QualityVerdict.BLOCK: 3,
}


class QualityGate(StrEnum):
    """検査の種類（設計 v3 §5.3 の QAGate）。"""

    HISTORICAL = "historical"
    GEO = "geo"
    MUSIC = "music"
    VISUAL = "visual"
    AUDIO = "audio"
    METADATA = "metadata"
    LICENSE = "license"
    CRITIC = "critic"


class QualityReason(StrEnum):
    """理由コード。検査の理由と、投稿可否の理由を**ここだけ**で定義する。"""

    OK = "ok"
    # --- 検査の理由（QualityCheck.reason_code） ---
    HISTORICAL_CLAIM_UNSUPPORTED = "historical_claim_unsupported"
    HISTORICAL_SOURCE_CONFLICT = "historical_source_conflict"
    HIGH_IMPORTANCE_UNVERIFIABLE = "high_importance_unverifiable"
    GEO_MISMATCH = "geo_mismatch"
    GEO_TIME_BASIS_UNKNOWN = "geo_time_basis_unknown"
    METADATA_LINT_FAILED = "metadata_lint_failed"
    LICENSE_UNVERIFIED = "license_unverified"
    AUDIO_DEFECT = "audio_defect"
    VISUAL_DEFECT = "visual_defect"
    MINOR_WORDING = "minor_wording"
    CRITIC_CONCERN = "critic_concern"
    # --- レポートの取り込み失敗（QualityReportError / 投稿可否） ---
    REPORT_MISSING = "report_missing"
    REPORT_PRODUCTION_FAILED = "report_production_failed"
    REPORT_UNPARSABLE = "report_unparsable"
    REPORT_SCHEMA_MISMATCH = "report_schema_mismatch"
    REPORT_UNKNOWN_VERDICT = "report_unknown_verdict"
    REPORT_EMPTY_CHECKS = "report_empty_checks"
    REPORT_VERDICT_INCONSISTENT = "report_verdict_inconsistent"
    REPORT_FINAL_VIDEO_MISMATCH = "report_final_video_mismatch"
    REPORT_INPUT_HASH_MISMATCH = "report_input_hash_mismatch"
    REPORT_FROM_THE_FUTURE = "report_from_the_future"
    REPORT_TIMESTAMP_INVALID = "report_timestamp_invalid"
    REPORT_EVALUATION_ERROR = "report_evaluation_error"
    # --- 投稿可否の結果 ---
    UPLOAD_ALLOWED = "upload_allowed"
    UPLOAD_ALLOWED_WITH_WARNINGS = "upload_allowed_with_warnings"
    REPORT_BLOCKED = "report_blocked"
    REPORT_NEEDS_REVIEW = "report_needs_review"


class QualityReportError(ValueError):
    """レポートを取り込めない。``reason_code`` が投稿を拒否する理由になる。"""

    def __init__(self, reason_code: QualityReason, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class QualityCheck:
    """1 つの検査結果。``deterministic=False`` は LLM 批評で、判定を厳しくする方向にだけ働く。"""

    gate: QualityGate
    verdict: QualityVerdict
    reason_code: QualityReason
    detail: str
    deterministic: bool
    scene_id: str | None = None

    def __post_init__(self) -> None:
        # PASS ⇔ OK。不合格なのに理由が無い（または合格なのに理由がある）検査は作れない
        if (self.verdict is QualityVerdict.PASS) != (self.reason_code is QualityReason.OK):
            raise ValueError("a check is PASS if and only if its reason_code is OK")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate.value,
            "verdict": self.verdict.value,
            "reason_code": self.reason_code.value,
            "detail": self.detail,
            "scene_id": self.scene_id,
            "deterministic": self.deterministic,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    """final_video と入力に結び付いた品質判定。``verdict`` は ``domain.quality.aggregate`` の値。"""

    episode_id: str
    final_video_sha256: str
    input_hash: str
    generated_at: datetime
    generator_version: str
    reviewer_version: str
    verdict: QualityVerdict
    checks: tuple[QualityCheck, ...] = field(default_factory=tuple)
    schema_version: int = QUALITY_REPORT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "final_video_sha256": self.final_video_sha256,
            "input_hash": self.input_hash,
            "generated_at": self.generated_at.isoformat(),
            "generator_version": self.generator_version,
            "reviewer_version": self.reviewer_version,
            "verdict": self.verdict.value,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True, slots=True)
class QualityReportFailure:
    """レポートを**作れなかった**ことの記録（無検査と区別する）。投稿可否は常に拒否。"""

    reason: str = ""


def describe(value: object) -> str:
    """例外文に入れてよい値の記述。**値そのものは写さない**（INV-20/39）: 型名と長さだけ。"""
    try:
        length = len(value)  # type: ignore[arg-type]
    except TypeError:
        return type(value).__name__
    return f"{type(value).__name__}(len={length})"


def _unparsable(message: str) -> QualityReportError:
    return QualityReportError(QualityReason.REPORT_UNPARSABLE, message)


def _str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _unparsable(f"{key} must be a non-empty string")
    return value


def _enum[E: StrEnum](cls: type[E], value: object, what: str) -> E:
    if not isinstance(value, str):
        raise _unparsable(f"{what} must be a string")
    try:
        return cls(value)
    except ValueError:
        raise _unparsable(f"unknown {what}: {describe(value)}") from None


def _verdict(value: object, what: str) -> QualityVerdict:
    try:
        return QualityVerdict(value)  # type: ignore[arg-type]
    except ValueError:
        raise QualityReportError(
            QualityReason.REPORT_UNKNOWN_VERDICT, f"unknown {what}: {describe(value)}"
        ) from None


def _parse_check(raw: object) -> QualityCheck:
    if not isinstance(raw, Mapping):
        raise _unparsable("check must be an object")
    deterministic = raw.get("deterministic")
    if not isinstance(deterministic, bool):
        raise _unparsable("check.deterministic must be a bool")
    scene_id = raw.get("scene_id")
    if scene_id is not None and not isinstance(scene_id, str):
        raise _unparsable("check.scene_id must be a string or null")
    detail = raw.get("detail", "")
    if not isinstance(detail, str):
        raise _unparsable("check.detail must be a string")
    verdict = _verdict(raw.get("verdict"), "check.verdict")
    try:
        return QualityCheck(
            gate=_enum(QualityGate, raw.get("gate"), "check.gate"),
            verdict=verdict,
            reason_code=_enum(QualityReason, raw.get("reason_code"), "check.reason_code"),
            detail=detail,
            deterministic=deterministic,
            scene_id=scene_id,
        )
    except ValueError as err:
        if isinstance(err, QualityReportError):
            raise
        raise _unparsable(str(err)) from None


def parse_quality_report(raw: object) -> QualityReport:
    """JSON 文字列・bytes・dict から取り込む。不備は必ず QualityReportError（None を返さない）。"""
    data: object = raw
    if isinstance(raw, bytes | bytearray):
        try:
            data = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _unparsable("report is not valid utf-8") from None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, RecursionError):
            # 深くネストした入力は RecursionError。例外にせず取り込み失敗にする
            raise _unparsable("report is not valid json") from None
    if not isinstance(data, Mapping):
        raise _unparsable("report must be an object")

    version = data.get("schema_version")
    # 型まで厳密に int（1.0 / True / "1" は別物）。値は写さない
    if type(version) is not int or version != QUALITY_REPORT_SCHEMA_VERSION:
        raise QualityReportError(
            QualityReason.REPORT_SCHEMA_MISMATCH,
            f"schema_version mismatch: {describe(version)}",
        )
    sha = _str(data, "final_video_sha256")
    if not _SHA256.match(sha):
        raise _unparsable("final_video_sha256 must be 64 lowercase hex chars")
    stamp = _str(data, "generated_at")
    try:
        generated_at = datetime.fromisoformat(stamp)
    except ValueError:
        raise _unparsable("generated_at is not an iso datetime") from None
    if generated_at.tzinfo is None:
        raise _unparsable("generated_at must carry a timezone")
    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list):
        raise _unparsable("checks must be a list")
    report = QualityReport(
        episode_id=_str(data, "episode_id"),
        final_video_sha256=sha,
        input_hash=_str(data, "input_hash"),
        generated_at=generated_at,
        generator_version=_str(data, "generator_version"),
        reviewer_version=_str(data, "reviewer_version"),
        verdict=_verdict(data.get("verdict"), "verdict"),
        checks=tuple(_parse_check(c) for c in raw_checks),
    )
    validate_quality_report(report)
    return report


def validate_quality_report(report: object) -> None:
    """直接構築された report にも取り込みと同じ検査をかける（型・形式）。不備は QualityReportError。

    集計との整合（verdict の再計算）と、final_video / input_hash への結び付きは
    ``domain.quality`` の責務。ここは形だけを見る。
    """
    if not isinstance(report, QualityReport):
        raise _unparsable("not a QualityReport")
    for name in ("episode_id", "input_hash", "generator_version", "reviewer_version"):
        value = getattr(report, name)
        if not isinstance(value, str) or not value.strip():
            raise _unparsable(f"{name} must be a non-empty string")
    if not isinstance(report.final_video_sha256, str) or not _SHA256.match(
        report.final_video_sha256
    ):
        raise _unparsable("final_video_sha256 must be 64 lowercase hex chars")
    if type(report.schema_version) is not int:
        raise QualityReportError(QualityReason.REPORT_SCHEMA_MISMATCH, "schema_version must be int")
    if not isinstance(report.verdict, QualityVerdict):
        raise QualityReportError(QualityReason.REPORT_UNKNOWN_VERDICT, "verdict is not a verdict")
    if not isinstance(report.generated_at, datetime):
        raise _unparsable("generated_at must be a datetime")
    if not isinstance(report.checks, tuple):
        raise _unparsable("checks must be a tuple")
    for check in report.checks:
        _validate_check(check)


def _validate_check(check: object) -> None:
    if not isinstance(check, QualityCheck):
        raise _unparsable("check must be a QualityCheck")
    if (
        not isinstance(check.gate, QualityGate)
        or not isinstance(check.reason_code, QualityReason)
        or not isinstance(check.deterministic, bool)
        or not isinstance(check.detail, str)
        or not (check.scene_id is None or isinstance(check.scene_id, str))
    ):
        raise _unparsable("check has a field of the wrong type")
    if not isinstance(check.verdict, QualityVerdict):
        raise QualityReportError(QualityReason.REPORT_UNKNOWN_VERDICT, "check.verdict invalid")
    if (check.verdict is QualityVerdict.PASS) != (check.reason_code is QualityReason.OK):
        raise _unparsable("a check is PASS if and only if its reason_code is OK")
