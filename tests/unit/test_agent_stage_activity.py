"""``run_agent_stage``（ADR-0043 §6 Agent stage、INV-51/56/57）。SQLite。外部呼び出しなし。

各テストの理由: docs/testing/jpmas2-p1b-rationale.md
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from contracts.agentic_pipeline import AGENT_SPEC_DRIFT_ERROR, AgentStageOutcome
from contracts.agents import AgentHandlerRef
from contracts.intervention import (
    ActorType,
    AgentRole,
    AgentRunOutcome,
    AnnotationKind,
    ChangeSetStatus,
    ControlState,
    RevisionLinkKind,
)
from contracts.states import ArtifactType, ProviderCall, ReservationStatus
from infrastructure.db.models import (
    ArtifactMetadataRow,
    DecisionRecordRow,
    ProviderReservationRow,
)
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.intervention.store import ControlChange, InterventionStore
from tests.support.agent_stage import (
    CountingLlm,
    adopt_candidate,
    awaiting_handler,
    creative_handler,
    make_world,
    quality_handler,
    rejecting_handler,
    stage_request,
    strategy_handler,
    wrong_artifact_handler,
)
from workers.agents.activities import AGENT_SCOPES

STRATEGY = AgentHandlerRef.STRATEGY_AGENT_V1
CREATIVE = AgentHandlerRef.CREATIVE_DIRECTOR_V1
QUALITY = AgentHandlerRef.QUALITY_REVIEWER_V1
LEARNING = AgentHandlerRef.LEARNING_AGENT_V1


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _snapshot(session_factory) -> dict[str, int]:
    """書き込みの有無を比べるための件数。"""
    from infrastructure.db.models import (
        AgentRunRow,
        AnnotationRow,
        EpisodeEventRow,
        RevisionLinkRow,
    )

    return {
        m.__tablename__: await _count(session_factory, m)
        for m in (
            ArtifactMetadataRow,
            AgentRunRow,
            DecisionRecordRow,
            RevisionLinkRow,
            AnnotationRow,
            EpisodeEventRow,
            ProviderReservationRow,
        )
    }


async def _current(world, artifact_type: ArtifactType):
    async with world.session_factory() as session:
        return await ArtifactMetadataRepository(session).find_current_by_type(
            world.episode_id, artifact_type
        )


# --------------------------------------------------------------------------- happy path


async def test_strategy_stage_writes_exactly_one_artifact_run_decision_and_a_link(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, spec = stage_request(AgentRole.STRATEGY, world.episode_id)
    brief_before = await _current(world, ArtifactType.USER_BRIEF)
    before = await _snapshot(session_factory)

    result = await world.activities({STRATEGY: strategy_handler()}).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.PRODUCED.value
    assert result.replayed is False
    after = await _snapshot(session_factory)
    assert after["artifact_metadata"] == before["artifact_metadata"] + 1
    assert after["agent_runs"] == before["agent_runs"] + 1
    assert after["decision_records"] == before["decision_records"] + 1
    assert after["revision_links"] == before["revision_links"] + 1
    assert after["provider_reservations"] == 1
    produced = await _current(world, ArtifactType.STRATEGY_BRIEF)
    assert produced is not None and produced.id == result.artifact_revision_id
    assert brief_before is not None
    async with session_factory() as session:
        istore = InterventionStore(session)
        run = await istore.get_agent_run(result.agent_run_id or "")
        assert run is not None
        assert run.role is AgentRole.STRATEGY and run.step == "strategy"
        assert run.outcome is AgentRunOutcome.SUCCEEDED
        assert run.output_revision_id == produced.id
        assert run.prompt_version == spec.prompt_version
        # codex の policy には単価が無い: 費用は測れず NULL（0 にしない）。token は gateway の実測
        assert run.estimated_cost_usd is None
        assert run.token_usage == {"input_tokens": 100, "output_tokens": 50, "llm_calls": 1}
        assert run.model == "fake-model"
        snapshot = await artifact_store.get_json(run.snapshot_object_key)
        assert snapshot["agent_spec"] == {
            "role": "strategy",
            "spec_version": "1.0",
            "manifest_hash": spec.manifest_hash,
            "model_policy_ref": spec.model_policy_ref,
        }
        (decision,) = await istore.list_decisions(episode_id=world.episode_id)
        assert decision.decision_id == result.decision_id
        assert decision.actor.type is ActorType.AGENT and decision.actor.id == "strategy"
        assert decision.agent_run_id == run.agent_run_id
        assert decision.result_revision_id == produced.id
        (link,) = await istore.links(episode_id=world.episode_id)
        assert link.kind is RevisionLinkKind.DERIVED_FROM
        assert link.upstream_revision_id == brief_before.id
        assert link.downstream_revision_id == produced.id


async def test_creative_stage_reads_the_adopted_decision_and_links_both_inputs(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store, strategy=True)
    await adopt_candidate(session_factory, world.episode_id)
    request, _ = stage_request(AgentRole.CREATIVE_DIRECTOR, world.episode_id, stage_id="creative")

    result = await world.activities({CREATIVE: creative_handler()}).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.PRODUCED.value
    produced = await _current(world, ArtifactType.CREATIVE_BRIEF)
    assert produced is not None
    async with session_factory() as session:
        links = await InterventionStore(session).links(downstream_revision_id=produced.id)
    assert len(links) == 2  # user_brief と strategy_brief（decision は revision ではない）


# --------------------------------------------------------------------------- replay


async def test_a_replay_writes_nothing_new_and_returns_the_stored_result(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    activities = world.activities({STRATEGY: strategy_handler()})
    first = await activities.run_agent_stage(request)
    after_first = await _snapshot(session_factory)

    again = await activities.run_agent_stage(request)

    assert again.replayed is True
    assert (again.outcome, again.artifact_revision_id, again.agent_run_id, again.decision_id) == (
        first.outcome,
        first.artifact_revision_id,
        first.agent_run_id,
        first.decision_id,
    )
    assert await _snapshot(session_factory) == after_first
    assert world.llm.calls == 1


async def test_after_a_crash_between_the_call_and_the_save_the_llm_is_not_called_again(
    session_factory, artifact_store, monkeypatch
) -> None:
    """予約が spent（evidence あり）なら、再試行は保存済みの応答を再生する（二重課金しない）。"""
    import workers.agents.activities as module

    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    activities = world.activities({STRATEGY: strategy_handler()})
    real = module.persist_stage
    calls = {"n": 0}

    async def crash_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker died before the save")
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, "persist_stage", crash_once)
    with pytest.raises(RuntimeError):
        await activities.run_agent_stage(request)
    assert world.llm.calls == 1
    async with session_factory() as session:
        row = (await session.scalars(select(ProviderReservationRow))).one()
        assert row.status == ReservationStatus.SPENT.value and row.raw_output_key

    result = await activities.run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.PRODUCED.value
    assert world.llm.calls == 1  # 再生: LLM は呼び直さない
    assert await _count(session_factory, ProviderReservationRow) == 1


# --------------------------------------------------------------------------- spec drift / flags


async def test_a_changed_spec_hash_is_a_non_retryable_drift(
    session_factory, artifact_store
) -> None:
    """INV-51: pin された hash と Registry の hash が違えば、その定義を走らせない。"""
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    request.stage["agent_spec_hash"] = "0" * 64
    before = await _snapshot(session_factory)

    with pytest.raises(ApplicationError) as exc:
        await world.activities({STRATEGY: strategy_handler()}).run_agent_stage(request)

    assert exc.value.type == AGENT_SPEC_DRIFT_ERROR
    assert exc.value.non_retryable is True
    assert await _snapshot(session_factory) == before
    assert world.llm.calls == 0


async def test_an_unknown_pinned_spec_version_is_also_drift(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    request.stage["agent_spec_version"] = "9.9"

    with pytest.raises(ApplicationError) as exc:
        await world.activities({STRATEGY: strategy_handler()}).run_agent_stage(request)

    assert exc.value.type == AGENT_SPEC_DRIFT_ERROR and exc.value.non_retryable


@pytest.mark.parametrize("variant", ["flag_off", "role_not_enabled"])
async def test_a_disabled_agent_is_blocked_not_silently_skipped(
    session_factory, artifact_store, variant
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    activities = (
        world.activities({STRATEGY: strategy_handler()}, agentic_enabled=False)
        if variant == "flag_off"
        else world.activities(
            {STRATEGY: strategy_handler()}, enabled=frozenset({AgentRole.QUALITY_REVIEWER})
        )
    )
    before = await _snapshot(session_factory)

    result = await activities.run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.BLOCKED.value
    assert result.reason == "agent_disabled"
    assert await _snapshot(session_factory) == before
    assert world.llm.calls == 0


# --------------------------------------------------------------------------- budget / hold


async def test_a_budget_refusal_leaves_no_reservation_and_no_artifact(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    before = await _snapshot(session_factory)

    result = await world.activities({STRATEGY: strategy_handler()}, rate=None).run_agent_stage(
        request
    )

    assert result.outcome == AgentStageOutcome.BLOCKED.value
    assert result.reason == "budget_unverifiable"
    assert await _snapshot(session_factory) == before
    assert world.llm.calls == 0


async def test_an_exceeded_budget_blocks_with_budget_exceeded(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)

    result = await world.activities(
        {STRATEGY: strategy_handler()}, episode_budget_jpy=1
    ).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.BLOCKED.value and result.reason == "budget_exceeded"
    assert await _count(session_factory, ProviderReservationRow) == 0
    assert await _current(world, ArtifactType.STRATEGY_BRIEF) is None


async def test_a_held_episode_is_blocked_with_an_undispatched_reservation(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    async with session_factory() as session:
        istore = InterventionStore(session)
        await istore.lock_control(world.episode_id)
        await istore.update_control(
            world.episode_id, ControlChange(control_state=ControlState.HELD)
        )
        await session.commit()
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)

    result = await world.activities({STRATEGY: strategy_handler()}).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.BLOCKED.value and result.reason == "held"
    async with session_factory() as session:
        row = (await session.scalars(select(ProviderReservationRow))).one()
    assert row.provider == ProviderCall.AGENT_LLM.value
    assert row.status == ReservationStatus.RESERVED.value and row.dispatched_at is None
    assert world.llm.calls == 0
    assert await _current(world, ArtifactType.STRATEGY_BRIEF) is None


async def test_a_missing_required_input_is_blocked_before_any_reservation(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store, brief=False)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)

    result = await world.activities({STRATEGY: strategy_handler()}).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.BLOCKED.value
    assert result.reason == "input_missing:user_brief"
    assert await _count(session_factory, ProviderReservationRow) == 0


# --------------------------------------------------------------------------- rejected output


async def test_a_rejected_output_is_recorded_without_an_artifact_and_is_not_repeated(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    activities = world.activities({STRATEGY: rejecting_handler()})

    result = await activities.run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.REJECTED_OUTPUT.value
    assert result.artifact_revision_id is None and result.decision_id is None
    assert await _current(world, ArtifactType.STRATEGY_BRIEF) is None
    async with session_factory() as session:
        run = await InterventionStore(session).get_agent_run(result.agent_run_id or "")
        assert run is not None and run.outcome is AgentRunOutcome.REJECTED_OUTPUT
        assert run.error_summary and "StrategyResponse" in run.error_summary
        row = (await session.scalars(select(ProviderReservationRow))).one()
        assert row.status == ReservationStatus.SPENT.value  # 呼んだ事実は残る
    assert await _count(session_factory, DecisionRecordRow) == 0

    again = await activities.run_agent_stage(request)
    assert again.outcome == AgentStageOutcome.REJECTED_OUTPUT.value and again.replayed
    assert world.llm.calls == 1


async def test_an_artifact_of_the_wrong_type_is_rejected_not_saved(
    session_factory, artifact_store
) -> None:
    """spec の ``output_schema_ref`` と違う型の成果物は保存しない。"""
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)

    result = await world.activities({STRATEGY: wrong_artifact_handler()}).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.REJECTED_OUTPUT.value
    assert await _current(world, ArtifactType.CREATIVE_BRIEF) is None


# --------------------------------------------------------------------------- proposals (INV-57)


async def test_quality_proposals_arrive_as_proposed_agent_change_sets_and_are_not_applied(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store, storyboard=True)
    storyboard_before = await _current(world, ArtifactType.STORYBOARD)
    request, _ = stage_request(AgentRole.QUALITY_REVIEWER, world.episode_id, stage_id="quality")
    handler = quality_handler(
        episode_id=world.episode_id, storyboard_revision_id=world.storyboard_revision_id or ""
    )

    result = await world.activities({QUALITY: handler}).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.PRODUCED.value
    assert len(result.proposal_command_ids) == 1
    async with session_factory() as session:
        istore = InterventionStore(session)
        sets = await istore.list_change_sets(world.episode_id)
        commands = await istore.list_commands(world.episode_id)
        findings = await istore.list_annotations(
            world.episode_id, kind=AnnotationKind.REVIEW_FINDING
        )
    # 受け付けた command は agent 主体の提案で、まだ適用されていない
    (command,) = [c for c in commands if c.command_id in result.proposal_command_ids]
    assert command.actor.type is ActorType.AGENT and command.actor.id == "quality_reviewer"
    assert all(s.status is ChangeSetStatus.PROPOSED for s in sets)
    assert not any(s.status is ChangeSetStatus.APPLIED for s in sets)
    assert world.starter.started == result.proposal_command_ids
    # 現行の storyboard は変わらない（提案だけ）
    storyboard_after = await _current(world, ArtifactType.STORYBOARD)
    assert storyboard_before is not None and storyboard_after is not None
    assert storyboard_after.id == storyboard_before.id
    # 所見は scene / time_range に anchor した review_finding 注記
    agent_findings = [a for a in findings if a.agent_run_id == result.agent_run_id]
    (annotation,) = agent_findings
    assert annotation.anchor["scene_id"] == "sb1"
    assert annotation.anchor["time_range"] == {"start_ms": 0, "end_ms": 1500}
    assert annotation.actor.type is ActorType.AGENT


async def test_the_agent_caller_never_holds_the_apply_scope() -> None:
    from contracts.intervention import Scope

    assert Scope.APPLY not in AGENT_SCOPES
    assert {Scope.READ, Scope.COMMENT, Scope.PROPOSE} == AGENT_SCOPES


async def test_a_replay_resubmits_proposals_idempotently(session_factory, artifact_store) -> None:
    world = await make_world(session_factory, artifact_store, storyboard=True)
    request, _ = stage_request(AgentRole.QUALITY_REVIEWER, world.episode_id, stage_id="quality")
    handler = quality_handler(
        episode_id=world.episode_id, storyboard_revision_id=world.storyboard_revision_id or ""
    )
    activities = world.activities({QUALITY: handler})
    first = await activities.run_agent_stage(request)
    commands_after_first = len(
        await InterventionStore(await _open(session_factory)).list_commands(world.episode_id)
    )

    again = await activities.run_agent_stage(request)

    assert again.proposal_command_ids == first.proposal_command_ids
    async with session_factory() as session:
        commands = await InterventionStore(session).list_commands(world.episode_id)
    assert len(commands) == commands_after_first


async def _open(session_factory):
    return session_factory()


# --------------------------------------------------------------------------- learning


async def test_an_awaiting_learning_stage_writes_no_lesson(session_factory, artifact_store) -> None:
    """INV-57: private・測定欠落のときは Lesson を作らない（欠測を低評価にしない）。"""
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.LEARNING, world.episode_id, stage_id="lesson")
    activities = world.activities({LEARNING: awaiting_handler()})

    result = await activities.run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.AWAITING_PUBLIC_METRICS.value
    assert result.artifact_revision_id is None and result.decision_id is None
    assert "private" in (result.reason or "")
    assert await _current(world, ArtifactType.LESSON) is None
    async with session_factory() as session:
        run = await InterventionStore(session).get_agent_run(result.agent_run_id or "")
    assert run is not None and run.output_revision_id is None
    # 入力の読み出しは LLM を呼ばなかった: token は既知の 0 回
    assert run.token_usage == {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}

    again = await activities.run_agent_stage(request)
    assert again.outcome == AgentStageOutcome.AWAITING_PUBLIC_METRICS.value and again.replayed


async def test_unknown_token_usage_stays_null(session_factory, artifact_store) -> None:
    """usage が取れない LLM: ``token_usage`` / 費用は NULL（0 で埋めない / INV-56）。"""
    world = await make_world(session_factory, artifact_store, llm=CountingLlm(tokens=(None, None)))
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)

    result = await world.activities({STRATEGY: strategy_handler()}).run_agent_stage(request)

    async with session_factory() as session:
        run = await InterventionStore(session).get_agent_run(result.agent_run_id or "")
    assert run is not None and run.token_usage is None and run.estimated_cost_usd is None


# --------------------------------------------------------------------------- tool wiring


async def test_artifact_and_analytics_tools_are_wired_and_read_only(
    session_factory, artifact_store
) -> None:
    from contracts.agents import AgentTool

    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    seen: dict[str, object] = {}
    inner = strategy_handler()

    async def handler(agent_input, gateway):
        seen["brief"] = await gateway.call(AgentTool.ARTIFACT_READ, artifact_type="user_brief")
        seen["gone"] = await gateway.call(AgentTool.ARTIFACT_READ, artifact_type="lesson")
        seen["analytics"] = await gateway.call(AgentTool.ANALYTICS_READ)
        return await inner(agent_input, gateway)

    before = await _snapshot(session_factory)
    result = await world.activities({STRATEGY: handler}).run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.PRODUCED.value
    assert seen["brief"]["theme"] == "Edo power"  # type: ignore[index]
    assert seen["gone"] == {}
    assert seen["analytics"]["measured"] is False  # type: ignore[index]
    after = await _snapshot(session_factory)
    assert after["artifact_metadata"] == before["artifact_metadata"] + 1  # tool は何も書かない


async def test_a_handler_cannot_use_a_tool_outside_its_spec(
    session_factory, artifact_store
) -> None:
    """INV-56: strategy の spec に ``intervention:propose`` は無い。拒否され何も保存しない。"""
    from contracts.agents import AgentTool

    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)

    async def handler(agent_input, gateway):
        await gateway.call(AgentTool.INTERVENTION_PROPOSE, proposal={})
        raise AssertionError("unreachable")

    with pytest.raises(ApplicationError) as exc:
        await world.activities({STRATEGY: handler}).run_agent_stage(request)

    assert exc.value.type == "ToolNotAllowedError" and exc.value.non_retryable
    assert await _current(world, ArtifactType.STRATEGY_BRIEF) is None
    async with session_factory() as session:
        row = (await session.scalars(select(ProviderReservationRow))).one()
    # LLM を試みていない: 台帳は確定済み（未照合のまま残して次の呼び出しを塞がない）
    assert row.status == ReservationStatus.SPENT.value
    assert world.llm.calls == 0


async def test_a_tool_queued_proposal_is_submitted_after_the_save_and_not_applied(
    session_factory, artifact_store
) -> None:
    from contracts.agents import AgentTool

    world = await make_world(session_factory, artifact_store, storyboard=True)
    request, _ = stage_request(AgentRole.QUALITY_REVIEWER, world.episode_id, stage_id="quality")
    inner = quality_handler(
        episode_id=world.episode_id,
        storyboard_revision_id=world.storyboard_revision_id or "",
        with_proposal=False,
    )

    async def handler(agent_input, gateway):
        await gateway.call(
            AgentTool.INTERVENTION_PROPOSE,
            proposal={
                "episode_id": world.episode_id,
                "base_storyboard_revision_id": world.storyboard_revision_id,
                "edits": [{"scene_id": "sb2", "visual_description": "A quiet harbour at dawn"}],
            },
        )
        return await inner(agent_input, gateway)

    result = await world.activities({QUALITY: handler}).run_agent_stage(request)

    assert len(result.proposal_command_ids) == 1
    async with session_factory() as session:
        sets = await InterventionStore(session).list_change_sets(world.episode_id)
    assert all(s.status is ChangeSetStatus.PROPOSED for s in sets)


# --------------------------------------------------------------------------- real handlers


def _canned(prompt: str, schema: dict) -> dict:
    title = schema.get("title")
    if title == "StrategyResponse":
        return {
            "audience_hypothesis": "US 20s like power-structure stories.",
            "candidates": [
                {
                    "id": "c1",
                    "title": "Why the Shogun hid in plain sight",
                    "subject": "tokugawa_ieyasu",
                    "angle": "reason",
                    "era": "edo",
                    "hook": "He ruled Japan without ever being king.",
                    "rationale": "Strong curiosity gap.",
                }
            ],
            "recommended_candidate_id": "c1",
        }
    if title == "CreativeResponse":
        return {
            "hook_options": ["He ruled Japan without ever being king."],
            "tone": "dry wit",
        }
    raise LookupError(title)


async def test_real_handlers_run_against_assembled_inputs_for_every_role(
    session_factory, artifact_store
) -> None:
    """P1-A の本物の ``HANDLERS`` + FakeAgentLlm が、組み立てた入力で通る（形の食い違いを検出）。"""
    from domain.agents.handlers import HANDLERS
    from infrastructure.agents.fake_llm import FakeAgentLlm

    world = await make_world(session_factory, artifact_store, storyboard=True)
    world.llm = FakeAgentLlm(_canned)  # type: ignore[assignment]
    activities = world.activities(HANDLERS)

    strategy, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    result = await activities.run_agent_stage(strategy)
    assert result.outcome == AgentStageOutcome.PRODUCED.value, result

    await adopt_candidate(session_factory, world.episode_id, "c1")
    async with session_factory() as session:
        brief = await ArtifactMetadataRepository(session).find_current_by_type(
            world.episode_id, ArtifactType.STRATEGY_BRIEF
        )
    assert brief is not None
    creative, _ = stage_request(AgentRole.CREATIVE_DIRECTOR, world.episode_id, stage_id="creative")
    result = await activities.run_agent_stage(creative)
    assert result.outcome == AgentStageOutcome.PRODUCED.value, result

    # 完成動画が無い: 本物の quality handler は LLM を呼ばず blocked_missing_input の所見を残す
    quality, _ = stage_request(AgentRole.QUALITY_REVIEWER, world.episode_id, stage_id="quality")
    result = await activities.run_agent_stage(quality)
    assert result.outcome == AgentStageOutcome.PRODUCED.value, result
    assert not result.proposal_command_ids

    # 投稿受領・公開測定が無い: Lesson を作らない
    learning, _ = stage_request(AgentRole.LEARNING, world.episode_id, stage_id="lesson")
    result = await activities.run_agent_stage(learning)
    assert result.outcome == AgentStageOutcome.AWAITING_PUBLIC_METRICS.value, result
    assert await _current(world, ArtifactType.LESSON) is None


# --- LLM を呼ばない経路は予約しない


async def test_a_learning_stage_that_awaits_metrics_reserves_no_budget(
    session_factory, artifact_store
) -> None:
    """LLM を呼ばない経路（awaiting）は予算枠を取らない。replay でも台帳は増えない。"""
    from domain.agents.handlers import HANDLERS

    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.LEARNING, world.episode_id, stage_id="lesson")
    activities = world.activities(HANDLERS)

    result = await activities.run_agent_stage(request)
    again = await activities.run_agent_stage(request)

    assert result.outcome == AgentStageOutcome.AWAITING_PUBLIC_METRICS.value
    assert again.replayed
    assert await _count(session_factory, ProviderReservationRow) == 0
    assert world.llm.calls == 0


async def test_an_llm_path_reserves_exactly_once_before_the_call(
    session_factory, artifact_store, monkeypatch
) -> None:
    """LLM を呼ぶ経路は従来どおり呼ぶ前に1回だけ予約する（事前判定が True のとき）。"""
    import workers.agents.activities as module

    monkeypatch.setattr(module, "needs_llm", lambda _ref, _docs: True)
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.LEARNING, world.episode_id, stage_id="lesson")
    activities = world.activities({LEARNING: awaiting_handler()})

    await activities.run_agent_stage(request)
    await activities.run_agent_stage(request)

    assert await _count(session_factory, ProviderReservationRow) == 1
