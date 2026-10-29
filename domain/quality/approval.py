"""手動承認の資格（ADR-0081 §9 / INV-129）。純粋。

REVIEW を解放できるのは**所有者の承認**だけ。種別を書かない・不明な承認者・エージェントの委任承認
（``ApproverKind.AGENT_DELEGATED``）は解放しない。fail-closed。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.approval import ApproverKind
from contracts.quality import QualityReason
from contracts.untrusted import is_safe_id


@dataclass(frozen=True, slots=True)
class UploadApprover:
    """手動投稿を指示した人 / 主体。認証（誰か）は API の依存 ``get_upload_approver`` が決める。"""

    kind: ApproverKind
    approver_id: str


def review_release_refusal(approver: object) -> QualityReason | None:
    """REVIEW を解放できないときの理由。解放できるなら ``None``。どんな入力でも例外を投げない。"""
    if not isinstance(approver, UploadApprover):
        return QualityReason.APPROVER_UNKNOWN
    if not isinstance(approver.kind, ApproverKind) or not is_safe_id(approver.approver_id):
        return QualityReason.APPROVER_UNKNOWN
    if approver.kind is not ApproverKind.OWNER:
        return QualityReason.APPROVER_NOT_OWNER
    return None
