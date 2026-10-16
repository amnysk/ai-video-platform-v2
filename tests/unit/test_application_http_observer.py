"""``application.operation.completed``（ADR-0044 §5）。

理由は docs/testing/jpmas2-p2a-http-rationale.md。
"""

from __future__ import annotations

import logging

import pytest

from application.ports import OperationRecord
from apps.api.application_adapters import LoggingOperationObserver
from contracts.application_operations import OperationName
from contracts.intervention import ActorType
from contracts.log_contract import EventName
from tests.support.log_capture import capture_json


def _record(**kw: object) -> OperationRecord:
    base: dict[str, object] = {
        "operation": OperationName.REQUEST_HOLD,
        "actor_type": ActorType.HUMAN,
        "result": "succeeded",
        "duration_ms": 12,
        "episode_id": "ep-1",
        "correlation_id": "corr-1",
        "command_id": "cmd-00000001",
        "workflow_id": "agentic-episode-ep-1",
    }
    return OperationRecord(**{**base, **kw})  # type: ignore[arg-type]


def test_one_line_with_the_documented_fields() -> None:
    with capture_json() as logs:
        LoggingOperationObserver().record(_record())
    [line] = logs.events(EventName.APPLICATION_OPERATION_COMPLETED.value)
    assert line["event_name"] == "application.operation.completed"
    assert line["level"] == "INFO"
    for key, value in {
        "episode_id": "ep-1",
        "correlation_id": "corr-1",
        "command_id": "cmd-00000001",
        "actor_type": "human",
        "workflow_id": "agentic-episode-ep-1",
        "outcome": "succeeded",
        "duration_ms": 12,
    }.items():
        assert line[key] == value, key
    assert line["attributes"]["operation"] == "request_hold"
    assert "error_code" not in line


@pytest.mark.parametrize(
    ("result", "outcome", "level"),
    [
        ("succeeded", "succeeded", "INFO"),
        ("rejected", "rejected", "INFO"),
        ("failed", "failed", "ERROR"),
    ],
)
def test_result_maps_to_outcome_and_level(result: str, outcome: str, level: str) -> None:
    with capture_json() as logs:
        LoggingOperationObserver().record(
            _record(result=result, error_code="invalid_state" if result != "succeeded" else None)
        )
    [line] = logs.events("application.operation.completed")
    assert (line["outcome"], line["level"]) == (outcome, level)
    if result != "succeeded":
        assert line["error_code"] == "invalid_state"


def test_missing_values_are_not_invented() -> None:
    with capture_json() as logs:
        LoggingOperationObserver().record(
            _record(episode_id=None, correlation_id=None, command_id=None, workflow_id=None)
        )
    [line] = logs.events("application.operation.completed")
    for key in ("episode_id", "correlation_id", "command_id", "workflow_id"):
        assert key not in line


def test_a_logging_failure_never_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("log down")

    monkeypatch.setattr("apps.api.application_adapters.emit", boom)
    LoggingOperationObserver().record(_record())  # 例外を投げない（INV-38）


def test_logger_name_is_under_avp_api() -> None:
    with capture_json() as logs:
        LoggingOperationObserver().record(_record())
    [line] = logs.events("application.operation.completed")
    assert line["logger"].startswith("avp.api")
    assert logging.getLogger(line["logger"]) is not None
