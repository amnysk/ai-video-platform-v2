"""品質ゲートの判定の構造化ログ（ADR-0076 §3/§4。ADR-0040 の既存語彙だけを使う）。

理由コード・Episode id・sha256・規則版だけを載せる。入力由来の文字列は写さない（INV-106）。
"""

from __future__ import annotations

import logging

from contracts.log_contract import EventName, LogStage, Outcome
from domain.quality.deterministic import QualityAssessment
from infrastructure.logging.emit import emit, log_guard


def log_quality_decision(
    logger: logging.Logger,
    *,
    path: str,
    episode_id: str,
    final_video_sha256: str,
    rule_version: str,
    assessment: QualityAssessment,
) -> None:
    """``path`` は ``auto`` | ``manual``。拒否は upload.skipped、許可・承認は log.record。"""
    allowed = assessment.decision.allowed
    reason = assessment.decision.reason_code.value
    attributes: dict[str, object] = {
        "path": path,
        "decision": "allowed" if allowed else "refused",
        "final_video_sha256": final_video_sha256,
        "rule_version": rule_version,
        "review_approved": assessment.review_approved,
    }
    if assessment.verdict is not None:
        attributes["verdict"] = assessment.verdict.value
    with log_guard():
        emit(
            logger,
            EventName.LOG_RECORD if allowed else EventName.UPLOAD_SKIPPED,
            logging.WARNING if assessment.review_approved or not allowed else logging.INFO,
            "quality gate (%s): episode=%s %s (%s)",
            path,
            episode_id,
            "allowed" if allowed else "refused",
            reason,
            episode_id=episode_id,
            stage=LogStage.UPLOAD.value,
            outcome=Outcome.SUCCEEDED.value if allowed else Outcome.BLOCKED.value,
            error_code=reason,
            attributes=attributes,
        )
