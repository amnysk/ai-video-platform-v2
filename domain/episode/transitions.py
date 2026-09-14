"""Episode状態機械。

**docs/domain/state-transitions.md の遷移表と1対1に対応する唯一のコード表。**
別モジュールで同じ遷移を書き直さないこと（AGENTS.md §8）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from contracts.states import EPISODE_ACTIVE_STATUSES, EpisodeStatus


class EpisodeEvent(StrEnum):
    """遷移の契機。遷移関数は (現在状態, 事象) を受ける（state-transitions.md 遷移の実装規則1）。"""

    WORKFLOW_STARTED = "workflow_started"
    STAGE_SUCCEEDED = "stage_succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    NEEDS_INPUT_FAILURE = "needs_input_failure"
    PERMANENT_FAILURE = "permanent_failure"
    ARTIFACTS_READY = "artifacts_ready"
    SKELETON_COMPLETED = "skeleton_completed"
    SCRIPT_READY = "script_ready"
    STORYBOARD_READY = "storyboard_ready"
    ASSETS_READY = "assets_ready"
    RENDER_READY = "render_ready"
    STAGE_ADMITTED = "stage_admitted"
    RETRY_ADMITTED = "retry_admitted"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    RESUMED = "resumed"
    APPROVED = "approved"
    REJECTED = "rejected"
    UPLOAD_SUCCEEDED = "upload_succeeded"
    METRICS_INGESTED = "metrics_ingested"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Rejected:
    """不正な遷移。例外ではなく値で返す（state-transitions.md 遷移の実装規則2）。"""

    reason: str


_TABLE: dict[tuple[EpisodeStatus, EpisodeEvent], EpisodeStatus] = {
    (EpisodeStatus.PLANNED, EpisodeEvent.WORKFLOW_STARTED): EpisodeStatus.IN_PROGRESS,
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.STAGE_SUCCEEDED): EpisodeStatus.IN_PROGRESS,
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.RETRYABLE_FAILURE): EpisodeStatus.NEEDS_WORK,
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.NEEDS_INPUT_FAILURE): EpisodeStatus.BLOCKED,
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.PERMANENT_FAILURE): EpisodeStatus.FAILED,
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.ARTIFACTS_READY): EpisodeStatus.READY_FOR_REVIEW,
    # ADR-0006: 骨組みworkflowの終端。本番パイプラインでは ARTIFACTS_READY を使う。
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.SKELETON_COMPLETED): EpisodeStatus.COMPLETED,
    # ADR-0011: 台本が生成・検証された。Phase 2 の workflow の終端であり駐機点。
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.SCRIPT_READY): EpisodeStatus.SCRIPT_READY,
    # ADR-0011: 駐機点の出口。Phase 3 の次工程が呼ぶ。
    (EpisodeStatus.SCRIPT_READY, EpisodeEvent.STAGE_ADMITTED): EpisodeStatus.IN_PROGRESS,
    # ADR-0015: storyboard が生成・検証された。Phase 3 の workflow の終端であり駐機点。
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.STORYBOARD_READY): EpisodeStatus.STORYBOARD_READY,
    # ADR-0015: 駐機点の出口。Phase 4 の次工程が呼ぶ。
    (EpisodeStatus.STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED): EpisodeStatus.IN_PROGRESS,
    # ADR-0017: 画像・音声・動画が揃った。Phase 4 の workflow の終端であり駐機点。
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.ASSETS_READY): EpisodeStatus.ASSETS_READY,
    # ADR-0017: 駐機点の出口。production の再実行または Phase 5 Render が呼ぶ。
    (EpisodeStatus.ASSETS_READY, EpisodeEvent.STAGE_ADMITTED): EpisodeStatus.IN_PROGRESS,
    # ADR-0019: 完成動画が技術検査を通って保存された。Phase 5 の workflow の終端であり駐機点。
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.RENDER_READY): EpisodeStatus.RENDER_READY,
    # ADR-0019: 駐機点の出口。再描画（別 profile 等）または Phase 6 Upload が呼ぶ。
    (EpisodeStatus.RENDER_READY, EpisodeEvent.STAGE_ADMITTED): EpisodeStatus.IN_PROGRESS,
    # ADR-0020: private 投稿が成功し受領 Artifact が保存された。Phase 6 の workflow の終端。
    # render_ready から STAGE_ADMITTED で入った upload workflow が呼ぶ。
    (EpisodeStatus.IN_PROGRESS, EpisodeEvent.UPLOAD_SUCCEEDED): EpisodeStatus.UPLOADED,
    (EpisodeStatus.NEEDS_WORK, EpisodeEvent.RETRY_ADMITTED): EpisodeStatus.IN_PROGRESS,
    (EpisodeStatus.NEEDS_WORK, EpisodeEvent.RETRY_BUDGET_EXHAUSTED): EpisodeStatus.BLOCKED,
    (EpisodeStatus.BLOCKED, EpisodeEvent.RESUMED): EpisodeStatus.IN_PROGRESS,
    (EpisodeStatus.READY_FOR_REVIEW, EpisodeEvent.APPROVED): EpisodeStatus.APPROVED,
    (EpisodeStatus.READY_FOR_REVIEW, EpisodeEvent.REJECTED): EpisodeStatus.NEEDS_WORK,
    (EpisodeStatus.APPROVED, EpisodeEvent.UPLOAD_SUCCEEDED): EpisodeStatus.UPLOADED,
    (EpisodeStatus.APPROVED, EpisodeEvent.NEEDS_INPUT_FAILURE): EpisodeStatus.BLOCKED,
    (EpisodeStatus.UPLOADED, EpisodeEvent.METRICS_INGESTED): EpisodeStatus.ANALYZED,
}

# 「任意の非terminal → cancelled」は表の最終行。literalで書き足さず派生で作る。
EPISODE_TRANSITIONS: dict[tuple[EpisodeStatus, EpisodeEvent], EpisodeStatus] = {
    **_TABLE,
    **{
        (status, EpisodeEvent.CANCELLED): EpisodeStatus.CANCELLED
        for status in EPISODE_ACTIVE_STATUSES
    },
}


def transition_episode(current: EpisodeStatus, event: EpisodeEvent) -> EpisodeStatus | Rejected:
    """純粋関数。表に無い組み合わせは ``Rejected`` を返す。"""
    target = EPISODE_TRANSITIONS.get((current, event))
    if target is None:
        return Rejected(reason=f"episode transition rejected: {current.value} + {event.value}")
    return target
