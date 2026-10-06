"""Activity interceptor（log-contract §5・§8 / INV-38）。理由は docs/testing/logging-rationale.md。

- 入力型ごとの明示の対応表で文脈を束縛する（属性名で汎用的に拾わない。Research の ``request_id``
  は依頼 ID なので ``research_request_id`` へ）
- 失敗・cancel を記録し、**同じ例外オブジェクト**をそのまま再送出する（業務の例外を変えない）
- 同時に走る2つの Episode の文脈が混ざらない（実 Worker で確かめる）
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import io
import json
import logging
import typing
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import ExecuteActivityInput, Worker

from contracts.production_activities import ImageSubmitRequest, VoiceGenerateRequest
from contracts.research import ResearchExecuteRequest
from infrastructure.logging.formatter import JsonFormatter, ServiceIdentity
from infrastructure.logging.temporal import (
    ACTIVITY_INPUT_FIELDS,
    ActivityLoggingInterceptor,
    input_fields,
)

ACTIVITY_MODULES = (
    "workers.planning.activities",
    "workers.planning.script_evidence_activities",
    "workers.planning.topic_activities",
    "workers.production.activities",
    "workers.production.scene_recovery_activities",
    "workers.production_image.activities",
    "workers.production_video.activities",
    "workers.production_voice.activities",
    "workers.render.activities",
    "workers.upload.activities",
    "workers.pipeline.activities",
    "workers.storyboard.activities",
    "workers.dummy.activities",
    "workers.research.activities",
)


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(ServiceIdentity("svc", "test", "sha")))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield stream
    root.removeHandler(handler)
    root.setLevel(level)


def _events(stream: io.StringIO, prefix: str = "") -> list[dict[str, Any]]:
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    return [e for e in events if e["event_name"].startswith(prefix)]


def _activity_input_types() -> dict[str, type]:
    found: dict[str, type] = {}
    for module_name in ACTIVITY_MODULES:
        module = importlib.import_module(module_name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            for _, fn in inspect.getmembers(cls, inspect.isfunction):
                if not getattr(fn, "__temporal_activity_definition", None):
                    continue
                hints = typing.get_type_hints(fn)
                for param in list(inspect.signature(fn).parameters)[1:]:
                    t = hints[param]
                    found[f"{t.__module__}.{t.__qualname__}"] = t
    return found


def test_every_activity_input_type_is_in_the_explicit_table() -> None:
    """新しい Activity を足したら対応表にも足す（黙って文脈が付かない状態を作らない）。"""
    missing = sorted(set(_activity_input_types()) - set(ACTIVITY_INPUT_FIELDS))
    assert not missing, missing


def test_the_table_only_names_attributes_that_exist() -> None:
    types = _activity_input_types()
    for name, mapping in ACTIVITY_INPUT_FIELDS.items():
        if name not in types:
            continue
        attrs = {f.name for f in dataclasses.fields(types[name])}
        assert set(mapping) <= attrs, (name, set(mapping) - attrs)


def test_research_request_id_is_not_the_api_request_id() -> None:
    fields = input_fields(ResearchExecuteRequest(request_id="rr-1"))
    assert fields == {"research_request_id": "rr-1"}


def test_voice_scene_id_is_the_script_scene_id() -> None:
    fields = input_fields(
        VoiceGenerateRequest(
            episode_id="ep",
            workflow_id="wf",
            run_id="run",
            script_scene_id="s3",
            storyboard_scene_ids=["sb5", "sb6"],
            storyboard_artifact_id="sba",
            script_artifact_id="sca",
        )
    )
    assert fields["scene_id"] == "s3"
    assert fields["attributes"] == {"storyboard_scene_ids": ["sb5", "sb6"]}


def test_unknown_input_types_bind_nothing() -> None:
    @dataclasses.dataclass
    class Other:
        episode_id: str
        request_id: str

    assert input_fields(Other("ep", "rid")) == {}


# ------------------------------------------------------------------ interceptor を直接呼ぶ


class _Next:
    def __init__(self, outcome: BaseException | None) -> None:
        self.outcome = outcome

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        logging.getLogger("tests.activity.body").info("inside")
        if self.outcome is not None:
            raise self.outcome
        return "ok"


def _info(monkeypatch: pytest.MonkeyPatch) -> None:
    info = type(
        "Info",
        (),
        {
            "activity_id": "7",
            "activity_type": "production.image.submit",
            "attempt": 3,
            "task_queue": "production-image",
            "workflow_id": "production-ep",
            "workflow_run_id": "run-1",
            "workflow_type": "ProductionWorkflow",
        },
    )()
    monkeypatch.setattr(activity, "info", lambda: info)


def _input() -> ExecuteActivityInput:
    request = ImageSubmitRequest(
        episode_id="ep-1",
        workflow_id="production-ep",
        run_id="run-1",
        scene_id="sb6",
        storyboard_artifact_id="sba",
        round=1,
    )
    return ExecuteActivityInput(fn=lambda r: r, args=[request], executor=None, headers={})


async def _run(outcome: BaseException | None, monkeypatch: pytest.MonkeyPatch) -> Any:
    _info(monkeypatch)
    inbound = ActivityLoggingInterceptor().intercept_activity(_Next(outcome))  # type: ignore[arg-type]
    return await inbound.execute_activity(_input())


async def test_success_binds_context_and_records_duration(
    captured: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _run(None, monkeypatch) == "ok"
    body = [e for e in _events(captured) if e["logger"] == "tests.activity.body"][0]
    assert body["episode_id"] == "ep-1" and body["scene_id"] == "sb6"
    assert body["activity_attempt"] == 3 and body["run_id"] == "run-1"
    assert body["storyboard_artifact_id"] == "sba"
    done = _events(captured, "activity.succeeded")[0]
    assert done["outcome"] == "succeeded" and isinstance(done["duration_ms"], float)
    assert _events(captured, "activity.started")[0]["level"] == "DEBUG"


async def test_failure_is_recorded_and_the_same_object_is_reraised(
    captured: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = ApplicationError(
        "ProviderRejectedError: rejected", type="ProviderRejectedError", non_retryable=True
    )
    with pytest.raises(ApplicationError) as caught:
        await _run(err, monkeypatch)
    assert caught.value is err
    failed = _events(captured, "activity.failed")[0]
    assert failed["outcome"] == "failed"
    assert failed["error_type"] == "ProviderRejectedError"
    assert failed["failure_class"] == "needs_input"
    assert failed["retryable"] is False
    assert failed["episode_id"] == "ep-1"


async def test_plain_exceptions_are_retryable_by_temporal(
    captured: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = ConnectionError("db down")
    with pytest.raises(ConnectionError) as caught:
        await _run(err, monkeypatch)
    assert caught.value is err
    failed = _events(captured, "activity.failed")[0]
    assert failed["error_type"] == "ConnectionError" and failed["retryable"] is True


async def test_cancel_is_recorded_as_cancelled_and_propagates(
    captured: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as caught:
        await _run(err, monkeypatch)
    assert caught.value is err
    assert _events(captured, "activity.failed")[0]["outcome"] == "cancelled"


async def test_a_broken_logger_does_not_change_the_activity_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-38: ログの故障で Activity の結果・例外は変わらない。"""

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("logging is broken")

    monkeypatch.setattr(logging.Logger, "log", boom)
    monkeypatch.setattr(logging.Logger, "_log", boom)
    assert await _run(None, monkeypatch) == "ok"
    err = KeyError("x")
    with pytest.raises(KeyError) as caught:
        await _run(err, monkeypatch)
    assert caught.value is err


