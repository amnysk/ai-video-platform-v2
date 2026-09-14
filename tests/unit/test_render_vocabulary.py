"""Render の語彙・遷移・失敗クラス・既定値の単一宣言元（ADR-0019）。"""

from __future__ import annotations

import pathlib
import re

import pytest

import contracts.render as render
import contracts.render_activities as ra
from contracts.states import (
    RENDER_ADMISSIBLE_STATUSES,
    RENDER_TASK_QUEUE,
    RENDER_WORKFLOW,
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobType,
)
from domain import errors
from domain.episode.transitions import EpisodeEvent, Rejected, transition_episode
from domain.errors import (
    NON_RETRYABLE_ERROR_TYPE_NAMES,
    classify_failure,
    failure_class_from_type_name,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_vocabulary_constants() -> None:
    assert EpisodeStatus.RENDER_READY.value == "render_ready"
    assert JobType.RENDER_FINAL_VIDEO.value == "render_final_video"
    assert ArtifactType.FINAL_VIDEO.value == "final_video"
    assert RENDER_WORKFLOW == ("RenderWorkflow", "render")
    assert RENDER_TASK_QUEUE == "render"
    assert (
        frozenset(
            {
                EpisodeStatus.ASSETS_READY,
                EpisodeStatus.RENDER_READY,
                EpisodeStatus.NEEDS_WORK,
                EpisodeStatus.BLOCKED,
            }
        )
        == RENDER_ADMISSIBLE_STATUSES
    )
    assert len(set(ra.RENDER_ACTIVITY_NAMES)) == len(ra.RENDER_ACTIVITY_NAMES) == 4


def test_render_happy_path_parks_at_render_ready() -> None:
    s = transition_episode(EpisodeStatus.ASSETS_READY, EpisodeEvent.STAGE_ADMITTED)
    assert s is EpisodeStatus.IN_PROGRESS
    s = transition_episode(s, EpisodeEvent.RENDER_READY)
    assert s is EpisodeStatus.RENDER_READY
    assert transition_episode(s, EpisodeEvent.STAGE_ADMITTED) is EpisodeStatus.IN_PROGRESS
    assert transition_episode(s, EpisodeEvent.CANCELLED) is EpisodeStatus.CANCELLED


@pytest.mark.parametrize(
    ("current", "event"),
    [
        (EpisodeStatus.ASSETS_READY, EpisodeEvent.RENDER_READY),  # 工程を飛ばせない
        (EpisodeStatus.STORYBOARD_READY, EpisodeEvent.RENDER_READY),
        (EpisodeStatus.RENDER_READY, EpisodeEvent.RENDER_READY),
        (EpisodeStatus.RENDER_READY, EpisodeEvent.ASSETS_READY),
    ],
)
def test_invalid_render_transitions_are_rejected(current, event) -> None:
    assert isinstance(transition_episode(current, event), Rejected)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (errors.DurationReconciliationError("x"), FailureClass.NEEDS_INPUT),
        (errors.VoiceTimelineOverflowError("x"), FailureClass.NEEDS_INPUT),
        (errors.RenderInputMissingError("x"), FailureClass.NEEDS_INPUT),
        (errors.RenderInputStaleError("x"), FailureClass.NEEDS_INPUT),
        (errors.RenderSourceMediaError("x"), FailureClass.NEEDS_INPUT),
        (errors.RenderInputIntegrityError("x"), FailureClass.PERMANENT),
        (errors.RenderEngineFailedError("x"), FailureClass.RETRYABLE),
        (errors.RenderEngineTimeoutError("x"), FailureClass.RETRYABLE),
        (errors.RenderWorkspaceFullError("x"), FailureClass.RETRYABLE),
        (errors.RenderEngineUnavailableError("x"), FailureClass.NEEDS_INPUT),
        (errors.FinalVideoValidationError("x"), FailureClass.NEEDS_INPUT),
        (errors.FinalVideoCorruptError("x"), FailureClass.RETRYABLE),
        (errors.UnknownRenderProfileError("x"), FailureClass.NEEDS_INPUT),
    ],
)
def test_render_exceptions_classify_by_their_base(exc: Exception, expected: FailureClass) -> None:
    assert classify_failure(exc) is expected
    assert failure_class_from_type_name(type(exc).__name__) is expected
    non_retryable = expected in {FailureClass.NEEDS_INPUT, FailureClass.PERMANENT}
    assert (type(exc).__name__ in NON_RETRYABLE_ERROR_TYPE_NAMES) is non_retryable


def test_render_defaults_are_declared_exactly_once() -> None:
    """DEFAULT_RENDER_* は contracts/render.py の1箇所だけで代入する。"""
    expected = {
        "DEFAULT_RENDER_CONCURRENCY": 1,
        "DEFAULT_RENDER_TIMEOUT_SECONDS": 1800,
        "DEFAULT_RENDER_HEARTBEAT_TIMEOUT_SECONDS": 60,
        "DEFAULT_RENDER_MIN_FREE_BYTES": 10 * 1024**3,
        "DEFAULT_RENDER_MAX_FREEZE_MS": 2000,
        "DEFAULT_RENDER_FFMPEG_THREADS": 4,
        "DEFAULT_RENDER_PROFILE_ID": "shorts_vertical",
    }
    for name, value in expected.items():
        assert getattr(render, name) == value
    assignment = re.compile(r"^\s*(DEFAULT_RENDER_\w+)\s*(?::[^=]+)?=", re.MULTILINE)
    owners: dict[str, list[str]] = {}
    for layer in ("apps", "workers", "domain", "infrastructure", "contracts"):
        for path in (REPO / layer).rglob("*.py"):
            for name in assignment.findall(path.read_text(encoding="utf-8")):
                owners.setdefault(name, []).append(str(path.relative_to(REPO)))
    assert set(expected) <= set(owners)
    assert all(paths == ["contracts/render.py"] for paths in owners.values()), owners
