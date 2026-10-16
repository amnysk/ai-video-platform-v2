"""発行ヘルパーと文脈（log-contract §5・§7.9 / INV-38）。

理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest

from contracts.log_contract import EventName
from infrastructure.logging import emit, log_context
from infrastructure.logging.context import current_context
from infrastructure.logging.formatter import JsonFormatter, ServiceIdentity


def _capture(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(ServiceIdentity("svc", "test", "sha")))
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_emit_puts_fields_under_the_single_extra_key() -> None:
    logger, stream = _capture("tests.emit.fields")
    emit(
        logger,
        EventName.RESERVATION_RESERVED,
        logging.INFO,
        "reserved %s",
        "r1",
        reservation_id="r1",
        scene_id=None,
        # LogRecord の予約属性と同名でも makeRecord が KeyError を投げない
        message="not a record attribute",
        name="also fine",
    )
    event = json.loads(stream.getvalue())
    assert event["event_name"] == "reservation.reserved"
    assert event["message"] == "reserved r1"
    assert event["reservation_id"] == "r1"
    assert "scene_id" not in event  # None は付けない
    assert event["attributes"]["name"] == "also fine"


class _Broken(logging.Logger):
    def log(self, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError("logger is broken")


def test_emit_never_raises_even_when_the_logger_is_broken() -> None:
    emit(_Broken("broken"), EventName.RESERVATION_SPENT, logging.INFO, "x")


def test_emit_respects_the_level() -> None:
    logger, stream = _capture("tests.emit.level")
    logger.setLevel(logging.INFO)
    emit(logger, EventName.PROVIDER_CALL_STARTED, logging.DEBUG, "poll")
    assert stream.getvalue() == ""


def test_context_is_restored_on_exception() -> None:
    with pytest.raises(ValueError), log_context(episode_id="ep"):
        assert current_context()["episode_id"] == "ep"
        raise ValueError
    assert "episode_id" not in current_context()


async def test_parallel_tasks_do_not_see_each_others_context() -> None:
    """同時に走る2つの Episode の文脈が混ざらない（log-contract §5）。"""
    logger, stream = _capture("tests.emit.parallel")
    gate = asyncio.Event()

    async def work(episode: str) -> None:
        with log_context(episode_id=episode):
            await gate.wait()
            for _ in range(20):
                await asyncio.sleep(0)
                emit(logger, EventName.ACTIVITY_SUCCEEDED, logging.INFO, "done %s", episode)

    tasks = [asyncio.create_task(work(e)) for e in ("ep-a", "ep-b")]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(events) == 40
    assert all(e["message"] == f"done {e['episode_id']}" for e in events)
