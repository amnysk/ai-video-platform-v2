"""介入 API（/v1/intervention、JP-MAS-001 / ADR-0041 §8、INV-47・INV-16）。

store と SQLite は本物、担当 A / B の関数（``receive_command`` / read model / ReviewPacket /
変更案ファイル）と workflow の start は fake（``InterventionModules``）。
理由は ``docs/testing/intervention-c-rationale.md``。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from apps.api.auth import parse_principals, token_sha256
from apps.api.intervention_backend import (
    InterventionModules,
    ProposalFile,
    ProposalFileError,
    StoreBackedInterventionBackend,
)
from contracts.intervention import (
    REQUIRED_SCOPE,
    ActorType,
    ChangeKind,
    CommandEnvelope,
    CommandStatus,
    CommandType,
    ProposeChangePayload,
    RationaleKind,
    Scope,
    payload_sha256,
)
from contracts.states import ArtifactType
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.intervention.store import Actor, InterventionStore

PREFIX = "/v1/intervention"

OWNER_TOKEN = "owner-secret-token-0001"
AI_TOKEN = "assistant-secret-token-0002"
READER_TOKEN = "reader-secret-token-0003"
ALL_TOKENS = (OWNER_TOKEN, AI_TOKEN, READER_TOKEN)

#: AI に与えうる全 scope（人専用を除く）
AI_SCOPES = ["read", "comment", "propose", "hold", "apply", "resume"]


def _principals_json() -> str:
    return json.dumps(
        {
            token_sha256(OWNER_TOKEN): {
                "actor_type": "human",
                "actor_id": "owner",
                "scopes": [s.value for s in Scope],
            },
            token_sha256(AI_TOKEN): {
                "actor_type": "ai_assistant",
                "actor_id": "chat-assistant",
                "scopes": AI_SCOPES,
            },
            token_sha256(READER_TOKEN): {
                "actor_type": "human",
                "actor_id": "reader",
                "scopes": ["read"],
            },
        }
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- fakes（担当 A / B）


@dataclass(frozen=True)
class FakePrincipal:
    actor: Actor
    scopes: frozenset[Scope]


@dataclass
class FakeReceiveResult:
    command: Any
    created: bool


async def fake_receive_command(session, episode_id, envelope: CommandEnvelope, principal):
    """B の ``receive_command`` の最小の代役: store に RECEIVED で保存する（commit しない）。"""
    command, created = await InterventionStore(session).insert_command(
        command_id=envelope.command_id,
        episode_id=episode_id,
        type=envelope.type,
        actor=principal.actor,
        granted_scope=REQUIRED_SCOPE[envelope.type],
        expected_control_revision=envelope.expected_control_revision,
        expected_target_revision=envelope.expected_target_revision,
        payload=envelope.payload,
        payload_sha256=payload_sha256(envelope),
        correlation_id=envelope.correlation_id,
    )
    return FakeReceiveResult(command=command, created=created)


@dataclass
class FakeView:
    episode_id: str
    phase: str = "planned"
    control_state: str = "running"


class FakeReadModel:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls

    async def load(self, episode_id: str) -> FakeView:
        self._calls.append(("load", episode_id))
        return FakeView(episode_id=str(episode_id))

    def compare_revisions(self, a: str, b: str) -> dict[str, Any]:
        self._calls.append(("compare", (a, b)))
        return {"a": a, "b": b, "changed": ["narration"]}


def fake_parse_proposal_file(raw: Mapping[str, Any]) -> ProposalFile:
    if "proposals" not in raw:
        raise ProposalFileError("proposals missing")
    return ProposalFile(
        episode_id=raw.get("episode_id"),
        proposals=tuple(
            ProposeChangePayload.model_validate(p).model_dump(mode="json", exclude_none=True)
            for p in raw["proposals"]
        ),
    )


@dataclass
class FakeStarter:
    started: list[str] = field(default_factory=list)
    fail: bool = False

    async def start(self, command_id: str) -> None:
        if self.fail:
            raise RuntimeError(f"temporal unavailable token={OWNER_TOKEN}")
        self.started.append(command_id)


@dataclass
class Harness:
    client: AsyncClient
    session_factory: Any
    starter: FakeStarter
    calls: list[tuple[str, Any]]
    receive_calls: list[tuple[str, Any]]
    episode_id: str


async def _seed_episode(session_factory) -> str:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="dummy")
        await session.commit()
        return str(episode.id)


def _build_app(session_factory, registry, starter, calls, receive_calls):
    from apps.api.main import create_app
    from apps.api.routers import intervention

    async def counting_receive(session, episode_id, envelope, principal):
        receive_calls.append((envelope.command_id, principal))
        return await fake_receive_command(session, episode_id, envelope, principal)

    modules = InterventionModules(
        receive_command=counting_receive,
        make_principal=lambda actor, scopes: FakePrincipal(actor=actor, scopes=scopes),
        read_model=lambda: FakeReadModel(calls),
        build_packet=lambda view: {"episode_id": view.episode_id, "sections": ["script"]},
        render_markdown=lambda packet: (
            f"# ReviewPacket {packet['episode_id']}\n\n<script>x</script>"
        ),
        parse_proposal_file=fake_parse_proposal_file,
        command_starter=starter,
    )
    backend = StoreBackedInterventionBackend(session_factory, modules)
    app = create_app()
    app.dependency_overrides[intervention.get_principal_registry] = lambda: registry
    app.dependency_overrides[intervention.get_intervention_backend] = lambda: backend
    return app


@pytest_asyncio.fixture
async def api(session_factory):
    starter = FakeStarter()
    calls: list[tuple[str, Any]] = []
    receive_calls: list[tuple[str, Any]] = []
    app = _build_app(
        session_factory, parse_principals(_principals_json()), starter, calls, receive_calls
    )
    episode_id = await _seed_episode(session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield Harness(client, session_factory, starter, calls, receive_calls, episode_id)


def _comment(command_id: str | None = None, body: str = "ナレーションが長い") -> dict[str, Any]:
    return {
        "command_id": command_id or uuid.uuid4().hex,
        "type": "comment",
        "payload": {"anchor": {"segment_id": "s1"}, "body": body},
    }


async def _commands_in_db(session_factory, episode_id: str):
    async with session_factory() as session:
        return await InterventionStore(session).list_commands(episode_id)


def _all_route_calls(episode_id: str) -> list[tuple[str, str]]:
    rid = str(uuid.uuid4())
    return [
        ("GET", f"{PREFIX}/episodes/{episode_id}"),
        ("GET", f"{PREFIX}/episodes/{episode_id}/decisions"),
        ("GET", f"{PREFIX}/episodes/{episode_id}/revisions/compare?a={rid}&b={rid}"),
        ("GET", f"{PREFIX}/episodes/{episode_id}/review-packet"),
        ("GET", f"{PREFIX}/episodes/{episode_id}/commands"),
        ("POST", f"{PREFIX}/episodes/{episode_id}/commands"),
        ("POST", f"{PREFIX}/episodes/{episode_id}/change-proposals"),
        ("GET", f"{PREFIX}/episodes/{episode_id}/change-sets"),
        ("GET", f"{PREFIX}/episodes/{episode_id}/change-sets/{rid}"),
        ("GET", f"{PREFIX}/commands/abcdefgh12345678"),
    ]


# --------------------------------------------------------------------------- 有効化・認証


async def test_every_intervention_route_is_503_when_unconfigured(session_factory, monkeypatch):
    """既定（INTERVENTION_PRINCIPALS 未設定）で介入 API は何も開かない。token があっても 503。"""
    from apps.api.dependencies import get_settings
    from apps.api.main import create_app
    from infrastructure.config import Settings

    monkeypatch.delenv("INTERVENTION_PRINCIPALS", raising=False)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)  # type: ignore[call-arg]
    episode_id = await _seed_episode(session_factory)
    calls = _all_route_calls(episode_id)
    # router の全 route を網羅していること（route を足したらここにも足す）
    from apps.api.routers.intervention import router

    assert len(calls) == len(router.routes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, url in calls:
            response = await client.request(
                method, url, headers=_auth(OWNER_TOKEN), json=_comment()
            )
            assert response.status_code == 503, (method, url, response.text)
            assert response.json()["detail"]["error"] == "intervention_disabled"


async def test_a_misconfigured_principal_file_disables_the_api_without_leaking(
    session_factory, monkeypatch, caplog
):
    """AI に set_mode を与えた設定は読み込まない → 503。ログに hash・token を出さない。"""
    from apps.api.dependencies import get_settings
    from apps.api.main import create_app
    from infrastructure.config import Settings

    bad = json.dumps(
        {
            token_sha256(AI_TOKEN): {
                "actor_type": "ai_assistant",
                "actor_id": "chat",
                "scopes": ["read", "set_mode"],
            }
        }
    )
    caplog.set_level(logging.DEBUG)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,  # type: ignore[call-arg]
        intervention_principals=bad,  # type: ignore[arg-type]
    )
    episode_id = await _seed_episode(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"{PREFIX}/episodes/{episode_id}", headers=_auth(AI_TOKEN))
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "intervention_misconfigured"
    assert "human-only" in caplog.text
    assert token_sha256(AI_TOKEN) not in caplog.text
    assert AI_TOKEN not in caplog.text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer "}, {"Authorization": "Basic abc"}, _auth("wrong-token")],
)
async def test_missing_or_unknown_token_is_401(api: Harness, headers) -> None:
    response = await api.client.get(f"{PREFIX}/episodes/{api.episode_id}", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_reads_require_the_read_scope(session_factory) -> None:
    no_read = json.dumps(
        {
            token_sha256("commenter-token-xyz"): {
                "actor_type": "human",
                "actor_id": "c",
                "scopes": ["comment"],
            }
        }
    )
    app = _build_app(session_factory, parse_principals(no_read), FakeStarter(), [], [])
    episode_id = await _seed_episode(session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"{PREFIX}/episodes/{episode_id}", headers=_auth("commenter-token-xyz")
        )
    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error": "missing_scope",
        "required_scope": "read",
        "message": "this principal lacks the 'read' scope",
    }


# --------------------------------------------------------------------------- 読み取り


async def test_episode_view_comes_from_the_read_model(api: Harness) -> None:
    response = await api.client.get(f"{PREFIX}/episodes/{api.episode_id}", headers=_auth(AI_TOKEN))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "episode_id": api.episode_id,
        "phase": "planned",
        "control_state": "running",
    }
    assert api.calls == [("load", api.episode_id)]


async def test_unknown_episode_is_404_for_reads(api: Harness) -> None:
    missing = str(uuid.uuid4())
    for path in ("", "/decisions", "/review-packet", "/commands", "/change-sets"):
        response = await api.client.get(
            f"{PREFIX}/episodes/{missing}{path}", headers=_auth(READER_TOKEN)
        )
        assert response.status_code == 404, path
        assert response.json()["detail"]["error"] == "episode_not_found"
    assert api.calls == []


async def test_decisions_are_read_from_the_store(api: Harness) -> None:
    async with api.session_factory() as session:
        await InterventionStore(session).insert_decision(
            idempotency_key="k1",
            decision_type="topic",
            actor=Actor(ActorType.AGENT, "producer"),
            payload={
                "decision_type": "topic",
                "question": "どの題材?",
                "candidates": [{"id": "c1", "summary": "a"}],
                "rationale_kind": RationaleKind.REPORTED.value,
            },
            episode_id=api.episode_id,
        )
        await session.commit()
    response = await api.client.get(
        f"{PREFIX}/episodes/{api.episode_id}/decisions", headers=_auth(READER_TOKEN)
    )
    assert response.status_code == 200
    [decision] = response.json()["decisions"]
    assert decision["decision_type"] == "topic"
    assert decision["actor"] == {"type": "agent", "id": "producer"}
    assert decision["payload"]["candidates"][0]["id"] == "c1"


async def _seed_revision(session_factory, episode_id: str) -> str:
    async with session_factory() as session:
        digest = uuid.uuid4().hex * 2
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.DUMMY,
            schema_version="1.0",
            bucket="artifacts",
            object_key=f"artifacts/{episode_id}/dummy/{digest}.json",
            sha256=digest,
        )
        await session.commit()
        return str(meta.id)


async def test_compare_revisions_passes_both_ids_to_the_read_model(api: Harness) -> None:
    a = await _seed_revision(api.session_factory, api.episode_id)
    b = await _seed_revision(api.session_factory, api.episode_id)
    response = await api.client.get(
        f"{PREFIX}/episodes/{api.episode_id}/revisions/compare",
        params={"a": a, "b": b},
        headers=_auth(READER_TOKEN),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"a": a, "b": b, "changed": ["narration"]}


async def test_compare_rejects_a_revision_of_another_episode(api: Harness) -> None:
    other = await _seed_episode(api.session_factory)
    a = await _seed_revision(api.session_factory, api.episode_id)
    foreign = await _seed_revision(api.session_factory, other)
    response = await api.client.get(
        f"{PREFIX}/episodes/{api.episode_id}/revisions/compare",
        params={"a": a, "b": foreign},
        headers=_auth(READER_TOKEN),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "revision_not_found"
    assert not [c for c in api.calls if c[0] == "compare"]


async def test_review_packet_json_markdown_and_html(api: Harness) -> None:
    base = f"{PREFIX}/episodes/{api.episode_id}/review-packet"
    as_json = await api.client.get(base, headers=_auth(READER_TOKEN))
    assert as_json.status_code == 200
    assert as_json.json() == {"episode_id": api.episode_id, "sections": ["script"]}

    as_md = await api.client.get(base, params={"format": "markdown"}, headers=_auth(READER_TOKEN))
    assert as_md.status_code == 200
    assert as_md.headers["content-type"].startswith("text/markdown")
    assert as_md.text.startswith(f"# ReviewPacket {api.episode_id}")

    as_html = await api.client.get(base, params={"format": "html"}, headers=_auth(READER_TOKEN))
    assert as_html.status_code == 200
    assert as_html.headers["content-type"].startswith("text/html")
    # Markdown はエスケープして <pre> に入れる（packet の中身をスクリプトとして実行させない）
    assert "<script>x</script>" not in as_html.text
    assert "&lt;script&gt;x&lt;/script&gt;" in as_html.text
    assert "default-src 'none'" in as_html.headers["content-security-policy"]

    bad = await api.client.get(base, params={"format": "pdf"}, headers=_auth(READER_TOKEN))
    assert bad.status_code == 422


# --------------------------------------------------------------------------- command


async def test_a_new_command_is_202_received_and_started_after_commit(api: Harness) -> None:
    body = _comment()
    response = await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands", json=body, headers=_auth(AI_TOKEN)
    )
    assert response.status_code == 202, response.text
    out = response.json()
    assert out["command_id"] == body["command_id"]
    assert out["status"] == "received"
    assert out["applied"] is False
    assert "完了" not in out["status_text"] and "done" not in out["status_text"].lower()
    assert api.starter.started == [body["command_id"]]
    [stored] = await _commands_in_db(api.session_factory, api.episode_id)
    assert stored.command_id == body["command_id"]
    assert stored.status is CommandStatus.RECEIVED
    # 実際に使った scope と主体を記録する（AI の提案を人の操作として記録しない）
    assert stored.granted_scope is Scope.COMMENT
    assert stored.actor == Actor(ActorType.AI_ASSISTANT, "chat-assistant")
    # B の Principal には設定どおりの actor・scope を渡す（広げない）
    [(_, principal)] = api.receive_calls
    assert principal.actor == stored.actor
    assert principal.scopes == frozenset(Scope(s) for s in AI_SCOPES)


async def test_an_identical_resend_is_200_with_the_stored_status(api: Harness) -> None:
    body = _comment()
    url = f"{PREFIX}/episodes/{api.episode_id}/commands"
    first = await api.client.post(url, json=body, headers=_auth(OWNER_TOKEN))
    assert first.status_code == 202
    async with api.session_factory() as session:
        await InterventionStore(session).set_command_status(
            body["command_id"], CommandStatus.APPLIED, result={"annotation_id": "x"}
        )
        await session.commit()
    again = await api.client.post(url, json=body, headers=_auth(OWNER_TOKEN))
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "applied"
    assert again.json()["applied"] is True
    assert len(await _commands_in_db(api.session_factory, api.episode_id)) == 1
    # 終端の command は start し直さない
    assert api.starter.started == [body["command_id"]]


async def test_reusing_a_command_id_with_another_payload_is_409(api: Harness) -> None:
    url = f"{PREFIX}/episodes/{api.episode_id}/commands"
    command_id = uuid.uuid4().hex
    assert (
        await api.client.post(url, json=_comment(command_id), headers=_auth(OWNER_TOKEN))
    ).status_code == 202
    response = await api.client.post(
        url, json=_comment(command_id, body="別の本文"), headers=_auth(OWNER_TOKEN)
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "command_id_reused"
    [stored] = await _commands_in_db(api.session_factory, api.episode_id)
    assert stored.payload["body"] == "ナレーションが長い"


async def test_missing_scope_is_403_and_nothing_is_persisted(api: Harness) -> None:
    response = await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands",
        json=_comment(),
        headers=_auth(READER_TOKEN),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["required_scope"] == "comment"
    assert api.receive_calls == []
    assert await _commands_in_db(api.session_factory, api.episode_id) == []


async def test_command_for_an_unknown_episode_is_404(api: Harness) -> None:
    response = await api.client.post(
        f"{PREFIX}/episodes/{uuid.uuid4()}/commands", json=_comment(), headers=_auth(OWNER_TOKEN)
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "episode_not_found"
    assert api.starter.started == []


@pytest.mark.parametrize(
    "body",
    [
        {"command_id": "short", "type": "comment", "payload": {}},
        {"command_id": uuid.uuid4().hex, "type": "comment", "payload": {"body": "x"}},
        {"command_id": uuid.uuid4().hex, "type": "comment"},
        {
            "command_id": uuid.uuid4().hex,
            "type": "request_hold",
            "payload": {"reason": "x"},
            "unknown": 1,
        },
        {"type": "comment", "payload": {"anchor": {}, "body": "x"}},
        [],
    ],
)
async def test_invalid_envelope_or_payload_is_422_invalid_payload(api: Harness, body) -> None:
    response = await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands", json=body, headers=_auth(OWNER_TOKEN)
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "invalid_payload"
    assert api.receive_calls == []


@pytest.mark.parametrize(
    "command_type",
    [
        "set_budget",
        "set_episode_budget",
        "raise_monthly_budget",
        "update_schedule",
        "pause_schedule",
        "set_visibility",
        "set_privacy_status",
        "publish",
        "make_public",
        "",
        None,
    ],
)
async def test_budget_schedule_and_visibility_commands_are_422_unsupported(
    api: Harness, command_type
) -> None:
    """INV-47: 予算・Schedule・公開設定を変える command 型は受け付けない（人の token でも）。"""
    response = await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands",
        json={"command_id": uuid.uuid4().hex, "type": command_type, "payload": {"value": 1}},
        headers=_auth(OWNER_TOKEN),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "unsupported_command"
    assert api.receive_calls == []
    assert await _commands_in_db(api.session_factory, api.episode_id) == []


@pytest.mark.parametrize(
    ("command_type", "payload"),
    [
        ("set_mode", {"mode": "co_production", "episode_budget_jpy": 99999}),
        ("apply_change", {"change_set_id": str(uuid.uuid4()), "privacy_status": "public"}),
        ("resume", {"start_stage": None, "schedule_cron": "* * * * *"}),
        ("release_checkpoint", {"point": "before_upload", "manifest_id": "m", "visibility": "x"}),
    ],
)
async def test_budget_schedule_visibility_fields_cannot_ride_on_other_commands(
    api: Harness, command_type: str, payload: dict[str, Any]
) -> None:
    """INV-47: 既存の型の payload に予算・Schedule・公開設定を混ぜても 422（extra=forbid）。"""
    response = await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands",
        json={"command_id": uuid.uuid4().hex, "type": command_type, "payload": payload},
        headers=_auth(OWNER_TOKEN),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "invalid_payload"
    assert api.receive_calls == []


async def test_an_ai_principal_cannot_set_mode(api: Harness) -> None:
    """INV-47: AI は人専用 scope を持てないので、set_mode は 403。人は受け付けられる。"""
    body = {
        "command_id": uuid.uuid4().hex,
        "type": "set_mode",
        "payload": {"mode": "co_production"},
    }
    url = f"{PREFIX}/episodes/{api.episode_id}/commands"
    denied = await api.client.post(url, json=body, headers=_auth(AI_TOKEN))
    assert denied.status_code == 403
    assert denied.json()["detail"]["required_scope"] == "set_mode"
    assert api.receive_calls == []
    allowed = await api.client.post(url, json=body, headers=_auth(OWNER_TOKEN))
    assert allowed.status_code == 202
    [stored] = await _commands_in_db(api.session_factory, api.episode_id)
    assert stored.actor.type is ActorType.HUMAN
    assert stored.granted_scope is Scope.SET_MODE


async def test_a_failed_workflow_start_still_returns_202_and_logs_no_secret(
    api: Harness, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    api.starter.fail = True
    body = _comment()
    response = await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands", json=body, headers=_auth(OWNER_TOKEN)
    )
    assert response.status_code == 202
    assert response.json()["status"] == "received"
    assert "redelivery" in caplog.text
    assert body["command_id"] in caplog.text
    # 例外の文言（token を含みうる）はログに出さない。型名だけ
    assert OWNER_TOKEN not in caplog.text


async def test_command_status_distinguishes_received_from_applied(api: Harness) -> None:
    body = _comment()
    await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands", json=body, headers=_auth(OWNER_TOKEN)
    )
    url = f"{PREFIX}/commands/{body['command_id']}"
    received = (await api.client.get(url, headers=_auth(READER_TOKEN))).json()
    assert received["status"] == "received"
    assert received["applied"] is False and received["terminal"] is False
    assert received["granted_scope"] == "comment"
    assert received["actor"] == {"type": "human", "id": "owner"}
    async with api.session_factory() as session:
        await InterventionStore(session).set_command_status(
            body["command_id"], CommandStatus.APPLIED, result={"annotation_id": "a1"}
        )
        await session.commit()
    applied = (await api.client.get(url, headers=_auth(READER_TOKEN))).json()
    assert applied["status"] == "applied"
    assert applied["applied"] is True and applied["terminal"] is True
    assert applied["result"] == {"annotation_id": "a1"}
    missing = await api.client.get(
        f"{PREFIX}/commands/{uuid.uuid4().hex}", headers=_auth(OWNER_TOKEN)
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"] == "command_not_found"


def test_status_text_never_says_done_unless_applied() -> None:
    from apps.api.routers.intervention import STATUS_TEXT

    assert set(STATUS_TEXT) == set(CommandStatus)
    for status, text in STATUS_TEXT.items():
        if status is CommandStatus.APPLIED:
            continue
        assert "完了" not in text and "done" not in text.lower(), status
        assert "未適用" in text or "適用されていない" in text or "適用しない" in text, status


async def test_episode_commands_are_listed(api: Harness) -> None:
    body = _comment()
    await api.client.post(
        f"{PREFIX}/episodes/{api.episode_id}/commands", json=body, headers=_auth(OWNER_TOKEN)
    )
    response = await api.client.get(
        f"{PREFIX}/episodes/{api.episode_id}/commands", headers=_auth(READER_TOKEN)
    )
    assert [c["command_id"] for c in response.json()["commands"]] == [body["command_id"]]


# --------------------------------------------------------------------------- 変更案・ChangeSet


def _proposal(narration: str) -> dict[str, Any]:
    return {
        "change": {
            "kind": "script.edit_scenes",
            "base_revision_id": "00000000-0000-0000-0000-000000000001",
            "edits": [{"segment_id": "s1", "narration": narration}],
        },
        "rationale": "短くする",
    }


async def test_importing_a_change_proposal_file_creates_deterministic_commands(
    api: Harness,
) -> None:
    url = f"{PREFIX}/episodes/{api.episode_id}/change-proposals"
    file = {"episode_id": api.episode_id, "proposals": [_proposal("a"), _proposal("b")]}
    first = await api.client.post(url, json=file, headers=_auth(AI_TOKEN))
    assert first.status_code == 202, first.text
    ids = [c["command_id"] for c in first.json()["commands"]]
    assert len(set(ids)) == 2
    assert all(c["status"] == "received" and c["created"] for c in first.json()["commands"])
    assert sorted(api.starter.started) == sorted(ids)
    stored = await _commands_in_db(api.session_factory, api.episode_id)
    assert {c.type for c in stored} == {CommandType.PROPOSE_CHANGE}
    assert {c.granted_scope for c in stored} == {Scope.PROPOSE}

    # 同じファイルの再取り込みは新しい command を作らない（200・同じ id）
    again = await api.client.post(url, json=file, headers=_auth(AI_TOKEN))
    assert again.status_code == 200, again.text
    assert [c["command_id"] for c in again.json()["commands"]] == ids
    assert not any(c["created"] for c in again.json()["commands"])
    assert len(await _commands_in_db(api.session_factory, api.episode_id)) == 2

    # 同じ内容の案が2つあっても別 command（順番を入れ替えても id は案の内容で決まる）
    swapped = {"episode_id": api.episode_id, "proposals": [_proposal("b"), _proposal("a")]}
    reordered = await api.client.post(url, json=swapped, headers=_auth(AI_TOKEN))
    assert sorted(c["command_id"] for c in reordered.json()["commands"]) == sorted(ids)


async def test_duplicate_proposals_in_one_file_are_distinct_commands(api: Harness) -> None:
    url = f"{PREFIX}/episodes/{api.episode_id}/change-proposals"
    file = {"proposals": [_proposal("same"), _proposal("same")]}
    response = await api.client.post(url, json=file, headers=_auth(OWNER_TOKEN))
    assert response.status_code == 202
    assert len({c["command_id"] for c in response.json()["commands"]}) == 2


async def test_change_proposal_import_checks_scope_episode_and_shape(api: Harness) -> None:
    url = f"{PREFIX}/episodes/{api.episode_id}/change-proposals"
    file = {"episode_id": api.episode_id, "proposals": [_proposal("a")]}
    assert (await api.client.post(url, json=file, headers=_auth(READER_TOKEN))).status_code == 403
    mismatch = {"episode_id": str(uuid.uuid4()), "proposals": [_proposal("a")]}
    response = await api.client.post(url, json=mismatch, headers=_auth(OWNER_TOKEN))
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "episode_mismatch"
    broken = await api.client.post(url, json={"nope": 1}, headers=_auth(OWNER_TOKEN))
    assert broken.status_code == 422
    assert broken.json()["detail"]["error"] == "invalid_payload"
    empty = await api.client.post(url, json={"proposals": []}, headers=_auth(OWNER_TOKEN))
    assert empty.status_code == 422
    assert api.receive_calls == []


async def _seed_change_set(api: Harness, *, cost: Decimal | None, episode_id: str | None = None):
    async with api.session_factory() as session:
        change_set = await InterventionStore(session).insert_change_set(
            episode_id=episode_id or api.episode_id,
            kind=ChangeKind.SCRIPT_EDIT_SCENES,
            base_revision_id=uuid.uuid4(),
            proposal=_proposal("x")["change"],
            actor=Actor(ActorType.AI_ASSISTANT, "chat-assistant"),
            proposed_by_command_id=uuid.uuid4().hex,
            impact={"stale": ["voice", "render"], "regenerate": ["voice"], "reuse": ["image"]},
            estimated_cost_usd=cost,
            rationale="短く",
        )
        await session.commit()
        return change_set


async def test_change_set_view_shows_diff_impact_and_cost(api: Harness) -> None:
    change_set = await _seed_change_set(api, cost=Decimal("0.25"))
    response = await api.client.get(
        f"{PREFIX}/episodes/{api.episode_id}/change-sets/{change_set.change_set_id}",
        headers=_auth(READER_TOKEN),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposal"]["edits"][0]["narration"] == "x"
    assert body["impact"]["stale"] == ["voice", "render"]
    assert body["estimated_cost_usd"] == pytest.approx(0.25)
    assert body["cost_estimated"] is True
    assert body["actor"] == {"type": "ai_assistant", "id": "chat-assistant"}
    listed = await api.client.get(
        f"{PREFIX}/episodes/{api.episode_id}/change-sets", headers=_auth(READER_TOKEN)
    )
    assert [c["change_set_id"] for c in listed.json()["change_sets"]] == [change_set.change_set_id]


async def test_an_unestimated_cost_is_not_shown_as_zero(api: Harness) -> None:
    change_set = await _seed_change_set(api, cost=None)
    body = (
        await api.client.get(
            f"{PREFIX}/episodes/{api.episode_id}/change-sets/{change_set.change_set_id}",
            headers=_auth(READER_TOKEN),
        )
    ).json()
    assert body["estimated_cost_usd"] is None
    assert body["cost_estimated"] is False


async def test_a_change_set_of_another_episode_is_404(api: Harness) -> None:
    other = await _seed_episode(api.session_factory)
    change_set = await _seed_change_set(api, cost=None, episode_id=other)
    response = await api.client.get(
        f"{PREFIX}/episodes/{api.episode_id}/change-sets/{change_set.change_set_id}",
        headers=_auth(READER_TOKEN),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "change_set_not_found"


# --------------------------------------------------------------------------- INV-47 の表面・secret


def test_the_router_exposes_no_budget_schedule_or_visibility_surface() -> None:
    """INV-47: 介入 API の route は読み取り（GET）と command の受付（POST 2つ）だけ。"""
    from fastapi.routing import APIRoute

    from apps.api.routers.intervention import router

    writes: list[str] = []
    for route in router.routes:
        assert isinstance(route, APIRoute)
        assert route.path.startswith(PREFIX)
        methods = route.methods or set()
        assert methods <= {"GET", "POST"}, route.path
        lowered = route.path.lower()
        for word in ("budget", "schedule", "visibility", "privacy", "publish", "settings"):
            assert word not in lowered, route.path
        if "POST" in methods:
            writes.append(route.path)
    assert sorted(writes) == sorted(
        [
            f"{PREFIX}/episodes/{{episode_id}}/commands",
            f"{PREFIX}/episodes/{{episode_id}}/change-proposals",
        ]
    )


async def test_tokens_never_reach_the_logs(api: Harness, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    url = f"{PREFIX}/episodes/{api.episode_id}"
    await api.client.get(url, headers=_auth(READER_TOKEN))
    await api.client.get(url, headers=_auth("not-a-real-token-123"))
    await api.client.post(f"{url}/commands", json=_comment(), headers=_auth(OWNER_TOKEN))
    await api.client.post(f"{url}/commands", json=_comment(), headers=_auth(READER_TOKEN))
    await api.client.post(
        f"{url}/commands",
        json={"command_id": uuid.uuid4().hex, "type": "set_budget"},
        headers=_auth(AI_TOKEN),
    )
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    for token in (*ALL_TOKENS, "not-a-real-token-123"):
        assert token not in text
        assert token_sha256(token) not in text
