"""``intervention.api.request``（ADR-0042 §6）。理由は docs/testing/assistant-rationale.md §1。"""
# ruff: noqa: F811, F401  (他の test module の fixture ``api`` を import して使う。pytest の標準の共有方法)

from __future__ import annotations

import logging

from tests.support.log_capture import capture_json
from tests.unit.test_intervention_api import (
    AI_TOKEN,
    OWNER_TOKEN,
    PREFIX,
    Harness,
    _auth,
    _comment,
    api,
)


async def test_a_request_is_logged_with_route_actor_status_and_correlation(
    api: Harness,
) -> None:
    headers = {
        **_auth(AI_TOKEN),
        "X-Correlation-ID": "assistant:sess-1:call-9",
        "X-Assistant-Tool": "get_episode_status",
    }
    with capture_json() as logs:
        response = await api.client.get(f"{PREFIX}/episodes/{api.episode_id}", headers=headers)
    assert response.status_code == 200
    [done] = logs.events("intervention.api.request")
    assert done["http_route"] == f"{PREFIX}/episodes/{{episode_id}}"
    assert done["http_method"] == "GET"
    assert done["http_status"] == 200
    assert done["actor_type"] == "ai_assistant"
    assert done["attributes"]["actor_id"] == "chat-assistant"
    assert done["correlation_id"] == "assistant:sess-1:call-9"
    assert done["tool_name"] == "get_episode_status"
    assert done["episode_id"] == api.episode_id
    assert done["outcome"] == "succeeded"
    assert isinstance(done["duration_ms"], float)


async def test_neither_token_nor_authorization_nor_body_is_logged(api: Harness, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    body = _comment(body="本文の秘密めいた文言ZZZ")
    with capture_json() as logs:
        response = await api.client.post(
            f"{PREFIX}/episodes/{api.episode_id}/commands",
            json=body,
            headers=_auth(OWNER_TOKEN),
        )
    assert response.status_code == 202
    for text in (logs.stream.getvalue(), caplog.text):
        assert OWNER_TOKEN not in text
        assert "authorization" not in text.lower()
        assert "Bearer" not in text
        assert "本文の秘密めいた文言ZZZ" not in text


async def test_a_rejected_request_is_logged_without_an_actor(api: Harness) -> None:
    with capture_json() as logs:
        response = await api.client.get(
            f"{PREFIX}/episodes/{api.episode_id}", headers=_auth("wrong-token-xyz")
        )
    assert response.status_code == 401
    [done] = logs.events("intervention.api.request")
    assert done["http_status"] == 401
    assert done["outcome"] == "rejected"
    assert "actor_type" not in done
    assert "wrong-token-xyz" not in logs.stream.getvalue()


async def test_unsafe_header_values_are_not_logged(api: Harness) -> None:
    headers = {
        **_auth(AI_TOKEN),
        "X-Correlation-ID": "bad value\twith spaces",
        "X-Assistant-Tool": "Not A Tool!",
    }
    with capture_json() as logs:
        await api.client.get(f"{PREFIX}/episodes/{api.episode_id}", headers=headers)
    [done] = logs.events("intervention.api.request")
    assert "correlation_id" not in done and "tool_name" not in done


async def test_only_the_intervention_prefix_is_logged_as_intervention(api: Harness) -> None:
    with capture_json() as logs:
        await api.client.get("/healthz")
    assert logs.events("intervention.api.request") == []
    assert len(logs.events("api.request.completed")) == 1
