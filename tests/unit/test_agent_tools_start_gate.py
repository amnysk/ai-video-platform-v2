"""shorts_agentic_tools@1 の起動は JPMAS2_AGENT_TOOLS が必要（ADR-0047 §2 / §9、INV-55 の延長）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any

import pytest

from application.errors import OperationError, OperationErrorCode
from application.use_cases.agentic_production import CreateEpisodeDraft, StartAgenticProduction
from contracts.agentic import UserBrief
from contracts.agentic_pipeline import agentic_episode_workflow_id
from contracts.application import (
    CreateEpisodeDraftRequest,
    StartAgenticProductionRequest,
    StartedWorkflow,
    WorkerCheck,
)
from infrastructure.agents.draft_port import DbEpisodeDraftPort

BUCKET = "artifacts"


class RecordingStarter:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, str, str]] = []

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow:
        self.calls.append((episode_id, template_id, template_version))
        return StartedWorkflow(
            workflow_id=agentic_episode_workflow_id(str(episode_id)), started=True
        )

    async def probe_workers(self) -> WorkerCheck:
        return WorkerCheck.UNKNOWN


async def _draft(session_factory: Any, artifact_store: Any, template: str) -> uuid.UUID:
    port = DbEpisodeDraftPort(session_factory=session_factory, store=artifact_store, bucket=BUCKET)
    brief = UserBrief(theme="Edo firefighters", template_id=template, template_version="1")
    result = await CreateEpisodeDraft(port).execute(
        CreateEpisodeDraftRequest(command_id=f"draft-{template}-0001", brief=brief)
    )
    return result.episode_id


def _use_case(session_factory: Any, artifact_store: Any, starter: Any, **flags: bool):
    port = DbEpisodeDraftPort(session_factory=session_factory, store=artifact_store, bucket=BUCKET)
    return StartAgenticProduction(port, starter, **flags)


async def test_the_tool_template_is_refused_with_feature_disabled_when_tools_are_off(
    session_factory, artifact_store
) -> None:
    episode_id = await _draft(session_factory, artifact_store, "shorts_agentic_tools")
    starter = RecordingStarter()
    use_case = _use_case(session_factory, artifact_store, starter, enabled=True)
    with pytest.raises(OperationError) as err:
        await use_case.execute(StartAgenticProductionRequest(episode_id=episode_id))
    assert err.value.code is OperationErrorCode.FEATURE_DISABLED
    assert "JPMAS2_AGENT_TOOLS" in str(err.value)
    assert starter.calls == []


async def test_the_tool_template_starts_when_both_flags_are_on(
    session_factory, artifact_store
) -> None:
    episode_id = await _draft(session_factory, artifact_store, "shorts_agentic_tools")
    starter = RecordingStarter()
    use_case = _use_case(
        session_factory, artifact_store, starter, enabled=True, agent_tools_enabled=True
    )
    result = await use_case.execute(StartAgenticProductionRequest(episode_id=episode_id))
    assert result.started is True
    assert starter.calls == [(episode_id, "shorts_agentic_tools", "1")]


async def test_the_agentic_flag_is_still_checked_first(session_factory, artifact_store) -> None:
    episode_id = await _draft(session_factory, artifact_store, "shorts_agentic_tools")
    use_case = _use_case(
        session_factory, artifact_store, RecordingStarter(), enabled=False, agent_tools_enabled=True
    )
    with pytest.raises(OperationError) as err:
        await use_case.execute(StartAgenticProductionRequest(episode_id=episode_id))
    assert err.value.code is OperationErrorCode.FEATURE_DISABLED
    assert "JPMAS2_AGENTIC_WORKFLOWS" in str(err.value)


async def test_the_existing_template_does_not_need_the_tools_flag(
    session_factory, artifact_store
) -> None:
    episode_id = await _draft(session_factory, artifact_store, "shorts_agentic")
    starter = RecordingStarter()
    use_case = _use_case(session_factory, artifact_store, starter, enabled=True)
    await use_case.execute(StartAgenticProductionRequest(episode_id=episode_id))
    assert starter.calls == [(episode_id, "shorts_agentic", "1")]


def test_the_cli_start_passes_the_tools_flag(session_factory, artifact_store) -> None:
    import importlib.util
    import pathlib
    import sys

    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agentic.py"
    spec = importlib.util.spec_from_file_location("agentic_cli_tools", path)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)
    starter = RecordingStarter()

    def run(argv: list[str], *, tools: bool) -> tuple[int, str]:
        async def deps() -> Any:
            return cli.Deps(
                drafts=DbEpisodeDraftPort(
                    session_factory=session_factory, store=artifact_store, bucket=BUCKET
                ),
                starter=starter,
                session_factory=session_factory,
                store=artifact_store,
                agentic_enabled=True,
                agent_tools_enabled=tools,
            )

        out = io.StringIO()
        return cli.main(argv, deps_factory=deps, out=out, err=io.StringIO()), out.getvalue()

    code, out = run(
        [
            "draft",
            "--theme",
            "abc def",
            "--template",
            "shorts_agentic_tools",
            "--command-id",
            "cli-tools-0001",
        ],
        tools=False,
    )
    assert code == 0
    episode_id = json.loads(out)["episode_id"]
    code, out = run(["start", episode_id], tools=False)
    assert code == 1 and json.loads(out)["error"]["code"] == "feature_disabled"
    assert starter.calls == []
    code, _ = run(["start", episode_id], tools=True)
    assert code == 0 and len(starter.calls) == 1
