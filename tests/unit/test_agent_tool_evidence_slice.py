"""人の可視化・介入の縦断（ADR-0047 §7、INV-63/64）: 本物の FastAPI・Application Service・SQLite。

同じ操作を HTTP とアシスタント tool の両方から呼び、同じ結果であること、人の指摘（既存の
``comment`` command）が根拠の横に出ること、再送が 1 つの論理変更であることを見る。
理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest_asyncio
from sqlalchemy import func, select

from application.service import ApplicationService
from apps.api.application_service import get_application_service
from apps.api.auth import parse_principals
from apps.api.dependencies import get_settings
from apps.api.intervention_backend import InterventionModules, StoreBackedInterventionBackend
from apps.api.routers import intervention
from apps.assistant.client import InterventionClient
from apps.assistant.tools import execute_tool
from infrastructure.config import Settings
from infrastructure.db.models import AnnotationRow
from infrastructure.intervention import commands
from tests.support.tool_evidence_world import World, make_world
from tests.unit.test_assistant_e2e import BridgeTransport
from tests.unit.test_intervention_api import (
    AI_TOKEN,
    OWNER_TOKEN,
    FakeStarter,
    _auth,
    _build_app,
    _principals_json,
)


class _Unused:
    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected use of {name}")


@dataclass
class Slice:
    app: Any
    http: httpx.AsyncClient
    world: World

    async def get(self, path: str, token: str = OWNER_TOKEN) -> httpx.Response:
        return await self.http.get(
            f"/v1/intervention/episodes/{self.world.episode_id}{path}", headers=_auth(token)
        )

    async def flag(self, command_id: str, call_id: str) -> httpx.Response:
        return await self.http.post(
            f"/v1/intervention/episodes/{self.world.episode_id}/commands",
            headers=_auth(OWNER_TOKEN),
            json={
                "command_id": command_id,
                "type": "comment",
                "payload": {
                    "anchor": {"target_ref": f"tool_call:{call_id}"},
                    "body": "the source is unrelated",
                    "kind": "review_finding",
                    "flag": "wrong_evidence",
                },
            },
        )

    async def tool(self, name: str, args: dict[str, Any], call: str) -> dict[str, Any]:
        client = InterventionClient(
            "http://test",
            AI_TOKEN,
            session_id="sess-ev",
            transport=BridgeTransport(self.app, asyncio.get_running_loop()),
        )
        return await asyncio.to_thread(execute_tool, client, name, args, tool_call_id=call)


@pytest_asyncio.fixture
async def sl(session_factory, artifact_store) -> AsyncIterator[Slice]:
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
    app.dependency_overrides[intervention.get_intervention_backend] = lambda: (
        StoreBackedInterventionBackend(session_factory, modules)
    )
    world = await make_world(session_factory, artifact_store)
    service = ApplicationService(
        commands=_Unused(),  # type: ignore[arg-type]
        reads=_Unused(),  # type: ignore[arg-type]
        drafts=_Unused(),  # type: ignore[arg-type]
        starter=_Unused(),  # type: ignore[arg-type]
        agentic_enabled=True,
        tool_evidence=world.reader,
    )
    app.dependency_overrides[get_application_service] = lambda: service
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,  # type: ignore[call-arg]
        jpmas2_application_api=True,
        jpmas2_agent_tools=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield Slice(app, http, world)


async def test_http_and_the_assistant_tool_return_the_same_list_and_trace(sl: Slice) -> None:
    call = await sl.world.tool_call(1)
    http = await sl.get("/tool-calls")
    assert http.status_code == 200
    via_tool = await sl.tool("list_tool_calls", {"episode_id": sl.world.episode_id}, "c-1")
    assert via_tool["status"] == "ok" and via_tool["data"] == http.json()
    assert http.json()["calls"][0]["tool_call_id"] == call
    one = await sl.get(f"/tool-calls/{call}")
    via_one = await sl.tool(
        "get_tool_call", {"episode_id": sl.world.episode_id, "tool_call_id": call}, "c-2"
    )
    assert one.status_code == 200 and via_one["data"] == one.json()
    trace = await sl.get("/improvement-trace")
    via_trace = await sl.tool("get_improvement_trace", {"episode_id": sl.world.episode_id}, "c-3")
    assert trace.status_code == 200 and via_trace["data"] == trace.json()
    assert set(trace.json()["missing"]) >= {"topic_selection", "publication"}


async def test_tool_filters_and_validation_go_through_the_same_route(sl: Slice) -> None:
    await sl.world.tool_call(1)
    await sl.world.tool_call(2, role="learning")
    got = await sl.tool(
        "list_tool_calls",
        {"episode_id": sl.world.episode_id, "agent_role": "learning", "limit": 1},
        "c-4",
    )
    assert [c["agent_role"] for c in got["data"]["calls"]] == ["learning"]
    bad = await sl.tool(
        "list_tool_calls", {"episode_id": sl.world.episode_id, "tool_id": "nope"}, "c-5"
    )
    assert bad["status"] == "error" and bad["http_status"] == 422


async def test_a_tampered_result_is_a_500_integrity_error_over_http_and_an_error_in_the_tool(
    sl: Slice, artifact_store
) -> None:
    from infrastructure.db.agent_tool_call_repository import tool_call_object_key

    call = await sl.world.tool_call(1)
    artifact_store._objects[tool_call_object_key(sl.world.episode_id, call)] = b"{}"  # noqa: SLF001
    http = await sl.get(f"/tool-calls/{call}")
    assert http.status_code == 500 and http.json()["detail"]["error"] == "integrity_error"
    via_tool = await sl.tool(
        "get_tool_call", {"episode_id": sl.world.episode_id, "tool_call_id": call}, "c-6"
    )
    assert via_tool["status"] == "error" and via_tool["error"] == "integrity_error"


async def test_a_human_flag_via_the_existing_command_route_shows_up_next_to_the_evidence(
    sl: Slice, session_factory
) -> None:
    call = await sl.world.tool_call(1)
    first = await sl.flag("flag-cmd-0001", call)
    assert first.status_code in (200, 201, 202), first.text
    resend = await sl.flag("flag-cmd-0001", call)
    assert resend.status_code in (200, 201, 202)
    async with session_factory() as s:  # 再送は 1 つの論理変更（INV-42）
        assert await s.scalar(select(func.count()).select_from(AnnotationRow)) == 1
    detail = (await sl.get(f"/tool-calls/{call}")).json()
    (note,) = detail["annotations"]
    assert note["payload"] == {"flag": "wrong_evidence"} and note["actor_id"] == "owner"
    trace = (await sl.get("/improvement-trace")).json()
    assert [a["annotation_id"] for a in trace["human_annotations"]] == [note["annotation_id"]]
    # 別の対象を指す同じ command_id は黙って受けない
    clash = await sl.http.post(
        f"/v1/intervention/episodes/{sl.world.episode_id}/commands",
        headers=_auth(OWNER_TOKEN),
        json={
            "command_id": "flag-cmd-0001",
            "type": "comment",
            "payload": {"anchor": {"segment_id": "s1"}, "body": "other"},
        },
    )
    assert clash.status_code == 409
    assert json.loads(clash.text)["detail"]["error"] == "command_id_reused"


async def test_reading_through_http_writes_nothing(sl: Slice) -> None:
    call = await sl.world.tool_call(1)
    before = await sl.world.counts()
    for path in ("/tool-calls", f"/tool-calls/{call}", "/improvement-trace"):
        assert (await sl.get(path)).status_code == 200
    assert await sl.world.counts() == before


async def test_an_unknown_token_is_401(sl: Slice) -> None:
    assert (await sl.get("/tool-calls", token="not-a-token")).status_code == 401
