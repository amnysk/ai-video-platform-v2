"""adapter を本物の FastAPI アプリ（介入 API）に繋ぐ（INV-49、ADR-0042 §4・§6）。

理由は docs/testing/assistant-rationale.md §6。store と SQLite は本物、担当 A / B の関数だけ fake
（``tests/unit/test_intervention_api.py`` と同じ）。sync の adapter は別 thread で動かし、
ASGI アプリへは event loop 経由の橋で繋ぐ。ネットワークは使わない。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest_asyncio

from apps.api.auth import parse_principals
from apps.assistant.client import InterventionClient
from apps.assistant.tools import execute_tool
from contracts.assistant_tools import tool_command_id
from contracts.intervention import CommandType
from infrastructure.intervention.store import InterventionStore
from tests.support.log_capture import capture_json
from tests.unit.test_intervention_api import (
    AI_TOKEN,
    READER_TOKEN,
    FakeStarter,
    Harness,
    _build_app,
    _principals_json,
    _seed_change_set,
    _seed_episode,
)


class BridgeTransport(httpx.BaseTransport):
    """sync の httpx.Client から、同じ process の ASGI アプリを呼ぶ橋。"""

    def __init__(self, app: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._transport = httpx.ASGITransport(app=app)
        self._loop = loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def go() -> httpx.Response:
            rebuilt = httpx.Request(
                request.method, request.url, headers=request.headers, content=request.read()
            )
            response = await self._transport.handle_async_request(rebuilt)
            body = await response.aread()
            return httpx.Response(response.status_code, headers=response.headers, content=body)

        return asyncio.run_coroutine_threadsafe(go(), self._loop).result(30)


@dataclass
class E2E:
    harness: Harness
    app: Any
    loop: asyncio.AbstractEventLoop

    def client(self, token: str = AI_TOKEN, session: str = "sess-e2e") -> InterventionClient:
        return InterventionClient(
            "http://test",
            token,
            session_id=session,
            transport=BridgeTransport(self.app, self.loop),
        )

    async def run(self, client: InterventionClient, tool: str, args: dict[str, Any], call: str):
        return await asyncio.to_thread(execute_tool, client, tool, args, tool_call_id=call)

    async def commands(self):
        async with self.harness.session_factory() as session:
            return await InterventionStore(session).list_commands(self.harness.episode_id)


@pytest_asyncio.fixture
async def e2e(session_factory) -> AsyncIterator[E2E]:
    starter = FakeStarter()
    app = _build_app(session_factory, parse_principals(_principals_json()), starter, [], [])
    episode_id = await _seed_episode(session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        harness = Harness(http, session_factory, starter, [], [], episode_id)
        yield E2E(harness, app, asyncio.get_running_loop())


def _comment_args(episode_id: str, body: str = "ここは短く") -> dict[str, Any]:
    return {"episode_id": episode_id, "anchor": {"segment_id": "s1"}, "body": body}


async def test_a_retried_tool_call_is_one_logical_change(e2e: E2E) -> None:
    """INV-49: 同じ tool 呼び出しの再試行は同じ command_id で、サーバは既存の command を返す。"""
    client = e2e.client()
    args = _comment_args(e2e.harness.episode_id)
    first = await e2e.run(client, "add_comment", args, "call-1")
    again = await e2e.run(client, "add_comment", args, "call-1")
    assert first["status"] == again["status"] == "submitted"
    assert first["replayed"] is False and again["replayed"] is True  # 202 と 200
    expected = tool_command_id("sess-e2e", "call-1")
    assert first["command_id"] == again["command_id"] == expected
    rows = await e2e.commands()
    assert [c.command_id for c in rows] == [expected]
    assert rows[0].type is CommandType.COMMENT
    assert (rows[0].actor.type.value, rows[0].actor.id) == ("ai_assistant", "chat-assistant")
    assert rows[0].correlation_id == "assistant:sess-e2e:call-1"


async def test_a_new_tool_call_is_a_new_command_and_a_reused_id_with_other_input_is_refused(
    e2e: E2E,
) -> None:
    client = e2e.client()
    ep = e2e.harness.episode_id
    await e2e.run(client, "add_comment", _comment_args(ep), "call-1")
    await e2e.run(client, "add_comment", _comment_args(ep), "call-2")
    assert len(await e2e.commands()) == 2
    clash = await e2e.run(client, "add_comment", _comment_args(ep, "別の本文"), "call-1")
    assert clash["status"] == "command_id_reused"
    assert len(await e2e.commands()) == 2


async def test_apply_with_a_stale_cost_sends_no_command_to_the_real_api(e2e: E2E) -> None:
    change_set = await _seed_change_set(e2e.harness, cost=Decimal("0.25"))
    client = e2e.client()
    ep = e2e.harness.episode_id
    stale = await e2e.run(
        client,
        "apply_change",
        {
            "episode_id": ep,
            "change_set_id": change_set.change_set_id,
            "acknowledged_estimated_cost_usd": 0.1,
        },
        "apply-1",
    )
    assert stale["status"] == "cost_changed"
    assert stale["current_estimated_cost_usd"] == 0.25
    assert await e2e.commands() == []
    good = await e2e.run(
        client,
        "apply_change",
        {
            "episode_id": ep,
            "change_set_id": change_set.change_set_id,
            "acknowledged_estimated_cost_usd": 0.25,
        },
        "apply-2",
    )
    assert good["status"] == "submitted"
    [row] = await e2e.commands()
    assert row.type is CommandType.APPLY_CHANGE
    assert row.payload == {
        "change_set_id": change_set.change_set_id,
        "max_estimated_cost_usd": 0.25,
    }


async def test_a_principal_without_the_scope_gets_a_missing_scope_result(e2e: E2E) -> None:
    client = e2e.client(READER_TOKEN)  # read だけ
    result = await e2e.run(client, "add_comment", _comment_args(e2e.harness.episode_id), "c1")
    assert result["status"] == "missing_scope" and result["required_scope"] == "comment"
    assert await e2e.commands() == []
    read = await e2e.run(client, "get_episode_status", {"episode_id": e2e.harness.episode_id}, "c2")
    assert read["status"] == "ok"


async def test_a_wrong_token_is_reported_not_retried_with_anything_else(e2e: E2E) -> None:
    client = e2e.client("not-a-registered-token")
    result = await e2e.run(
        client, "get_episode_status", {"episode_id": e2e.harness.episode_id}, "c"
    )
    assert result["status"] == "error" and result["http_status"] == 401


async def test_the_server_logs_the_assistant_correlation_and_never_the_token(e2e: E2E) -> None:
    client = e2e.client(session="log-sess")
    with capture_json() as logs:
        await e2e.run(client, "add_comment", _comment_args(e2e.harness.episode_id), "call-77")
        await e2e.run(client, "get_episode_status", {"episode_id": e2e.harness.episode_id}, "c-78")
    events = logs.events("intervention.api.request")
    assert [(e["tool_name"], e["correlation_id"]) for e in events] == [
        ("add_comment", "assistant:log-sess:call-77"),
        ("get_episode_status", "assistant:log-sess:c-78"),
    ]
    assert {e["actor_type"] for e in events} == {"ai_assistant"}
    text = logs.stream.getvalue()
    assert AI_TOKEN not in text and json.dumps("Bearer") not in text
