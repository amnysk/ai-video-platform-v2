"""品質レポートの鮮度の判定（INV-107）。純粋: 時計は注入される。

レポートは「その時点の、その規則版で、その artifact を検査した」という主張。今の artifact・規則版・
時刻に照らして合わなければ、中身が PASS でも**投稿の根拠にしない**（fail-closed）。
"""

from __future__ import annotations

from datetime import datetime

from contracts.quality import CLOCK_SKEW_TOLERANCE, QualityReason, QualityReport


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def check_freshness(
    report: QualityReport,
    *,
    current_final_video_sha256: str,
    current_input_hash: str,
    current_rule_version: str,
    now: datetime,
) -> QualityReason | None:
    """今のものに結び付いていなければ拒否の理由コード、結び付いていれば ``None``。

    判定順（先に当たったものを返す）: artifact の sha256 → 入力 hash → 規則版 → 時刻の形（aware）→
    未来日付（``CLOCK_SKEW_TOLERANCE`` まで許す）→ 失効（``now >= expires_at``）。
    空の「今の値」は何にも一致しない。
    """
    if not current_final_video_sha256 or report.final_video_sha256 != current_final_video_sha256:
        return QualityReason.REPORT_FINAL_VIDEO_MISMATCH
    if not current_input_hash or report.input_hash != current_input_hash:
        return QualityReason.REPORT_INPUT_HASH_MISMATCH
    if not current_rule_version or report.rule_version != current_rule_version:
        return QualityReason.REPORT_RULE_VERSION_MISMATCH
    if not _aware(report.generated_at) or not _aware(report.expires_at) or not _aware(now):
        return QualityReason.REPORT_TIMESTAMP_INVALID
    if report.generated_at > now + CLOCK_SKEW_TOLERANCE:
        return QualityReason.REPORT_FROM_THE_FUTURE
    if now >= report.expires_at:
        return QualityReason.REPORT_EXPIRED
    return None
