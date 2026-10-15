"""ProposeStoryboardChange の縦断スライス（INV-50）。理由は docs/testing/jpmas2-p0b-rationale.md。

本物の FastAPI・``receive_command`` / ``apply_command``・SQLite。fake は workflow の start
だけ。同じ提案を HTTP・内部 port から出し、結果の形・拒否 code が同じことを見る。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest_asyncio

from application.errors import OperationError
from application.ports import Caller
from application.use_cases.propose_storyboard_change import ProposeStoryboardChange
from apps.api.auth import parse_principals
from apps.api.dependencies import get_settings
from apps.api.intervention_backend import InterventionModules, StoreBackedInterventionBackend
from apps.api.routers import intervention
from apps.assistant.client import InterventionClient
from apps.assistant.tools import execute_tool
from contracts.application import (
    ProposeStoryboardChangeRequest,
    StoryboardProposalResult,
    storyboard_edit_command_id,
)
from contracts.assistant_tools import tool_command_id
from contracts.intervention import (
    ActorType,
    ChangeKind,
    ChangeSetStatus,
    CommandStatus,
    Scope,
)
from contracts.states import ArtifactType, EpisodeStatus
from infrastructure.config import Settings
from infrastructure.intervention import commands
from infrastructure.intervention.internal_port import SessionCommandPort
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.intervention import (
    apply,
    make_episode,
    seed_script_and_storyboard,
    store_artifact,
)
from tests.unit.test_assistant_e2e import BridgeTransport
from tests.unit.test_intervention_api import (
    AI_TOKEN,
    READER_TOKEN,
    FakeStarter,
    _auth,
    _build_app,
    _principals_json,
)

PATH = "/v1/intervention/episodes/{episode_id}/storyboard/proposals"
AGENT = Caller(ActorType.AGENT, "agent-1", frozenset({Scope.READ, Scope.COMMENT, Scope.PROPOSE}))


@dataclass
class Slice:
    app: Any
    http: httpx.AsyncClient
    session_factory: Any
    artifact_store: Any
    starter: FakeStarter
    episode_id: str
    storyboard_revision_id: str
    settings: dict[str, bool]

    def body(
        self, request_id: str | None = None, edits: list[dict] | None = None, **top: Any
    ) -> dict[str, Any]:
        return {
            "command_id": request_id or f"req-{uuid.uuid4().hex[:12]}",
            "proposal": {
                "episode_id": self.episode_id,
                "base_storyboard_revision_id": self.storyboard_revision_id,
                "rationale": "clarify the location",
                "edits": edits
                or [
                    {"scene_id": "sb1", "visual_description": "Aerial view of a castle"},
                    {"scene_id": "sb2", "visual_description": "Close-up of a hearth"},
                ],
            },
            **top,
        }

    async def post(self, body: dict[str, Any], token: str = AI_TOKEN) -> httpx.Response:
        return await self.http.post(
            PATH.format(episode_id=self.episode_id), json=body, headers=_auth(token)
        )

    def internal(self) -> ProposeStoryboardChange:
        return ProposeStoryboardChange(SessionCommandPort(self.session_factory, self.starter))

    async def commands(self) -> list[Any]:
        async with self.session_factory() as session:
            return await InterventionStore(session).list_commands(self.episode_id)

    async def change_sets(self) -> list[Any]:
        async with self.session_factory() as session:
            return await InterventionStore(session).list_change_sets(self.episode_id)

    async def apply_commands(self, command_ids: list[str]) -> None:
        for command_id in command_ids:
            await apply(self.session_factory, self.artifact_store, command_id)


@pytest_asyncio.fixture
async def sl(session_factory) -> AsyncIterator[Slice]:
    starter = FakeStarter()
    app = _build_app(session_factory, parse_principals(_principals_json()), starter, [], [])
    modules = InterventionModules(
        receive_command=commands.receive_command,
        make_principal=lambda actor, scopes: commands.Principal(actor=actor, scopes=scopes),
        read_model=lambda: None,
        build_packet=lambda view: view,
        render_markdown=lambda packet: "",
        parse_proposal_file=lambda raw: None,  # type: ignore[arg-type,return-value]
        command_starter=starter,
        receive_error_types=(commands.CommandReceiveError,),
    )
    backend = StoreBackedInterventionBackend(session_factory, modules)
    app.dependency_overrides[intervention.get_intervention_backend] = lambda: backend
    flags = {"on": True}
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,  # type: ignore[call-arg]
        jpmas2_application_api=flags["on"],
    )
    store = InMemoryArtifactStore()
    async with session_factory() as session:
        episode_id = await make_episode(session, EpisodeStatus.ASSETS_READY)
        _script, storyboard, _media = await seed_script_and_storyboard(session, store, episode_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield Slice(app, http, session_factory, store, starter, episode_id, storyboard.id, flags)


def shape(result: dict[str, Any]) -> dict[str, Any]:
    """入口に依らず同じであるべき部分（id と時刻を除く）。"""
    return {
        "keys": sorted(result),
        "commands": [
            {"scene_id": c["scene_id"], "status": c["status"], "created": c["created"]}
            for c in result["commands"]
        ],
    }


async def test_http_and_internal_port_give_the_same_result_shape_and_change_sets(sl: Slice) -> None:
    """INV-50: 入口が違っても同じ結果型で、どちらも scene.override_visual の ChangeSet になる。"""
    http = await sl.post(sl.body())
    assert http.status_code == 202
    internal = await sl.internal().execute(
        AGENT,
        uuid.UUID(sl.episode_id),
        ProposeStoryboardChangeRequest.model_validate(sl.body()),
    )
    assert isinstance(internal, StoryboardProposalResult)
    assert shape(http.json()) == shape(internal.model_dump(mode="json"))
    assert http.json()["commands"][0]["status"] == CommandStatus.RECEIVED.value
    assert len(await sl.commands()) == 4
    await sl.apply_commands([c["command_id"] for c in http.json()["commands"]])
    await sl.apply_commands([c.command_id for c in internal.commands])
    sets = await sl.change_sets()
    assert len(sets) == 4
    assert {s.kind for s in sets} == {ChangeKind.SCENE_OVERRIDE_VISUAL}
    assert {s.status for s in sets} == {ChangeSetStatus.PROPOSED}
    actors = {s.actor.type for s in sets}
    assert actors == {ActorType.AI_ASSISTANT, ActorType.AGENT}
    assert len(sl.starter.started) == 4


async def test_resending_the_same_request_returns_the_existing_commands(sl: Slice) -> None:
    body = sl.body("req-resend-0001")
    first = await sl.post(body)
    again = await sl.post(body)
    assert (first.status_code, again.status_code) == (202, 200)
    assert [c["command_id"] for c in first.json()["commands"]] == [
        c["command_id"] for c in again.json()["commands"]
    ]
    assert [c["created"] for c in again.json()["commands"]] == [False, False]
    assert len(await sl.commands()) == 2
    request = ProposeStoryboardChangeRequest.model_validate(sl.body("req-resend-0002"))
    use_case = sl.internal()
    one = await use_case.execute(AGENT, uuid.UUID(sl.episode_id), request)
    two = await use_case.execute(AGENT, uuid.UUID(sl.episode_id), request)
    assert [c.created for c in one.commands] == [True, True]
    assert [c.created for c in two.commands] == [False, False]
    assert len(await sl.commands()) == 4
    await sl.apply_commands([c.command_id for c in one.commands])
    assert len(await sl.change_sets()) == 2


async def test_the_same_request_id_with_other_content_is_command_id_reused(sl: Slice) -> None:
    await sl.post(sl.body("req-reuse-00001"))
    other = sl.body(
        "req-reuse-00001", [{"scene_id": "sb1", "visual_description": "Something else"}]
    )
    http = await sl.post(other)
    assert http.status_code == 409 and http.json()["detail"]["error"] == "command_id_reused"
    try:
        await sl.internal().execute(
            AGENT, uuid.UUID(sl.episode_id), ProposeStoryboardChangeRequest.model_validate(other)
        )
    except OperationError as exc:
        assert exc.code.value == "command_id_reused"
    else:
        raise AssertionError("expected command_id_reused")
    assert len(await sl.commands()) == 2


async def test_unsupported_fields_give_unsupported_change_from_every_entry(sl: Slice) -> None:
    body = sl.body(edits=[{"scene_id": "sb1", "visual_description": "x", "framing": "wide shot"}])
    http = await sl.post(body)
    assert http.status_code == 422
    assert http.json()["detail"]["error"] == "unsupported_change"
    assert "storyboard.patch_scenes" in http.json()["detail"]["message"]
    try:
        await sl.internal().execute(
            AGENT, uuid.UUID(sl.episode_id), ProposeStoryboardChangeRequest.model_validate(body)
        )
    except OperationError as exc:
        assert exc.code.value == "unsupported_change"
    else:
        raise AssertionError("expected unsupported_change")
    assert await sl.commands() == []


async def test_missing_propose_scope_is_missing_scope(sl: Slice) -> None:
    http = await sl.post(sl.body(), READER_TOKEN)
    assert http.status_code == 403 and http.json()["detail"]["error"] == "missing_scope"
    reader = Caller(ActorType.AGENT, "r", frozenset({Scope.READ}))
    try:
        await sl.internal().execute(
            reader,
            uuid.UUID(sl.episode_id),
            ProposeStoryboardChangeRequest.model_validate(sl.body()),
        )
    except OperationError as exc:
        assert exc.code.value == "missing_scope"
    else:
        raise AssertionError("expected missing_scope")
    assert await sl.commands() == []


async def test_the_internal_port_refuses_a_human_caller(sl: Slice) -> None:
    human = Caller(ActorType.HUMAN, "owner", frozenset(Scope))
    try:
        await sl.internal().execute(
            human,
            uuid.UUID(sl.episode_id),
            ProposeStoryboardChangeRequest.model_validate(sl.body()),
        )
    except OperationError as exc:
        assert exc.code.value == "forbidden"
    else:
        raise AssertionError("expected forbidden")
    assert await sl.commands() == []


async def test_the_flag_off_is_404_feature_disabled_before_anything_else(sl: Slice) -> None:
    sl.settings["on"] = False
    for token in (AI_TOKEN, "not-a-token"):
        response = await sl.post(sl.body(), token)
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "feature_disabled"
    garbage = await sl.http.post(PATH.format(episode_id=sl.episode_id), json={"x": 1})
    assert garbage.status_code == 404
    assert await sl.commands() == []


async def test_unknown_episode_and_episode_mismatch(sl: Slice) -> None:
    other = str(uuid.uuid4())
    body = sl.body()
    body["proposal"]["episode_id"] = other
    response = await sl.http.post(PATH.format(episode_id=other), json=body, headers=_auth(AI_TOKEN))
    assert response.status_code == 404 and response.json()["detail"]["error"] == "episode_not_found"
    mismatch = await sl.http.post(
        PATH.format(episode_id=other), json=sl.body(), headers=_auth(AI_TOKEN)
    )
    assert mismatch.status_code == 422 and mismatch.json()["detail"]["error"] == "invalid_payload"


async def test_a_stale_base_revision_ends_as_a_conflict_without_a_change_set(sl: Slice) -> None:
    """既存の競合の扱いのまま: 受付は成功し、適用で CONFLICT（ChangeSet は作らない）。"""
    async with sl.session_factory() as session:
        board = await store_artifact(
            session,
            sl.artifact_store,
            sl.episode_id,
            ArtifactType.STORYBOARD,
            {"stand_in": "a newer storyboard"},
        )
    assert board.id != sl.storyboard_revision_id  # 新しい storyboard が現行になった
    response = await sl.post(sl.body())  # 古い revision を base にする
    assert response.status_code == 202
    await sl.apply_commands([c["command_id"] for c in response.json()["commands"]])
    rows = await sl.commands()
    assert {c.status for c in rows} == {CommandStatus.CONFLICT}
    assert await sl.change_sets() == []


# --------------------------------------------------------------------------- アシスタント tool


def tool_args(sl: Slice, **top: Any) -> dict[str, Any]:
    proposal = sl.body()["proposal"]
    return {**proposal, **top}


async def run_tool(sl: Slice, args: dict[str, Any], call: str, token: str = AI_TOKEN) -> dict:
    client = InterventionClient(
        "http://test",
        token,
        session_id="sess-sb",
        transport=BridgeTransport(sl.app, asyncio.get_running_loop()),
    )
    return await asyncio.to_thread(
        execute_tool, client, "propose_storyboard_patch", args, tool_call_id=call
    )


async def test_the_assistant_tool_gives_the_same_result_shape_and_change_sets(sl: Slice) -> None:
    result = await run_tool(sl, tool_args(sl), "call-1")
    assert result["status"] == "submitted" and result["replayed"] is False
    http = await sl.post(sl.body())
    assert shape(result["proposal"]) == shape(http.json())
    expected_request_id = tool_command_id("sess-sb", "call-1")
    assert result["proposal"]["request_command_id"] == expected_request_id
    assert result["proposal"]["commands"][0]["command_id"] == storyboard_edit_command_id(
        expected_request_id, 0
    )
    await sl.apply_commands([c["command_id"] for c in result["proposal"]["commands"]])
    sets = await sl.change_sets()
    assert {s.kind for s in sets} == {ChangeKind.SCENE_OVERRIDE_VISUAL} and len(sets) == 2
    assert {s.actor.type for s in sets} == {ActorType.AI_ASSISTANT}


async def test_a_retried_tool_call_returns_the_existing_commands(sl: Slice) -> None:
    first = await run_tool(sl, tool_args(sl), "call-7")
    again = await run_tool(sl, tool_args(sl), "call-7")
    assert (first["replayed"], again["replayed"]) == (False, True)
    assert [c["created"] for c in again["proposal"]["commands"]] == [False, False]
    assert len(await sl.commands()) == 2
    clash = await run_tool(
        sl,
        tool_args(sl, edits=[{"scene_id": "sb1", "visual_description": "different"}]),
        "call-7",
    )
    assert clash["status"] == "command_id_reused" and clash["error"] == "command_id_reused"
    assert len(await sl.commands()) == 2


async def test_the_tool_reports_the_same_refusal_codes_as_the_other_entries(sl: Slice) -> None:
    edits = [{"scene_id": "sb1", "visual_description": "x", "camera_movement": "push in"}]
    unsupported = await run_tool(sl, tool_args(sl, edits=edits), "c1")
    assert unsupported["status"] == "error" and unsupported["error"] == "unsupported_change"
    assert unsupported["fields"] == ["camera_movement"]
    scope = await run_tool(sl, tool_args(sl), "c2", READER_TOKEN)
    assert scope["status"] == "missing_scope" and scope["required_scope"] == "propose"
    sl.settings["on"] = False
    off = await run_tool(sl, tool_args(sl), "c3")
    assert off["status"] == "error" and off["error"] == "feature_disabled"
    assert await sl.commands() == []


async def test_the_tool_passes_expected_control_revision_through(sl: Slice) -> None:
    result = await run_tool(sl, tool_args(sl, expected_control_revision=0), "c9")
    assert result["status"] == "submitted"
    assert {c.expected_control_revision for c in await sl.commands()} == {0}
