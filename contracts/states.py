"""状態値とその語彙の**唯一の定義**（AGENTS.md §8）。

ここ以外のモジュールで同じ集合を literal に書き直さないこと。
派生集合は必ずここで導出する。
"""

from __future__ import annotations

from enum import StrEnum


class EpisodeStatus(StrEnum):
    """docs/domain/episode.md の状態表。"""

    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    NEEDS_WORK = "needs_work"
    BLOCKED = "blocked"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    UPLOADED = "uploaded"
    ANALYZED = "analyzed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


EPISODE_TERMINAL_STATUSES: frozenset[EpisodeStatus] = frozenset(
    {
        EpisodeStatus.ANALYZED,
        EpisodeStatus.COMPLETED,
        EpisodeStatus.FAILED,
        EpisodeStatus.CANCELLED,
    }
)

EPISODE_ACTIVE_STATUSES: frozenset[EpisodeStatus] = frozenset(EpisodeStatus) - (
    EPISODE_TERMINAL_STATUSES
)


class JobStatus(StrEnum):
    """docs/domain/job.md の状態表。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    SKIPPED = "skipped"


JOB_TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.TERMINAL_FAILED, JobStatus.SKIPPED}
)


class FailureClass(StrEnum):
    """docs/failure-policy.md §1。例外型から導出する。散文のgrepで決めない。"""

    TRANSIENT = "transient"
    RETRYABLE = "retryable"
    NEEDS_INPUT = "needs_input"
    PERMANENT = "permanent"


#: retryを許してよい失敗クラス。permanent / needs_input はretryしない。
RETRYABLE_FAILURE_CLASSES: frozenset[FailureClass] = frozenset(
    {FailureClass.TRANSIENT, FailureClass.RETRYABLE}
)


class JobType(StrEnum):
    """工程の種類。Phase 1 は骨組みの dummy だけ。"""

    DUMMY = "dummy"


class ArtifactType(StrEnum):
    """Artifactの種類。docs/domain/artifact.md の表に対応する。"""

    DUMMY = "dummy"


DEFAULT_MAX_ATTEMPTS = 3
