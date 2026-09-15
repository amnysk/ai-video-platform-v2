"""投稿済み動画の YouTube 処理状態の分類（ADR-0022）。I/O をしない純粋関数。

- ``PROCESSED``: 処理完了・private・投稿先チャンネル → ``uploaded`` にしてよい
- ``PENDING``: まだ見えない / 受理済みで処理中 → 待って照会し直す（retryable）
- ``FAILED``: 拒否・失敗・削除・チャンネル違い・private でない・未知の状態
  → 人間が判断する（needs_input）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from contracts.upload import UPLOAD_PRIVACY_STATUS
from domain.upload.ports import VideoProcessingState

_REASON_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

TERMINAL_UPLOAD_STATUSES = frozenset({"rejected", "failed", "deleted"})
PENDING_UPLOAD_STATUSES = frozenset({"uploaded"})
FAILED_PROCESSING_STATUSES = frozenset({"failed", "terminated"})


class ProcessingOutcome(StrEnum):
    PROCESSED = "processed"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessingVerdict:
    outcome: ProcessingOutcome
    #: 識別子として安全な短い理由コード（ログ・blocked_reason に載せてよい）
    reason: str


def _code(value: str | None) -> str:
    return value if value is not None and _REASON_RE.match(value) else "unknown"


def classify_processing(state: VideoProcessingState, expected_channel_id: str) -> ProcessingVerdict:
    """状態を判定する。順序: 未発見 → チャンネル → 終端の失敗 → privacy → 完了 / 処理中。"""
    if not state.found:
        return ProcessingVerdict(ProcessingOutcome.PENDING, "not_found_yet")
    if state.channel_id != expected_channel_id:
        return ProcessingVerdict(ProcessingOutcome.FAILED, "channel_mismatch")
    upload = state.upload_status
    if upload == "rejected":
        return ProcessingVerdict(
            ProcessingOutcome.FAILED, f"rejected_{_code(state.rejection_reason)}"
        )
    if upload == "failed":
        return ProcessingVerdict(ProcessingOutcome.FAILED, f"failed_{_code(state.failure_reason)}")
    if upload == "deleted":
        return ProcessingVerdict(ProcessingOutcome.FAILED, "deleted")
    if state.processing_status in FAILED_PROCESSING_STATUSES:
        return ProcessingVerdict(ProcessingOutcome.FAILED, f"processing_{state.processing_status}")
    if upload is not None and upload != "processed" and upload not in PENDING_UPLOAD_STATUSES:
        return ProcessingVerdict(ProcessingOutcome.FAILED, "unknown_upload_status")
    if state.privacy_status != UPLOAD_PRIVACY_STATUS and (
        upload == "processed" or state.privacy_status is not None
    ):
        # INV-19: private 以外は処理中でも止める。完了時は privacy が読めないことも許さない
        return ProcessingVerdict(ProcessingOutcome.FAILED, "not_private")
    if upload == "processed":
        return ProcessingVerdict(ProcessingOutcome.PROCESSED, "processed")
    return ProcessingVerdict(ProcessingOutcome.PENDING, f"pending_{_code(upload)}")


__all__ = [
    "FAILED_PROCESSING_STATUSES",
    "PENDING_UPLOAD_STATUSES",
    "TERMINAL_UPLOAD_STATUSES",
    "ProcessingOutcome",
    "ProcessingVerdict",
    "classify_processing",
]