# ------------------------------------------------------------------ 実 Worker で並列


@activity.defn(name="tests.log.scene")
async def _scene_activity(request: ImageSubmitRequest) -> str:
    for _ in range(5):
        await asyncio.sleep(0.01)
        logging.getLogger("tests.activity.parallel").info("working on %s", request.episode_id)
    return request.episode_id


@workflow.defn(name="TestsLogParallel", sandboxed=False)
class _ParallelWorkflow:
    @workflow.run
    async def run(self, episodes: list[str]) -> list[str]:
        calls = [
            workflow.execute_activity(
                "tests.log.scene",
                ImageSubmitRequest(
                    episode_id=ep,
                    workflow_id="wf",
                    run_id="run",
                    scene_id=f"sb-{ep}",
                    storyboard_artifact_id="sba",
                    round=1,
                ),
                result_type=str,
                start_to_close_timeout=timedelta(seconds=30),
            )
            for ep in episodes
        ]
        return list(await asyncio.gather(*calls))


async def test_two_episodes_in_parallel_do_not_mix(captured: io.StringIO) -> None:
    queue = f"tests-log-{uuid.uuid4().hex[:8]}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=queue,
            workflows=[_ParallelWorkflow],
            activities=[_scene_activity],
            interceptors=[ActivityLoggingInterceptor()],
        ),
    ):
        result = await env.client.execute_workflow(
            _ParallelWorkflow.run, ["ep-a", "ep-b"], id=queue, task_queue=queue
        )
    assert result == ["ep-a", "ep-b"]
    work = [e for e in _events(captured) if e["logger"] == "tests.activity.parallel"]
    assert len(work) == 10
    for event in work:
        assert event["message"] == f"working on {event['episode_id']}"
        assert event["scene_id"] == f"sb-{event['episode_id']}"
    assert {e["episode_id"] for e in _events(captured, "activity.succeeded")} == {"ep-a", "ep-b"}
