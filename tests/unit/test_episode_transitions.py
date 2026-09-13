"""Episode状態遷移（docs/domain/state-transitions.md の表が権威）。"""

from __future__ import annotations

import pytest

from contracts.states import EPISODE_TERMINAL_STATUSES, EpisodeStatus
from domain.episode.transitions import (
    EPISODE_TRANSITIONS,
    EpisodeEvent,
    Rejected,
    transition_episode,
)


def test_happy_path_of_the_skeleton_workflow() -> None:
    """今回の縦切り: planned -> in_progress -> completed。"""
    s = EpisodeStatus.PLANNED
    s = transition_episode(s, EpisodeEvent.WORKFLOW_STARTED)
    assert s is EpisodeStatus.IN_PROGRESS
    s = transition_episode(s, EpisodeEvent.SKELETON_COMPLETED)
    assert s is EpisodeStatus.COMPLETED


@pytest.mark.parametrize(
    ("current", "event"),
    [
        (EpisodeStatus.PLANNED, EpisodeEvent.SKELETON_COMPLETED),  # 工程を飛ばせない
        (EpisodeStatus.COMPLETED, EpisodeEvent.WORKFLOW_STARTED),  # terminalから戻らない
        (EpisodeStatus.FAILED, EpisodeEvent.RESUMED),
        (EpisodeStatus.CANCELLED, EpisodeEvent.WORKFLOW_STARTED),
        (EpisodeStatus.PLANNED, EpisodeEvent.RETRYABLE_FAILURE),
        (EpisodeStatus.UPLOADED, EpisodeEvent.SKELETON_COMPLETED),
    ],
)
def test_invalid_transitions_are_rejected(current: EpisodeStatus, event: EpisodeEvent) -> None:
    result = transition_episode(current, event)
    assert isinstance(result, Rejected)
    assert current.value in result.reason and event.value in result.reason


def test_rejection_is_a_value_not_an_exception() -> None:
    """遷移規則3: 不正な遷移は例外ではなく Rejected を返す。"""
    assert isinstance(transition_episode(EpisodeStatus.FAILED, EpisodeEvent.RESUMED), Rejected)


def test_terminal_statuses_have_no_outgoing_transitions() -> None:
    for status in EPISODE_TERMINAL_STATUSES:
        outgoing = [k for k in EPISODE_TRANSITIONS if k[0] is status]
        assert not outgoing, f"{status} is terminal but has outgoing transitions"


def test_every_non_terminal_status_has_an_exit() -> None:
    """出口の無い状態を作らない（state-transitions.md「出口の保証」）。"""
    for status in EpisodeStatus:
        if status in EPISODE_TERMINAL_STATUSES:
            continue
        assert any(k[0] is status for k in EPISODE_TRANSITIONS), f"{status} has no exit"


def test_every_non_terminal_status_can_reach_a_terminal_status() -> None:
    reachable_from: dict[EpisodeStatus, set[EpisodeStatus]] = {}
    for (src, _event), dst in EPISODE_TRANSITIONS.items():
        reachable_from.setdefault(src, set()).add(dst)

    for start in EpisodeStatus:
        if start in EPISODE_TERMINAL_STATUSES:
            continue
        seen: set[EpisodeStatus] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(reachable_from.get(node, set()))
        assert seen & EPISODE_TERMINAL_STATUSES, f"{start} cannot reach a terminal status"


def test_cancel_is_available_from_every_non_terminal_status() -> None:
    for status in EpisodeStatus:
        if status in EPISODE_TERMINAL_STATUSES:
            continue
        assert transition_episode(status, EpisodeEvent.CANCELLED) is EpisodeStatus.CANCELLED


def test_storyboard_stage_rows_of_adr_0015() -> None:
    """script_ready -> in_progress -> storyboard_ready -> in_progress（ADR-0015）。"""
    s = transition_episode(EpisodeStatus.SCRIPT_READY, EpisodeEvent.STAGE_ADMITTED)
    assert s is EpisodeStatus.IN_PROGRESS
    s = transition_episode(s, EpisodeEvent.STORYBOARD_READY)
    assert s is EpisodeStatus.STORYBOARD_READY
    assert s not in EPISODE_TERMINAL_STATUSES
    s = transition_episode(s, EpisodeEvent.STAGE_ADMITTED)
    assert s is EpisodeStatus.IN_PROGRESS


@pytest.mark.parametrize(
    ("current", "event"),
    [
        (EpisodeStatus.SCRIPT_READY, EpisodeEvent.STORYBOARD_READY),  # 工程を飛ばせない
        (EpisodeStatus.PLANNED, EpisodeEvent.STORYBOARD_READY),
        (EpisodeStatus.STORYBOARD_READY, EpisodeEvent.SCRIPT_READY),
        (EpisodeStatus.STORYBOARD_READY, EpisodeEvent.STORYBOARD_READY),
    ],
)
def test_invalid_storyboard_transitions_are_rejected(
    current: EpisodeStatus, event: EpisodeEvent
) -> None:
    assert isinstance(transition_episode(current, event), Rejected)
