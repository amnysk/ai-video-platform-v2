"""``scripts/agentic.py``（所有者向け CLI、ADR-0043 §6）。DB は SQLite、Temporal は fake。

各テストの理由: docs/testing/jpmas2-p1c-rationale.md
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import pathlib
import sys
import uuid
from typing import Any

import pytest

from contracts.agentic_pipeline import PinWorkflowPlanRequest, agentic_episode_workflow_id
from contracts.application import StartedWorkflow
from infrastructure.agents.draft_port import DbEpisodeDraftPort
from workers.pipeline.agentic_activities import AgenticPipelineActivities

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agentic.py"
BUCKET = "artifacts"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("agentic_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load()


class FakeStarter:
    def __init__(self) -> None:
        self.started: list[uuid.UUID] = []
        self.learned: list[uuid.UUID] = []

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow:
        self.started.append(episode_id)
        return StartedWorkflow(
            workflow_id=agentic_episode_workflow_id(str(episode_id)), started=True
        )

    async def start_learning(self, episode_id: uuid.UUID) -> StartedWorkflow:
        self.learned.append(episode_id)
        return StartedWorkflow(workflow_id=f"agentic-learning-{episode_id}", started=True)


def _run(argv: list[str], session_factory: Any, store: Any, *, enabled: bool, starter: Any = None):
    starter = starter or FakeStarter()

    async def deps() -> Any:
        return cli.Deps(
            drafts=DbEpisodeDraftPort(session_factory=session_factory, store=store, bucket=BUCKET),
            starter=starter,
            session_factory=session_factory,
            store=store,
            agentic_enabled=enabled,
        )

    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, deps_factory=deps, out=out, err=err)
    return code, out.getvalue(), err.getvalue(), starter


def test_plan_previews_the_compile_without_touching_the_database() -> None:
    async def forbidden() -> Any:
        raise AssertionError("plan --template must not open any dependency")

    out = io.StringIO()
    code = cli.main(
        ["plan", "--template", "shorts_agentic"], deps_factory=forbidden, out=out, err=io.StringIO()
    )
    payload = json.loads(out.getvalue())
    assert code == 0 and payload["pinned"] is False
    assert payload["plan"]["execution"] == "agentic"


@pytest.mark.parametrize(
    ("template", "code"),
    [("daily_legacy", "template_not_runnable"), ("nope", "unknown_template")],
)
def test_plan_reports_the_refusal_code(template: str, code: str) -> None:
    out = io.StringIO()
    exit_code = cli.main(
        ["plan", "--template", template], deps_factory=None, out=out, err=io.StringIO()
    )
    assert exit_code == 1 and json.loads(out.getvalue())["error"]["code"] == code


def test_draft_prints_the_command_id_and_resending_it_returns_the_same_episode(
    session_factory, artifact_store
) -> None:
    argv = ["draft", "--theme", "Edo firefighters", "--command-id", "cli-command-0001"]
    code, out, err, _ = _run(argv, session_factory, artifact_store, enabled=False)
    first = json.loads(out)
    assert code == 0 and first["created"] is True and "cli-command-0001" in err
    code, out, _, _ = _run(argv, session_factory, artifact_store, enabled=False)
    again = json.loads(out)
    assert code == 0 and again["created"] is False and again["episode_id"] == first["episode_id"]


def test_draft_with_the_same_command_id_and_another_theme_is_rejected(
    session_factory, artifact_store
) -> None:
    base = ["draft", "--command-id", "cli-command-0002", "--theme"]
    _run([*base, "one"], session_factory, artifact_store, enabled=False)
    code, out, _, _ = _run([*base, "two"], session_factory, artifact_store, enabled=False)
    assert code == 1 and json.loads(out)["error"]["code"] == "command_id_reused"


def test_draft_without_a_command_id_generates_one(session_factory, artifact_store) -> None:
    code, out, err, _ = _run(
        ["draft", "--theme", "x y z"], session_factory, artifact_store, enabled=False
    )
    assert code == 0
    generated = err.split("command_id=")[1].strip()
    uuid.UUID(generated)
    assert json.loads(out)["command_id"] == generated


def test_draft_with_an_invalid_input_is_a_usage_error(session_factory, artifact_store) -> None:
    code, out, err, _ = _run(
        ["draft", "--theme", "ok", "--duration", "5"],
        session_factory,
        artifact_store,
        enabled=False,
    )
    assert code == 2 and out == "" and "invalid input" in err


def test_there_is_no_way_to_pass_a_budget_visibility_or_schedule() -> None:
    parser = cli.build_parser()
    draft = parser._subparsers._group_actions[0].choices["draft"]  # type: ignore[union-attr]
    options = {o for a in draft._actions for o in a.option_strings}
    for word in ("budget", "visibility", "privacy", "schedule", "publish", "cost", "price"):
        assert not any(word in o for o in options), word


def test_start_is_refused_when_the_flag_is_off(session_factory, artifact_store) -> None:
    code, out, _, _ = _run(
        ["draft", "--theme", "abc def", "--command-id", "cli-command-0003"],
        session_factory,
        artifact_store,
        enabled=True,
    )
    episode_id = json.loads(out)["episode_id"]
    code, out, _, starter = _run(
        ["start", episode_id], session_factory, artifact_store, enabled=False
    )
    assert code == 1 and json.loads(out)["error"]["code"] == "feature_disabled"
    assert starter.started == []
    code, out, _, starter = _run(
        ["start", episode_id], session_factory, artifact_store, enabled=True
    )
    assert code == 0 and json.loads(out)["started"] is True
    assert starter.started == [uuid.UUID(episode_id)]


def test_learn_requires_the_flag_and_starts_the_learning_workflow(
    session_factory, artifact_store
) -> None:
    episode_id = str(uuid.uuid4())
    code, out, _, starter = _run(
        ["learn", episode_id], session_factory, artifact_store, enabled=False
    )
    assert code == 1 and json.loads(out)["error"]["code"] == "feature_disabled"
    assert starter.learned == []
    code, out, _, starter = _run(
        ["learn", episode_id], session_factory, artifact_store, enabled=True
    )
    assert code == 0 and starter.learned == [uuid.UUID(episode_id)]


def test_a_malformed_episode_id_is_a_usage_error(session_factory, artifact_store) -> None:
    code, _, err, _ = _run(["start", "not-a-uuid"], session_factory, artifact_store, enabled=True)
    assert code == 2 and "UUID" in err


def test_plan_reads_the_pinned_plan_of_an_episode(session_factory, artifact_store) -> None:
    code, out, _, _ = _run(
        ["draft", "--theme", "abc def", "--command-id", "cli-command-0004"],
        session_factory,
        artifact_store,
        enabled=True,
    )
    episode_id = json.loads(out)["episode_id"]
    code, out, _, _ = _run(
        ["plan", "--episode", episode_id], session_factory, artifact_store, enabled=True
    )
    assert code == 1 and json.loads(out)["error"]["code"] == "no_pinned_plan"
    pinner = AgenticPipelineActivities(
        session_factory=session_factory, store=artifact_store, bucket=BUCKET
    )
    request = PinWorkflowPlanRequest(
        episode_id=episode_id, template_id="shorts_agentic", template_version="1"
    )
    asyncio.run(pinner.pin_workflow_plan(request))
    code, out, _, _ = _run(
        ["plan", "--episode", episode_id], session_factory, artifact_store, enabled=True
    )
    payload = json.loads(out)
    assert (
        code == 0
        and payload["pinned"] is True
        and payload["plan"]["template_id"] == "shorts_agentic"
    )
