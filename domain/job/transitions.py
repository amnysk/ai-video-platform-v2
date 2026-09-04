"""Job状態機械と、失敗クラスからEpisode事象への写像（docs/domain/job.md）。"""

from __future__ import annotations

from enum import StrEnum

from contracts.states import JOB_TERMINAL_STATUSES, FailureClass, JobStatus
from domain.episode.transitions import EpisodeEvent, Rejected

__all__ = [
    "JOB_TERMINAL_STATUSES",
    "JOB_TRANSITIONS",
    "JobEvent",
    "episode_event_for_failure",
    "may_retry",
    "transition_job",
]


class JobEvent(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    RETRY_ADMITTED = "retry_admitted"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    SKIPPED = "skipped"


JOB_TRANSITIONS: dict[tuple[JobStatus, JobEvent], JobStatus] = {
    (JobStatus.QUEUED, JobEvent.STARTED): JobStatus.RUNNING,
    (JobStatus.QUEUED, JobEvent.SKIPPED): JobStatus.SKIPPED,
    # Activityが running を書く前に失敗しうる（例: 開始直後のDB障害）。
    (JobStatus.QUEUED, JobEvent.RETRYABLE_FAILURE): JobStatus.RETRYABLE_FAILED,
    (JobStatus.QUEUED, JobEvent.PERMANENT_FAILURE): JobStatus.TERMINAL_FAILED,
    (JobStatus.RUNNING, JobEvent.SUCCEEDED): JobStatus.SUCCEEDED,
    (JobStatus.RUNNING, JobEvent.SKIPPED): JobStatus.SKIPPED,
    (JobStatus.RUNNING, JobEvent.RETRYABLE_FAILURE): JobStatus.RETRYABLE_FAILED,
    (JobStatus.RUNNING, JobEvent.PERMANENT_FAILURE): JobStatus.TERMINAL_FAILED,
    (JobStatus.RETRYABLE_FAILED, JobEvent.RETRY_ADMITTED): JobStatus.RUNNING,
    (JobStatus.RETRYABLE_FAILED, JobEvent.ATTEMPTS_EXHAUSTED): JobStatus.TERMINAL_FAILED,
    (JobStatus.RETRYABLE_FAILED, JobEvent.PERMANENT_FAILURE): JobStatus.TERMINAL_FAILED,
}


def transition_job(current: JobStatus, event: JobEvent) -> JobStatus | Rejected:
    target = JOB_TRANSITIONS.get((current, event))
    if target is None:
        return Rejected(reason=f"job transition rejected: {current.value} + {event.value}")
    return target


def may_retry(*, attempts: int, max_attempts: int) -> bool:
    """まだ試行枠が残っているか。``attempts`` は実施済みの試行回数。"""
    return attempts < max_attempts


def episode_event_for_failure(failure_class: FailureClass) -> EpisodeEvent:
    """失敗クラス -> Episode事象。

    INV-12: ``PERMANENT`` 以外は terminal failed へ向かう事象を返さない。
    """
    match failure_class:
        case FailureClass.PERMANENT:
            return EpisodeEvent.PERMANENT_FAILURE
        case FailureClass.NEEDS_INPUT:
            return EpisodeEvent.NEEDS_INPUT_FAILURE
        case FailureClass.TRANSIENT | FailureClass.RETRYABLE:
            return EpisodeEvent.RETRYABLE_FAILURE
    raise AssertionError(f"unhandled failure class: {failure_class}")


def job_event_for_failure(failure_class: FailureClass) -> JobEvent:
    if failure_class in {FailureClass.TRANSIENT, FailureClass.RETRYABLE}:
        return JobEvent.RETRYABLE_FAILURE
    return JobEvent.PERMANENT_FAILURE
