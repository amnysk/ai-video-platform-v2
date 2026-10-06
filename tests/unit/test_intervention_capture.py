"""AgentRun / ContextSnapshot の捕捉（JP-MAS-001 §6/§7、ADR-0041、capture.py）。

SQLite + InMemoryArtifactStore。実 LLM は呼ばない（FakeStoryGenerator）。
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from sqlalchemy import func, select

from contracts.intervention import AgentRole, AgentRunOutcome, InterventionEvent
from domain.artifact.hashing import sha256_hex
from domain.errors import NeedsInputError
from domain.script.ports import GenerationRequest
from infrastructure.db.models import AgentRunRow
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention.capture import (
    AGENT_RUNS_PREFIX,
    MODEL_SOURCE_CONFIGURED,
    AgentRunCaptureError,
    AgentRunRecorder,
    ExchangeRecordingGenerator,
    PromptSource,
    RunCapture,
    agent_run_id_for,
    recording_exchanges,
)
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryGenerator


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


def _capture(episode_id: str, *, key: str = "run-key-1", prompt: str = "PROMPT") -> RunCapture:
    return RunCapture(
        idempotency_key=key,
        role=AgentRole.WRITER_DIRECTOR,
        step="write_script",
        model="gpt-x",
        prompt_version="write_script@3",
        prompt=prompt,
        prompt_source=PromptSource.CAPTURED,
        input_refs={"topic_plan_id": None, "topic": "t"},
        request={"prompt": prompt, "timeout_seconds": 5},
        response={"text": '{"title": "x"}', "provider_id": "codex"},
        tool_invocations=({"kind": "provider_reservation", "id": "r-1"},),
        episode_id=episode_id,
    )


class _FailingStore(InMemoryArtifactStore):
    async def put_json(self, key, payload):  # type: ignore[override]
        raise OSError("minio down")


async def test_snapshot_is_written_under_the_run_prefix_and_the_row_points_at_it(session) -> None:
    episode_id = await _episode(session)
    store = InMemoryArtifactStore()
    recorder = AgentRunRecorder(store)

    run = await recorder.record(session, _capture(episode_id))
    await session.commit()

    assert run.agent_run_id == agent_run_id_for("run-key-1")
    prefix = f"{AGENT_RUNS_PREFIX}/{episode_id}/{run.agent_run_id}/"
    assert run.snapshot_object_key.startswith(prefix)
    assert run.request_object_key and run.request_object_key.startswith(prefix)
    assert run.response_object_key and run.response_object_key.startswith(prefix)
    body = await store.get_bytes(run.snapshot_object_key)
    assert sha256_hex(body) == run.snapshot_sha256
    snapshot = json.loads(body)
    assert snapshot["prompt"] == "PROMPT"
    assert snapshot["prompt_source"] == "captured"
    assert snapshot["model"] == "gpt-x"
    assert snapshot["model_source"] == MODEL_SOURCE_CONFIGURED
    assert snapshot["prompt_version"] == "write_script@3"
    assert snapshot["input_refs"]["topic"] == "t"
    assert snapshot["request"]["prompt"] == "PROMPT"
    assert snapshot["response"]["text"] == '{"title": "x"}'
    assert run.role is AgentRole.WRITER_DIRECTOR
    assert run.outcome is AgentRunOutcome.SUCCEEDED
    assert run.tool_invocations == [{"kind": "provider_reservation", "id": "r-1"}]
    events = await InterventionStore(session).list_events(episode_id)
    assert [e.event_name for e in events] == [InterventionEvent.AGENT_RUN_RECORDED]


async def test_token_usage_stays_null_unless_it_was_parsed(session) -> None:
    episode_id = await _episode(session)
    run = await AgentRunRecorder(InMemoryArtifactStore()).record(session, _capture(episode_id))
    assert run.token_usage is None


async def test_activity_retry_with_the_same_key_does_not_duplicate_the_run(session) -> None:
    episode_id = await _episode(session)
    store = InMemoryArtifactStore()
    first = await AgentRunRecorder(store).record(session, _capture(episode_id))
    await session.commit()
    again = await AgentRunRecorder(store).record(session, _capture(episode_id))
    await session.commit()
    assert again.agent_run_id == first.agent_run_id
    count = await session.scalar(select(func.count()).select_from(AgentRunRow))
    assert count == 1


async def test_secrets_are_redacted_before_the_snapshot_is_stored(session) -> None:
    episode_id = await _episode(session)
    store = InMemoryArtifactStore()
    secret_prompt = (
        "use Authorization: Bearer abcdefghijklmnop12345 and key sk-abcdefghijklmnop1234 "
        "see https://bucket.example.com/x.png?X-Amz-Signature=deadbeef"
    )
    run = await AgentRunRecorder(store).record(session, _capture(episode_id, prompt=secret_prompt))
    for key in (run.snapshot_object_key, run.request_object_key, run.response_object_key):
        assert key is not None
        text = (await store.get_bytes(key)).decode()
        assert "abcdefghijklmnop12345" not in text
        assert "sk-abcdefghijklmnop1234" not in text
        assert "X-Amz-Signature" not in text
    snapshot = json.loads(await store.get_bytes(run.snapshot_object_key))
    assert snapshot["redaction_applied"] is True


async def test_a_storage_failure_is_a_capture_error_and_writes_no_row(session) -> None:
    episode_id = await _episode(session)
    with pytest.raises(AgentRunCaptureError) as info:
        await AgentRunRecorder(_FailingStore()).record(session, _capture(episode_id))
    # 人の判断へ回す（自動の再ラウンドで有料呼び出しを増やさない）
    assert isinstance(info.value, NeedsInputError)
    count = await session.scalar(select(func.count()).select_from(AgentRunRow))
    assert count == 0


async def test_capture_needs_a_scope() -> None:
    capture = _capture("x")
    unscoped = dataclasses.replace(capture, episode_id=None)
    with pytest.raises(ValueError):
        await AgentRunRecorder(InMemoryArtifactStore()).write(unscoped)


async def test_exchange_recorder_is_transparent_outside_a_tape() -> None:
    inner = FakeStoryGenerator(output="out")
    wrapped = ExchangeRecordingGenerator(inner)
    request = GenerationRequest(episode_id="e", prompt="p", output_schema=None, timeout_seconds=1)
    result = await wrapped.generate(request)
    assert result.text == "out"
    with recording_exchanges() as tape:
        await wrapped.generate(request)
    assert len(tape.exchanges) == 1
    assert tape.exchanges[0].request.prompt == "p"
    assert tape.exchanges[0].result is not None and tape.exchanges[0].result.text == "out"
    # tape の外の呼び出しは記録されない
    await wrapped.generate(request)
    assert len(tape.exchanges) == 1


async def test_exchange_recorder_keeps_the_request_of_a_failed_call() -> None:
    wrapped = ExchangeRecordingGenerator(FakeStoryGenerator(fail_times=1))
    request = GenerationRequest(episode_id="e", prompt="p", output_schema=None, timeout_seconds=1)
    with recording_exchanges() as tape, pytest.raises(RuntimeError):
        await wrapped.generate(request)
    assert tape.exchanges[0].result is None
    assert tape.exchanges[0].error == "RuntimeError"
