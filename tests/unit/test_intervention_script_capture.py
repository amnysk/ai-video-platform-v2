"""台本 Activity の実行記録（JP-MAS-001 §6/§7、ADR-0041）。

- legacy（行なし・mode=legacy）の Episode では何も記録せず、旧経路の結果を変えない
- 新方式（mode != legacy）では、成果物と同じ transaction で agent_run・判断記録を残し、
  記録できなければ Activity を失敗させる（記録の無い成果物を下流へ流さない）
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from contracts.intervention import (
    AgentRole,
    InterventionEvent,
    InterventionMode,
    RationaleKind,
)
from contracts.states import ArtifactType, JobType, ProviderCall
from domain.errors import NeedsInputError
from infrastructure.db.models import (
    AgentRunRow,
    ArtifactMetadataRow,
    DecisionRecordRow,
    EpisodeControlRow,
    EpisodeEventRow,
    RevisionLinkRow,
)
from infrastructure.db.repositories import (
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.intervention.capture import AGENT_RUNS_PREFIX, AgentRunCaptureError
from infrastructure.intervention.store import ControlChange, InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryGenerator
from workers.planning.activities import CreateJobRequest, GenerateScriptRequest, ScriptActivities

SCRIPT_JSON = json.dumps(
    {
        "language": "ja",
        "title": "二本差し",
        "hook": "なぜ二本なのか",
        "scenes": [
            {"id": f"s{i}", "narration": n, "visual": "close-up", "duration_ms": 10_000}
            for i, n in enumerate(("武士は刀を二本差した。", "一本は戦のため。", "身分の証。"), 1)
        ],
    },
    ensure_ascii=False,
)

NEW_TABLES = (AgentRunRow, DecisionRecordRow, RevisionLinkRow, EpisodeControlRow, EpisodeEventRow)


class _Store(InMemoryArtifactStore):
    """``agent-runs/`` への書き込みだけを壊せる store。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_capture = False

    async def put_json(self, key: str, payload: Any):  # type: ignore[override]
        if self.fail_capture and key.startswith(AGENT_RUNS_PREFIX):
            raise OSError("minio down")
        return await super().put_json(key, payload)

    def object_keys(self) -> list[str]:
        return sorted(self._objects)


async def _episode(session_factory, mode: InterventionMode | None) -> str:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="応仁の乱")
        if mode is not None:
            store = InterventionStore(session)
            await store.lock_control(episode.id)
            await store.update_control(episode.id, ControlChange(mode=mode))
        await session.commit()
        return episode.id


def _activities(session_factory, store, generator) -> ScriptActivities:
    return ScriptActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        bucket="artifacts",
        generator_id="fake",
        model="configured-model",
        timeout_seconds=5,
    )


async def _run(activities: ScriptActivities, episode_id: str, *, job_id: str | None = None):
    if job_id is None:
        job_id = await activities.create_script_job(
            CreateJobRequest(episode_id=episode_id, max_attempts=3)
        )
    return await activities.generate_script(
        GenerateScriptRequest(episode_id=episode_id, job_id=job_id, round=1)
    )


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.parametrize("mode", [None, InterventionMode.LEGACY])
async def test_legacy_episodes_get_no_capture_and_no_records(session_factory, mode) -> None:
    episode_id = await _episode(session_factory, mode)
    before = {m.__tablename__: await _count(session_factory, m) for m in NEW_TABLES}
    store = _Store()
    generator = FakeStoryGenerator(output=SCRIPT_JSON)

    result = await _run(_activities(session_factory, store, generator), episode_id)

    assert result.reused is False
    after = {m.__tablename__: await _count(session_factory, m) for m in NEW_TABLES}
    assert after == before  # 新しい表に1行も足さない（行の無い Episode は行を作らない）
    assert not [k for k in store.object_keys() if k.startswith(AGENT_RUNS_PREFIX)]
    # 旧経路の保存物は台本本体と生出力の2つだけ
    assert len(store.object_keys()) == 2


