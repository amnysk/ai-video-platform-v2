"""Job状態遷移と、失敗クラスからEpisode事象への写像。"""

from __future__ import annotations

import pytest

from contracts.states import FailureClass, JobStatus
from domain.episode.transitions import EpisodeEvent, Rejected
from domain.job.transitions import (
    JOB_TERMINAL_STATUSES,
    JobEvent,
    episode_event_for_failure,
    may_retry,
    transition_job,
)


def test_happy_path() -> None:
    s = JobStatus.QUEUED
    s = transition_job(s, JobEvent.STARTED)
    assert s is JobStatus.RUNNING
    s = transition_job(s, JobEvent.SUCCEEDED)
    assert s is JobStatus.SUCCEEDED


def test_retryable_failure_then_retry_then_success() -> None:
    s = transition_job(JobStatus.RUNNING, JobEvent.RETRYABLE_FAILURE)
    assert s is JobStatus.RETRYABLE_FAILED
    s = transition_job(s, JobEvent.RETRY_ADMITTED)
    assert s is JobStatus.RUNNING
    assert transition_job(s, JobEvent.SUCCEEDED) is JobStatus.SUCCEEDED


def test_attempts_exhausted_becomes_terminal_failed() -> None:
    s = transition_job(JobStatus.RETRYABLE_FAILED, JobEvent.ATTEMPTS_EXHAUSTED)
    assert s is JobStatus.TERMINAL_FAILED


@pytest.mark.parametrize(
    ("current", "event"),
    [
        (JobStatus.QUEUED, JobEvent.SUCCEEDED),
        (JobStatus.SUCCEEDED, JobEvent.STARTED),
        (JobStatus.TERMINAL_FAILED, JobEvent.RETRY_ADMITTED),
        (JobStatus.RUNNING, JobEvent.RETRY_ADMITTED),
    ],
)
def test_invalid_job_transitions_are_rejected(current: JobStatus, event: JobEvent) -> None:
    assert isinstance(transition_job(current, event), Rejected)


def test_job_terminal_statuses_have_no_outgoing_transitions() -> None:
    from domain.job.transitions import JOB_TRANSITIONS

    for status in JOB_TERMINAL_STATUSES:
        assert not [k for k in JOB_TRANSITIONS if k[0] is status]


@pytest.mark.parametrize(
    ("attempts", "max_attempts", "expected"),
    [(1, 3, True), (2, 3, True), (3, 3, False), (4, 3, False)],
)
def test_may_retry_respects_max_attempts(attempts: int, max_attempts: int, expected: bool) -> None:
    assert may_retry(attempts=attempts, max_attempts=max_attempts) is expected


@pytest.mark.parametrize(
    ("failure_class", "expected_event"),
    [
        (FailureClass.TRANSIENT, EpisodeEvent.RETRYABLE_FAILURE),
        (FailureClass.RETRYABLE, EpisodeEvent.RETRYABLE_FAILURE),
        (FailureClass.NEEDS_INPUT, EpisodeEvent.NEEDS_INPUT_FAILURE),
        (FailureClass.PERMANENT, EpisodeEvent.PERMANENT_FAILURE),
    ],
)
def test_failure_class_maps_to_episode_event(
    failure_class: FailureClass, expected_event: EpisodeEvent
) -> None:
    assert episode_event_for_failure(failure_class) is expected_event
