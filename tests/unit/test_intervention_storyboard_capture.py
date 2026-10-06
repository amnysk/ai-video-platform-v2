"""storyboard Activity の実行記録（JP-MAS-001 §6/§7/§12、ADR-0041）。

prompt は生成器（OpenMontage adapter）の内側で組まれるので、``ExchangeRecordingGenerator`` が
包んだ LLM 呼び出しから実際の prompt を捕捉する。
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from sqlalchemy import func, select

from contracts.intervention import AgentRole, InterventionMode, RevisionLinkKind
from contracts.states import ArtifactType, ProviderCall
from domain.script.ports import GenerationRequest
from domain.storyboard.ports import StoryboardRawResult, StoryboardRequest
from infrastructure.db.models import (
    AgentRunRow,
    ArtifactMetadataRow,
    DecisionRecordRow,
    EpisodeControlRow,
    EpisodeEventRow,
    RevisionLinkRow,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    ProviderReservationRepository,
)
from infrastructure.intervention.capture import (
    AGENT_RUNS_PREFIX,
    AgentRunCaptureError,
    ExchangeRecordingGenerator,
)
from infrastructure.intervention.store import ControlChange, InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryboardGenerator, FakeStoryGenerator
from tests.support.storyboard import create_episode_at_script_ready, good_storyboard
from tests.unit.test_storyboard_activities import admitted_job, make_activities
from workers.storyboard import run_worker
from workers.storyboard.activities import GenerateStoryboardRequest

NEW_TABLES = (AgentRunRow, DecisionRecordRow, RevisionLinkRow, EpisodeControlRow, EpisodeEventRow)


class _Store(InMemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_capture = False

    async def put_json(self, key: str, payload: Any):  # type: ignore[override]
        if self.fail_capture and key.startswith(AGENT_RUNS_PREFIX):
            raise OSError("minio down")
        return await super().put_json(key, payload)

    def object_keys(self) -> list[str]:
        return sorted(self._objects)


class _LlmBackedStoryboard(FakeStoryboardGenerator):
    """本物の adapter と同じく、prompt を内側で組んで ``StoryGenerator`` を1回呼ぶ。"""

    def __init__(self) -> None:
        super().__init__(output=good_storyboard())
        self.llm = ExchangeRecordingGenerator(FakeStoryGenerator(output=good_storyboard()))
        self.prompts: list[str] = []

    async def generate(self, request: StoryboardRequest) -> StoryboardRawResult:
        prompt = f"SCENE DIRECTOR\nscript: {request.script.title}\nschema: {{...}}"
        self.prompts.append(prompt)
        self.calls += 1
        result = await self.llm.generate(
            GenerationRequest(
                episode_id=request.episode_id,
                prompt=prompt,
                output_schema=None,
                timeout_seconds=request.timeout_seconds,
            )
        )
        return StoryboardRawResult(
            text=result.text, provider_id=result.provider_id, model=result.model, raw_log=""
        )


async def _episode(session_factory, store, mode: InterventionMode | None) -> str:
    episode_id = await create_episode_at_script_ready(session_factory, store)
    if mode is not None:
        async with session_factory() as session:
            control = InterventionStore(session)
            await control.lock_control(episode_id)
            await control.update_control(episode_id, ControlChange(mode=mode))
            await session.commit()
    return episode_id


async def _generate(session_factory, store, generator, episode_id: str):
    activities = make_activities(session_factory, store, generator)
    job_id = await admitted_job(activities, episode_id)
    return await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )


async def _counts(session_factory) -> dict[str, int]:
    async with session_factory() as session:
        return {
            m.__tablename__: int(await session.scalar(select(func.count()).select_from(m)) or 0)
            for m in NEW_TABLES
        }


@pytest.mark.parametrize("mode", [None, InterventionMode.LEGACY])
async def test_legacy_storyboard_writes_nothing_new(session_factory, mode) -> None:
    store = _Store()
    episode_id = await _episode(session_factory, store, mode)
    before = await _counts(session_factory)
    generator = _LlmBackedStoryboard()
    result = await _generate(session_factory, store, generator, episode_id)
    assert result.reused is False
    assert await _counts(session_factory) == before
    assert not [k for k in store.object_keys() if k.startswith(AGENT_RUNS_PREFIX)]


async def test_new_method_records_the_actual_prompt_and_links_the_script(session_factory) -> None:
    store = _Store()
    episode_id = await _episode(session_factory, store, InterventionMode.CO_PRODUCTION)
    generator = _LlmBackedStoryboard()

    result = await _generate(session_factory, store, generator, episode_id)

    async with session_factory() as session:
        script = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.SCRIPT
        )
        intervention = InterventionStore(session)
        (run,) = await intervention.list_agent_runs(episode_id=episode_id)
        (decision,) = await intervention.list_decisions(episode_id=episode_id)
        links = await intervention.links(episode_id=episode_id)
        (reservation,) = await ProviderReservationRepository(session).list_for_episode_provider(
            episode_id, ProviderCall.CODEX_STORYBOARD
        )
    assert script is not None
    assert run.role is AgentRole.WRITER_DIRECTOR and run.step == "plan_storyboard"
    assert run.output_revision_id == result.artifact_id
    assert run.model == "label-model"
    snapshot = json.loads(await store.get_bytes(run.snapshot_object_key))
    assert snapshot["prompt"] == generator.prompts[0]
    assert snapshot["prompt_source"] == "captured"
    assert snapshot["input_refs"]["source_script"]["artifact_id"] == script.id
    assert snapshot["input_refs"]["source_script"]["sha256"] == script.sha256
    assert [t["id"] for t in run.tool_invocations] == [reservation.id]
    assert decision.decision_type == "plan_storyboard"
    assert decision.result_revision_id == result.artifact_id
    assert decision.payload["target_revisions"] == [script.id]
    assert [(lk.upstream_revision_id, lk.downstream_revision_id, lk.kind) for lk in links] == [
        (script.id, result.artifact_id, RevisionLinkKind.DERIVED_FROM)
    ]


async def test_without_a_recorded_exchange_the_prompt_is_unavailable_not_invented(
    session_factory,
) -> None:
    store = _Store()
    episode_id = await _episode(session_factory, store, InterventionMode.REVIEW_POINTS)
    generator = FakeStoryboardGenerator(output=good_storyboard())  # LLM を tape に通さない

    await _generate(session_factory, store, generator, episode_id)

    async with session_factory() as session:
        (run,) = await InterventionStore(session).list_agent_runs(episode_id=episode_id)
    snapshot = json.loads(await store.get_bytes(run.snapshot_object_key))
    assert snapshot["prompt"] is None
    assert snapshot["prompt_source"] == "unavailable"
    assert snapshot["response"]["text"] == good_storyboard()


async def test_capture_failure_fails_the_storyboard_activity_without_an_artifact(
    session_factory,
) -> None:
    store = _Store()
    episode_id = await _episode(session_factory, store, InterventionMode.CO_PRODUCTION)
    store.fail_capture = True
    with pytest.raises(AgentRunCaptureError):
        await _generate(session_factory, store, _LlmBackedStoryboard(), episode_id)
    async with session_factory() as session:
        storyboards = await session.scalars(
            select(ArtifactMetadataRow).where(
                ArtifactMetadataRow.artifact_type == ArtifactType.STORYBOARD.value
            )
        )
        assert list(storyboards) == []
    assert (await _counts(session_factory))["agent_runs"] == 0


def test_the_storyboard_worker_routes_its_llm_through_the_exchange_recorder() -> None:
    """prompt を adapter の内側で組む工程は、配線で LLM を包まないと捕捉できない。"""
    source = inspect.getsource(run_worker)
    assert "llm=ExchangeRecordingGenerator(" in source
