"""INV-12: retry可能なJob失敗だけでEpisodeをterminal failedにしない。"""

from __future__ import annotations

import pytest

from contracts.states import EPISODE_TERMINAL_STATUSES, EpisodeStatus, FailureClass
from domain.episode.transitions import transition_episode
from domain.errors import (
    NeedsInputError,
    PermanentError,
    RetryableError,
    TransientError,
    classify_failure,
)
from domain.job.transitions import episode_event_for_failure


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TransientError("net"), FailureClass.TRANSIENT),
        (RetryableError("gate"), FailureClass.RETRYABLE),
        (NeedsInputError("budget"), FailureClass.NEEDS_INPUT),
        (PermanentError("bad schema"), FailureClass.PERMANENT),
    ],
)
def test_failure_class_is_derived_from_exception_type(
    exc: Exception, expected: FailureClass
) -> None:
    """散文のgrepではなく例外型で分類する（failure-policy §1）。"""
    assert classify_failure(exc) is expected


def test_unclassified_failure_becomes_needs_input_not_permanent() -> None:
    """分類できない失敗は自動修復に流さず人間へ（INV-12）。"""
    assert classify_failure(ValueError("who knows")) is FailureClass.NEEDS_INPUT


@pytest.mark.parametrize(
    "failure_class",
    [FailureClass.TRANSIENT, FailureClass.RETRYABLE, FailureClass.NEEDS_INPUT],
)
def test_non_permanent_failures_never_reach_a_terminal_episode_status(
    failure_class: FailureClass,
) -> None:
    event = episode_event_for_failure(failure_class)
    result = transition_episode(EpisodeStatus.IN_PROGRESS, event)
    assert result not in EPISODE_TERMINAL_STATUSES


def test_only_permanent_failure_reaches_failed() -> None:
    event = episode_event_for_failure(FailureClass.PERMANENT)
    assert transition_episode(EpisodeStatus.IN_PROGRESS, event) is EpisodeStatus.FAILED