async def test_new_method_records_the_run_and_the_decision_with_the_artifact(
    session_factory,
) -> None:
    episode_id = await _episode(session_factory, InterventionMode.CO_PRODUCTION)
    store = _Store()
    generator = FakeStoryGenerator(output=SCRIPT_JSON)

    result = await _run(_activities(session_factory, store, generator), episode_id)

    async with session_factory() as session:
        intervention = InterventionStore(session)
        runs = await intervention.list_agent_runs(episode_id=episode_id)
        decisions = await intervention.list_decisions(episode_id=episode_id)
        events = await intervention.list_events(episode_id)
        reservations = await ProviderReservationRepository(session).list_for_episode_provider(
            episode_id, ProviderCall.CODEX_SCRIPT
        )
    assert len(runs) == 1 and len(decisions) == 1 and len(reservations) == 1
    run, decision, reservation = runs[0], decisions[0], reservations[0]
    assert run.role is AgentRole.WRITER_DIRECTOR
    assert run.step == "write_script"
    assert run.output_revision_id == result.artifact_id
    assert run.model == "configured-model"
    assert run.token_usage is None
    assert [t["id"] for t in run.tool_invocations] == [reservation.id]
    snapshot = json.loads(await store.get_bytes(run.snapshot_object_key))
    assert snapshot["prompt"] == generator.requests[0].prompt
    assert snapshot["prompt_source"] == "captured"
    assert snapshot["response"]["text"] == SCRIPT_JSON
    assert snapshot["model_source"] == "configured"
    assert decision.decision_type == "write_script"
    assert decision.result_revision_id == result.artifact_id
    assert decision.agent_run_id == run.agent_run_id
    assert decision.payload["rationale_kind"] == RationaleKind.NONE.value
    assert decision.payload["input_snapshot_ref"] == run.snapshot_object_key
    assert decision.payload["tool_invocation_refs"] == [f"provider_reservation:{reservation.id}"]
    assert len(decision.payload["candidates"]) == 1
    assert [e.event_name for e in events] == [
        InterventionEvent.AGENT_RUN_RECORDED,
        InterventionEvent.DECISION_RECORDED,
    ]


async def test_capture_failure_fails_the_activity_and_records_no_artifact(
    session_factory,
) -> None:
    episode_id = await _episode(session_factory, InterventionMode.AUTO_INTERVENABLE)
    store = _Store()
    store.fail_capture = True
    generator = FakeStoryGenerator(output=SCRIPT_JSON)
    activities = _activities(session_factory, store, generator)

    with pytest.raises(AgentRunCaptureError) as info:
        await _run(activities, episode_id)

    assert isinstance(info.value, NeedsInputError)
    assert await _count(session_factory, ArtifactMetadataRow) == 0  # 下流へ流す成果物が無い
    assert await _count(session_factory, AgentRunRow) == 0
    async with session_factory() as session:
        (reservation,) = await ProviderReservationRepository(session).list_for_episode_provider(
            episode_id, ProviderCall.CODEX_SCRIPT
        )
    # 生出力は台帳の evidence として残る → 再開は LLM を呼び直さない
    assert reservation.raw_output_key is not None

    # 再開（同じ round・同じ冪等キー）: 保存済みの生出力から続け、prompt は組み直して記録する
    store.fail_capture = False
    async with session_factory() as session:
        job = await JobRepository(session).create(
            episode_id=episode_id, type=JobType.WRITE_SCRIPT, max_attempts=3
        )
        await session.commit()
    result = await _run(activities, episode_id, job_id=job.id)

    assert generator.calls == 1
    async with session_factory() as session:
        (run,) = await InterventionStore(session).list_agent_runs(episode_id=episode_id)
    snapshot = json.loads(await store.get_bytes(run.snapshot_object_key))
    assert snapshot["prompt_source"] == "re_rendered"
    assert snapshot["prompt"] == generator.requests[0].prompt
    assert snapshot["response"]["text"] == SCRIPT_JSON
    assert run.output_revision_id == result.artifact_id


async def test_reusing_an_existing_artifact_does_not_add_runs(session_factory) -> None:
    episode_id = await _episode(session_factory, InterventionMode.CO_PRODUCTION)
    store = _Store()
    activities = _activities(session_factory, store, FakeStoryGenerator(output=SCRIPT_JSON))
    await _run(activities, episode_id)
    again = await _run(activities, episode_id)
    assert again.reused is True
    assert await _count(session_factory, AgentRunRow) == 1
    assert await _count(session_factory, DecisionRecordRow) == 1
    async with session_factory() as session:
        artifacts = await session.scalars(
            select(ArtifactMetadataRow).where(
                ArtifactMetadataRow.artifact_type == ArtifactType.SCRIPT.value
            )
        )
        assert len(list(artifacts)) == 1
