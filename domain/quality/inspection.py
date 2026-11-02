"""検査系の障害の扱い（ADR-0087 §8 / INV-156）。純粋: I/O・時計・sleep を持たない。

品質の問題（動画が悪い）と、検査系の故障（検査を実行できなかった）を区別する。

- ``InspectionFault``: 一時的。I/O・タイムアウト・接続。gate の内側で上限つきで再試行する
- ``InspectionDefect``: 恒久的。契約違反・hash 不一致・別の動画の証跡。再試行しない

どちらも**判定を緩めない**。再試行しても記録できない必須の検査は、これまでどおり REVIEW。
違いは理由コード（``QualityReason.INSPECTION_UNAVAILABLE``）だけで、記録・ログで区別できる。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from contracts.quality import QualityCheck, QualityReason, QualityVerdict
from domain.quality.upload_decision import UploadDecision


class FailureClass(StrEnum):
    TRANSIENT = "transient"  # InspectionFault
    PERMANENT = "permanent"  # InspectionDefect


class InspectionFault(Exception):  # noqa: N818 - 設計書の語彙
    """検査系の一時的な失敗。再試行してよい。"""


class InspectionDefect(Exception):  # noqa: N818 - 設計書の語彙
    """検査の入力・契約の恒久的な不備。再試行しても同じ。"""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """3 回・指数バックオフ・合計 30 秒以内（ADR-0087 §8）。"""

    attempts: int
    base_delay_seconds: float
    factor: float
    max_total_seconds: float

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.base_delay_seconds < 0 or self.factor < 1:
            raise ValueError("retry policy must have attempts >= 1, delay >= 0, factor >= 1")
        if sum(backoff_delays(self)) > self.max_total_seconds:
            raise ValueError("the backoff alone must fit within max_total_seconds")


def backoff_delays(policy: RetryPolicy) -> tuple[float, ...]:
    """試行の間の待ち（試行回数 - 1 個）。"""
    return tuple(policy.base_delay_seconds * policy.factor**i for i in range(policy.attempts - 1))


RETRY_POLICY = RetryPolicy(attempts=3, base_delay_seconds=1.0, factor=2.0, max_total_seconds=30.0)


def refine_hold_reason(decision: UploadDecision, checks: Sequence[QualityCheck]) -> UploadDecision:
    """保留（REVIEW）の原因が検査系の故障だけなら、理由を ``INSPECTION_UNAVAILABLE`` にする。

    - 許可は触らない。他の拒否理由（鮮度・形・BLOCK など）も触らない
    - 品質の保留・BLOCK が 1 つでも混じるなら、本来の理由のまま（品質の問題を隠さない）
    - 拒否を許可にする経路は無い（返すのは同じ拒否か、理由だけ違う拒否）
    """
    if decision.allowed or decision.reason_code is not QualityReason.REPORT_NEEDS_REVIEW:
        return decision
    holds = [c for c in checks if c.verdict in (QualityVerdict.REVIEW, QualityVerdict.BLOCK)]
    if holds and all(c.reason_code is QualityReason.INSPECTION_UNAVAILABLE for c in holds):
        return UploadDecision(False, QualityReason.INSPECTION_UNAVAILABLE)
    return decision
