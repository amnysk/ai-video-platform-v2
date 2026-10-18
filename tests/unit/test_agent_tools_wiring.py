"""Agent Tool の配線と flag（ADR-0047 §3 / §8 / §9、INV-55 の延長）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from contracts.agent_tools import ToolId
from contracts.agents import AgentHandlerRef
from contracts.intervention import AgentRole
from contracts.log_contract import RECORD_EXTRA_KEY, EventName
from domain.agents.runtime import AgentInput, AgentOutput
from domain.agents.tool_gateway import ToolGateway, ToolSupport
from domain.agents.tools.observer import NoopToolObserver, ToolEvent
from infrastructure.agents import tools as tools_pkg
from infrastructure.agents.tools import build_tool_executors, build_tool_support
from infrastructure.agents.tools.observer import LoggingToolObserver
from infrastructure.config import Settings, enabled_tool_ids
from infrastructure.db.agent_tool_call_repository import AgentToolCallRepository
from infrastructure.db.models import AgentToolCallRow
from tests.support.agent_stage import make_world, stage_request
from tests.support.agent_tools import SECRET_QUERY, SECRET_RESULT, FakeExecutor, tool_def

_STRATEGY = AgentHandlerRef.STRATEGY_AGENT_V1
ENV_KEYS = ("JPMAS2_AGENT_TOOLS", "JPMAS2_ENABLED_TOOLS")


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# --- flag ------------------------------------------------------------------


def test_tool_flags_default_to_off(monkeypatch) -> None:
    s = _settings(monkeypatch)
    assert s.jpmas2_agent_tools is False
    assert s.jpmas2_enabled_tools == ""
    assert enabled_tool_ids(s) == frozenset()


def test_enabled_tools_are_parsed_from_a_comma_separated_list(monkeypatch) -> None:
    s = _settings(monkeypatch, JPMAS2_ENABLED_TOOLS=" search_trends, fetch_sources ,,")
    assert enabled_tool_ids(s) == {ToolId.SEARCH_TRENDS, ToolId.FETCH_SOURCES}


def test_an_unknown_tool_id_fails_at_load_with_the_known_ids(monkeypatch) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(monkeypatch, JPMAS2_ENABLED_TOOLS="search_trends,wizard")
    assert "wizard" in str(exc.value) and "get_video_analytics" in str(exc.value)


def test_no_tool_support_is_built_when_the_flag_is_off(
    monkeypatch, session_factory, artifact_store
):
    s = _settings(monkeypatch, JPMAS2_ENABLED_TOOLS="search_trends")  # 有効な tool があっても
    assert (
        build_tool_support(s, session_factory=session_factory, store=artifact_store, bucket="b")
        is None
    )


def test_with_the_flag_on_support_is_built_with_the_db_store_and_the_logging_observer(
    monkeypatch, session_factory, artifact_store
) -> None:
    s = _settings(monkeypatch, JPMAS2_AGENT_TOOLS="true")
    support = build_tool_support(
        s, session_factory=session_factory, store=artifact_store, bucket="b"
    )
    assert support is not None and support.executors == {}  # 実 tool は別の変更で足す
    assert isinstance(support.store, AgentToolCallRepository)
    assert isinstance(support.observer, LoggingToolObserver)


def test_only_enabled_tools_get_an_executor(monkeypatch, session_factory, artifact_store) -> None:
    fake = FakeExecutor()
    monkeypatch.setattr(
        tools_pkg,
        "_research_executors",
        lambda ctx: {ToolId.SEARCH_TRENDS: fake, ToolId.FETCH_SOURCES: fake},
    )
    s = _settings(monkeypatch, JPMAS2_AGENT_TOOLS="true", JPMAS2_ENABLED_TOOLS="search_trends")
    built = build_tool_executors(
        s, session_factory=session_factory, store=artifact_store, bucket="b"
    )
    assert set(built) == {ToolId.SEARCH_TRENDS}


def test_the_same_tool_from_two_groups_is_a_startup_error(
    monkeypatch, session_factory, artifact_store
) -> None:
    fake = FakeExecutor()
    monkeypatch.setattr(tools_pkg, "_research_executors", lambda ctx: {ToolId.SEARCH_TRENDS: fake})
    monkeypatch.setattr(tools_pkg, "_analytics_executors", lambda ctx: {ToolId.SEARCH_TRENDS: fake})
    s = _settings(monkeypatch, JPMAS2_AGENT_TOOLS="true", JPMAS2_ENABLED_TOOLS="search_trends")
    with pytest.raises(ValueError, match="duplicate"):
        build_tool_executors(s, session_factory=session_factory, store=artifact_store, bucket="b")


# --- Activity の配線 ---------------------------------------------------------


def _strategy_handler(seen: list[object]):
    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        env = await gateway.call_tool("search_trends@1", {"q": SECRET_QUERY})
        seen.append(env)
        return AgentOutput(artifact=None, decision=None, outcome="awaiting_public_metrics")

    return handler


def _support(session_factory, artifact_store, executor: FakeExecutor) -> ToolSupport:
    trends = tool_def(ToolId.SEARCH_TRENDS)
    return ToolSupport(
        defs={trends.ref: trends},
        executors={ToolId.SEARCH_TRENDS: executor},
        store=AgentToolCallRepository(session_factory, artifact_store),
    )


async def test_a_strategy_1_1_stage_calls_a_tool_once_and_a_replay_does_not(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    executor = FakeExecutor()
    seen: list[object] = []
    activities = world.activities(
        {_STRATEGY: _strategy_handler(seen)},
        tool_support=_support(session_factory, artifact_store, executor),
    )
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id, version="1.1")
    first = await activities.run_agent_stage(request)
    assert first.outcome == "awaiting_public_metrics"
    assert len(executor.calls) == 1
    async with session_factory() as s:
        rows = (await s.execute(select(AgentToolCallRow))).scalars().all()
    assert [(r.status, r.seq, r.agent_role, r.agent_spec_version) for r in rows] == [
        ("ok", 1, "strategy", "1.1")
    ]
    assert SECRET_RESULT not in repr(rows[0].__dict__)

    # Temporal の再試行: stage の結果が保存済みなので何も呼ばない
    again = await activities.run_agent_stage(request)
    assert again.replayed and len(executor.calls) == 1


async def test_a_stage_retry_before_the_stage_was_saved_reuses_the_stored_tool_result(
    session_factory, artifact_store
) -> None:
    """stage の保存の前に落ちた retry: 同じ入力なら同じ tool_call_id で外部を呼ばない。"""
    world = await make_world(session_factory, artifact_store)
    executor = FakeExecutor()
    calls = {"n": 0}

    async def flaky(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        await gateway.call_tool("search_trends@1", {"q": "a"})
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash after the tool, before the stage was saved")
        return AgentOutput(artifact=None, decision=None, outcome="awaiting_public_metrics")

    activities = world.activities(
        {_STRATEGY: flaky}, tool_support=_support(session_factory, artifact_store, executor)
    )
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id, version="1.1")
    with pytest.raises(RuntimeError):
        await activities.run_agent_stage(request)
    result = await activities.run_agent_stage(request)
    assert result.outcome == "awaiting_public_metrics"
    assert len(executor.calls) == 1
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(AgentToolCallRow))).scalar() == 1


async def test_without_tool_support_a_tool_call_is_refused_and_no_row_is_written(
    session_factory, artifact_store
) -> None:
    """JPMAS2_AGENT_TOOLS 無効の worker は従来どおり。tool は呼べず agent_tool_calls は空のまま。"""
    world = await make_world(session_factory, artifact_store)
    activities = world.activities({_STRATEGY: _strategy_handler([])})
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id, version="1.1")
    with pytest.raises(ApplicationError) as exc:
        await activities.run_agent_stage(request)
    assert exc.value.type == "ToolNotAllowedError" and exc.value.non_retryable
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(AgentToolCallRow))).scalar() == 0


async def test_a_1_0_stage_never_gets_tools_even_when_tool_support_is_wired(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    executor = FakeExecutor()
    activities = world.activities(
        {_STRATEGY: _strategy_handler([])},
        tool_support=_support(session_factory, artifact_store, executor),
    )
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)  # 1.0
    with pytest.raises(ApplicationError) as exc:
        await activities.run_agent_stage(request)
    assert exc.value.type == "ToolNotAllowedError"
    assert executor.calls == []


# --- 観測 ------------------------------------------------------------------


def _avp(records: list[logging.LogRecord]) -> list[dict[str, object]]:
    return [getattr(r, RECORD_EXTRA_KEY) for r in records if hasattr(r, RECORD_EXTRA_KEY)]


def test_the_logging_observer_emits_ids_and_states_only(caplog) -> None:
    obs = LoggingToolObserver()
    with caplog.at_level(logging.DEBUG):
        obs.completed(
            ToolEvent(
                tool_id="search_trends",
                tool_version=1,
                agent_role="strategy",
                episode_id="ep-1",
                status="ok",
                tool_call_id="call-1",
                duration_ms=12,
                cost_class="research_ledger",
                cache_hit=True,
            )
        )
        obs.refused(
            ToolEvent(
                tool_id="fetch_sources",
                tool_version=None,
                agent_role="strategy",
                episode_id="ep-1",
                reason="tool_not_allowed",
            )
        )
    done, refused = _avp(caplog.records)
    assert done["event_name"] == EventName.AGENT_TOOL_COMPLETED.value
    assert refused["event_name"] == EventName.AGENT_TOOL_REFUSED.value
    assert done["episode_id"] == "ep-1" and done["tool_name"] == "search_trends"
    assert done["duration_ms"] == 12
    assert done["attributes"] == {
        "tool_call_id": "call-1",
        "tool_version": 1,
        "agent_role": "strategy",
        "status": "ok",
        "cost_class": "research_ledger",
        "cache_hit": True,
    }
    assert refused["error_code"] == "tool_not_allowed"
    assert SECRET_QUERY not in repr(caplog.records) and SECRET_RESULT not in repr(caplog.records)


async def test_a_real_stage_logs_the_tool_without_inputs_or_results(
    session_factory, artifact_store, caplog
) -> None:
    world = await make_world(session_factory, artifact_store)
    trends = tool_def(ToolId.SEARCH_TRENDS)
    support = ToolSupport(
        defs={trends.ref: trends},
        executors={ToolId.SEARCH_TRENDS: FakeExecutor()},
        store=AgentToolCallRepository(session_factory, artifact_store),
        observer=LoggingToolObserver(),
    )
    activities = world.activities({_STRATEGY: _strategy_handler([])}, tool_support=support)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id, version="1.1")
    with caplog.at_level(logging.DEBUG):
        await activities.run_agent_stage(request)
    events = [e for e in _avp(caplog.records) if e["event_name"] == "agent.tool.completed"]
    assert len(events) == 1 and events[0]["episode_id"] == world.episode_id
    assert SECRET_QUERY not in repr(caplog.records) and SECRET_RESULT not in repr(caplog.records)


def test_the_default_observer_does_nothing() -> None:
    event = ToolEvent(tool_id="x", tool_version=1, agent_role="strategy", episode_id="e")
    assert NoopToolObserver().completed(event) is None
    assert NoopToolObserver().refused(event) is None


def test_a_log_failure_does_not_change_the_tool_result(monkeypatch) -> None:
    import infrastructure.agents.tools.observer as mod

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("logger down")

    monkeypatch.setattr(mod.logger, "log", boom)
    LoggingToolObserver().completed(
        ToolEvent(tool_id="x", tool_version=1, agent_role="strategy", episode_id="e", status="ok")
    )


def test_compose_passes_the_tool_flags_to_the_agent_worker_with_off_defaults() -> None:
    from pathlib import Path

    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "compose.yaml").read_text())
    env = compose["services"]["agent-worker"]["environment"]
    assert env["JPMAS2_AGENT_TOOLS"] == "${JPMAS2_AGENT_TOOLS:-false}"
    assert env["JPMAS2_ENABLED_TOOLS"] == "${JPMAS2_ENABLED_TOOLS:-}"
