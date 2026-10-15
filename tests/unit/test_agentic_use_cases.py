"""CreateEpisodeDraft / StartAgenticProduction とその DB・Temporal adapter（ADR-0043 §6）。

各テストの理由: docs/testing/jpmas2-p1c-rationale.md
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from temporalio.testing import WorkflowEnvironment

from application.errors import OperationError, OperationErrorCode
from application.use_cases.agentic_production import CreateEpisodeDraft, StartAgenticProduction
from contracts.agentic import UserBrief
from contracts.agentic_pipeline import agentic_episode_workflow_id
from contracts.application import (
    CreateEpisodeDraftRequest,
    StartAgenticProductionRequest,
    StartedWorkflow,
    episode_draft_id,
)
from contracts.intervention import InterventionMode
from contracts.states import ArtifactType, EpisodeStatus
from infrastructure.agents.draft_port import DbEpisodeDraftPort
from infrastructure.db.models import DailyEpisodeSlotRow, EpisodeRow, TopicPlanRow
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.intervention.store import InterventionStore
from infrastructure.temporal.agentic_starter import TemporalAgenticStarter

BUCKET = "artifacts"
COMMAND_ID = "draft-command-0001"


def _brief(**over: Any) -> UserBrief:
    base: dict[str, Any] = {
        "theme": "How Edo firefighters fought fires",
        "template_id": "shorts_agentic",
        "template_version": "1",
    }
    return UserBrief.model_validate({**base, **over})


def _port(session_factory: Any, artifact_store: Any) -> DbEpisodeDraftPort:
    return DbEpisodeDraftPort(session_factory=session_factory, store=artifact_store, bucket=BUCKET)


def _request(brief: UserBrief | None = None, command_id: str = COMMAND_ID):
    return CreateEpisodeDraftRequest(command_id=command_id, brief=brief or _brief())


class RecordingStarter:
    def __init__(self, *, started: bool = True) -> None:
        self.calls: list[tuple[uuid.UUID, str, str]] = []
        self._started = started

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow:
        self.calls.append((episode_id, template_id, template_version))
        return StartedWorkflow(
            workflow_id=agentic_episode_workflow_id(str(episode_id)), started=self._started
        )


# ------------------------------------------------------------------------------ draft


async def test_a_draft_is_an_idle_planned_episode_with_a_brief_and_an_intervenable_control(
    session_factory, artifact_store
) -> None:
    result = await CreateEpisodeDraft(_port(session_factory, artifact_store)).execute(_request())
    assert result.created is True and result.episode_id == episode_draft_id(COMMAND_ID)
    async with session_factory() as s:
        episode = await EpisodeRepository(s).get(result.episode_id)
        control = await InterventionStore(s).get_control(result.episode_id)
        brief_meta = await ArtifactMetadataRepository(s).find_current_by_type(
            result.episode_id, ArtifactType.USER_BRIEF
        )
        workflow_id = await EpisodeRepository(s).get_workflow_id(result.episode_id)
    assert episode is not None
    assert episode.status is EpisodeStatus.PLANNED
    assert episode.topic == "How Edo firefighters fought fires" and episode.topic_plan_id is None
    assert control.persisted and control.mode is InterventionMode.AUTO_INTERVENABLE
    assert brief_meta is not None
    assert workflow_id is None  # 生成を始めない


async def test_a_draft_uses_no_daily_slot_and_no_topic_plan(
    session_factory, artifact_store
) -> None:
    await CreateEpisodeDraft(_port(session_factory, artifact_store)).execute(_request())
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(DailyEpisodeSlotRow)) == 0
        assert await s.scalar(select(func.count()).select_from(TopicPlanRow)) == 0
        assert await s.scalar(select(func.count()).select_from(EpisodeRow)) == 1


async def test_resending_the_same_command_returns_the_same_episode(
    session_factory, artifact_store
) -> None:
    use_case = CreateEpisodeDraft(_port(session_factory, artifact_store))
    first = await use_case.execute(_request())
    again = await use_case.execute(_request())
    assert again.episode_id == first.episode_id and again.created is False
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(EpisodeRow)) == 1
        artifacts = await ArtifactMetadataRepository(s).list_for_episode(first.episode_id)
    assert len(artifacts) == 1


async def test_the_same_command_id_with_another_brief_is_command_id_reused(
    session_factory, artifact_store
) -> None:
    use_case = CreateEpisodeDraft(_port(session_factory, artifact_store))
    await use_case.execute(_request())
    with pytest.raises(OperationError) as err:
        await use_case.execute(_request(_brief(theme="Something else entirely")))
    assert err.value.code is OperationErrorCode.COMMAND_ID_REUSED


async def test_different_commands_make_different_episodes(session_factory, artifact_store) -> None:
    use_case = CreateEpisodeDraft(_port(session_factory, artifact_store))
    a = await use_case.execute(_request(command_id="draft-command-000a"))
    b = await use_case.execute(_request(command_id="draft-command-000b"))
    assert a.episode_id != b.episode_id


@pytest.mark.parametrize(
    ("template", "version", "code"),
    [
        ("daily_legacy", "1", OperationErrorCode.TEMPLATE_NOT_RUNNABLE),
        ("shorts_reviewed", "1", OperationErrorCode.TEMPLATE_NOT_RUNNABLE),
        ("shorts_agentic", "9", OperationErrorCode.UNKNOWN_TEMPLATE),
        ("nope", "1", OperationErrorCode.UNKNOWN_TEMPLATE),
    ],
)
async def test_a_draft_for_an_unknown_unrunnable_or_legacy_template_creates_nothing(
    session_factory, artifact_store, template: str, version: str, code: OperationErrorCode
) -> None:
    use_case = CreateEpisodeDraft(_port(session_factory, artifact_store))
    with pytest.raises(OperationError) as err:
        await use_case.execute(_request(_brief(template_id=template, template_version=version)))
    assert err.value.code is code
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(EpisodeRow)) == 0


async def test_a_format_profile_other_than_the_templates_is_refused(
    session_factory, artifact_store
) -> None:
    with pytest.raises(OperationError) as err:
        await CreateEpisodeDraft(_port(session_factory, artifact_store)).execute(
            _request(_brief(format_profile="long_form"))
        )
    assert err.value.code is OperationErrorCode.INVALID_PAYLOAD


# ------------------------------------------------------------------------------ start


async def _draft(session_factory, artifact_store, **over: Any) -> uuid.UUID:
    result = await CreateEpisodeDraft(_port(session_factory, artifact_store)).execute(
        _request(_brief(**over))
    )
    return result.episode_id


async def test_start_is_refused_when_the_flag_is_off(session_factory, artifact_store) -> None:
    episode_id = await _draft(session_factory, artifact_store)
    starter = RecordingStarter()
    use_case = StartAgenticProduction(
        _port(session_factory, artifact_store), starter, enabled=False
    )
    with pytest.raises(OperationError) as err:
        await use_case.execute(StartAgenticProductionRequest(episode_id=episode_id))
    assert err.value.code is OperationErrorCode.FEATURE_DISABLED
    assert starter.calls == []


async def test_start_runs_the_agentic_workflow_for_the_drafts_template(
    session_factory, artifact_store
) -> None:
    episode_id = await _draft(session_factory, artifact_store)
    starter = RecordingStarter()
    use_case = StartAgenticProduction(_port(session_factory, artifact_store), starter, enabled=True)
    result = await use_case.execute(StartAgenticProductionRequest(episode_id=episode_id))
    assert starter.calls == [(episode_id, "shorts_agentic", "1")]
    assert result.started is True
    assert result.workflow_id == agentic_episode_workflow_id(str(episode_id))


async def test_start_reports_an_already_started_workflow_without_starting_another(
    session_factory, artifact_store
) -> None:
    episode_id = await _draft(session_factory, artifact_store)
    use_case = StartAgenticProduction(
        _port(session_factory, artifact_store), RecordingStarter(started=False), enabled=True
    )
    result = await use_case.execute(StartAgenticProductionRequest(episode_id=episode_id))
    assert result.started is False


async def test_start_for_an_episode_without_a_draft_is_episode_not_found(
    session_factory, artifact_store
) -> None:
    starter = RecordingStarter()
    use_case = StartAgenticProduction(_port(session_factory, artifact_store), starter, enabled=True)
    with pytest.raises(OperationError) as err:
        await use_case.execute(StartAgenticProductionRequest(episode_id=uuid.uuid4()))
    assert err.value.code is OperationErrorCode.EPISODE_NOT_FOUND
    assert starter.calls == []


class _FixedDrafts:
    """UserBrief の template が後から legacy / 未実装になった場合（draft の後の登録変更）。"""

    def __init__(self, brief: UserBrief) -> None:
        self._brief = brief

    async def create_draft(self, **_: Any) -> Any:  # pragma: no cover - 使わない
        raise AssertionError

    async def get_brief(self, episode_id: uuid.UUID) -> UserBrief | None:
        return self._brief


@pytest.mark.parametrize("template", ["daily_legacy", "shorts_reviewed"])
async def test_start_rejects_a_legacy_or_unrunnable_template(template: str) -> None:
    starter = RecordingStarter()
    use_case = StartAgenticProduction(
        _FixedDrafts(_brief(template_id=template)), starter, enabled=True
    )
    with pytest.raises(OperationError) as err:
        await use_case.execute(StartAgenticProductionRequest(episode_id=uuid.uuid4()))
    assert err.value.code is OperationErrorCode.TEMPLATE_NOT_RUNNABLE
    assert starter.calls == []


# ------------------------------------------------------------------------------ Temporal


async def test_the_temporal_starter_uses_one_workflow_id_per_episode_and_rejects_duplicates() -> (
    None
):
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        starter = TemporalAgenticStarter(wf_env.client)
        episode_id = uuid.uuid4()
        first = await starter.start(
            episode_id=episode_id, template_id="shorts_agentic", template_version="1"
        )
        second = await starter.start(
            episode_id=episode_id, template_id="shorts_agentic", template_version="1"
        )
        assert first.started is True and second.started is False
        assert first.workflow_id == second.workflow_id == f"agentic-episode-{episode_id}"
        desc = await wf_env.client.get_workflow_handle(first.workflow_id).describe()
        assert desc.task_queue == "pipeline"
        assert desc.raw_description.workflow_execution_info.type.name == "AgenticEpisodeWorkflow"
