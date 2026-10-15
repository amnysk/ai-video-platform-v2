"""介入 API の裏側（``apps/api/intervention_backend.py``）の失敗の写し方。

store と SQLite は本物、担当 A / B は fake。理由は ``docs/testing/intervention-c-rationale.md``。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.auth import parse_principals
from apps.api.intervention_backend import (
    InterventionModules,
    StoreBackedInterventionBackend,
    TemporalCommandStarter,
)
from infrastructure.config import Settings
from infrastructure.db.repositories import EpisodeRepository
from tests.unit.test_intervention_api import (
    AI_TOKEN,
    OWNER_TOKEN,
    PREFIX,
    FakePrincipal,
    FakeReadModel,
    FakeStarter,
    _auth,
    _commands_in_db,
    _comment,
    _principals_json,
    _proposal,
    _seed_episode,
    _seed_revision,
    fake_parse_proposal_file,
    fake_receive_command,
)

SECRET = "sk-super-secret-value"


class RejectError(Exception):
    """B の ``CommandReceiveError`` の代役（``code`` を持つ）。"""

    def __init__(self, code: str) -> None:
        super().__init__(f"rejected {code} {SECRET}")
        self.code = code


def _modules(**overrides: Any) -> InterventionModules:
    base = InterventionModules(
        receive_command=fake_receive_command,
        make_principal=lambda actor, scopes: FakePrincipal(actor=actor, scopes=scopes),
        read_model=lambda: FakeReadModel([]),
        build_packet=lambda view: {"episode_id": view.episode_id},
        render_markdown=lambda packet: "# packet",
        parse_proposal_file=fake_parse_proposal_file,
        command_starter=FakeStarter(),
        receive_error_types=(RejectError,),
    )
    return replace(base, **overrides)


def _client(session_factory, modules: Any, **backend_kwargs: Any) -> AsyncClient:
    from apps.api.main import create_app
    from apps.api.routers import intervention

    backend = StoreBackedInterventionBackend(session_factory, modules, **backend_kwargs)
    app = create_app()
    registry = parse_principals(_principals_json())
    app.dependency_overrides[intervention.get_principal_registry] = lambda: registry
    app.dependency_overrides[intervention.get_intervention_backend] = lambda: backend
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------- 503


async def test_unavailable_modules_are_503_and_store_only_routes_keep_working(
    session_factory, caplog
) -> None:
    def broken() -> InterventionModules:
        raise ImportError(f"cannot import {SECRET}")

    episode_id = await _seed_episode(session_factory)
    caplog.set_level(logging.DEBUG)
    async with _client(session_factory, broken) as client:
        for method, url in (
            ("GET", f"{PREFIX}/episodes/{episode_id}"),
            ("GET", f"{PREFIX}/episodes/{episode_id}/review-packet"),
            ("POST", f"{PREFIX}/episodes/{episode_id}/commands"),
        ):
            r = await client.request(method, url, headers=_auth(OWNER_TOKEN), json=_comment())
            assert r.status_code == 503, (url, r.text)
            assert r.json()["detail"]["error"] == "intervention_unavailable"
            assert SECRET not in r.text
        ok = await client.get(
            f"{PREFIX}/episodes/{episode_id}/decisions", headers=_auth(OWNER_TOKEN)
        )
        assert ok.status_code == 200
    assert await _commands_in_db(session_factory, episode_id) == []
    assert SECRET not in caplog.text


async def test_unavailable_modules_are_retried_on_the_next_request(session_factory) -> None:
    attempts: list[int] = []

    def flaky() -> InterventionModules:
        attempts.append(1)
        if len(attempts) == 1:
            raise ImportError("not yet")
        return _modules()

    episode_id = await _seed_episode(session_factory)
    async with _client(session_factory, flaky) as client:
        first = await client.get(f"{PREFIX}/episodes/{episode_id}", headers=_auth(OWNER_TOKEN))
        second = await client.get(f"{PREFIX}/episodes/{episode_id}", headers=_auth(OWNER_TOKEN))
    assert (first.status_code, second.status_code) == (503, 200)


# --------------------------------------------------------------------------- 受付拒否


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("stale_control_revision", 409),
        ("stale_target_revision", 409),
        ("invalid_state", 409),
        ("forbidden", 403),
        ("invalid_payload", 422),
        ("unsupported_command", 422),
        ("unsupported_change", 422),
        ("user_constraint_violation", 422),
        ("budget_exceeded", 422),
        ("budget_unverifiable", 422),
    ],
)
async def test_a_receive_rejection_maps_to_a_machine_readable_error(
    session_factory, code: str, status: int
) -> None:
    async def reject(session, episode_id, envelope, principal):
        raise RejectError(code)

    episode_id = await _seed_episode(session_factory)
    async with _client(session_factory, _modules(receive_command=reject)) as client:
        r = await client.post(
            f"{PREFIX}/episodes/{episode_id}/commands", headers=_auth(OWNER_TOKEN), json=_comment()
        )
    assert r.status_code == status, r.text
    assert r.json()["detail"]["error"] == code
    assert SECRET not in r.text  # 例外文・入力値を返さない
    assert await _commands_in_db(session_factory, episode_id) == []


async def test_an_unknown_rejection_code_is_422_command_rejected(session_factory) -> None:
    async def reject(session, episode_id, envelope, principal):
        raise RejectError("something_new")

    episode_id = await _seed_episode(session_factory)
    async with _client(session_factory, _modules(receive_command=reject)) as client:
        r = await client.post(
            f"{PREFIX}/episodes/{episode_id}/commands", headers=_auth(OWNER_TOKEN), json=_comment()
        )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "command_rejected"
    assert "something_new" not in r.text


async def test_a_rejection_in_an_import_rolls_back_every_proposal(session_factory) -> None:
    """取り込みは全部か無し: 2件目の拒否で1件目の書き込みも残らない。

    store の ``insert_command`` は SAVEPOINT を使い、SQLite（pysqlite）では最外の RELEASE が
    commit になってしまうため、ここでは SAVEPOINT を使わない書き込み（Episode 行）で見る。
    """
    from sqlalchemy import func, select

    from infrastructure.db.models import EpisodeRow

    calls: list[str] = []

    async def second_rejects(session, episode_id, envelope, principal):
        calls.append(envelope.command_id)
        if len(calls) == 2:
            raise RejectError("invalid_state")
        await EpisodeRepository(session).create(topic="written-by-first-proposal")
        return await fake_receive_command(session, episode_id, envelope, principal)

    async def episodes() -> int:
        async with session_factory() as session:
            return int(
                (await session.execute(select(func.count()).select_from(EpisodeRow))).scalar()
            )

    episode_id = await _seed_episode(session_factory)
    before = await episodes()
    body = {"proposals": [_proposal("a"), _proposal("b")]}
    async with _client(session_factory, _modules(receive_command=second_rejects)) as client:
        r = await client.post(
            f"{PREFIX}/episodes/{episode_id}/change-proposals",
            headers=_auth(OWNER_TOKEN),
            json=body,
        )
    assert r.status_code == 409, r.text
    assert len(calls) == 2
    assert await episodes() == before


# --------------------------------------------------------------------------- 404 / 422 / 409


async def test_a_read_model_without_the_episode_is_404(session_factory) -> None:
    class Missing:
        def load(self, episode_id: str) -> None:
            return None

    episode_id = await _seed_episode(session_factory)
    async with _client(session_factory, _modules(read_model=lambda: Missing())) as client:
        for suffix in ("", "/review-packet", "/review-packet?format=markdown"):
            r = await client.get(
                f"{PREFIX}/episodes/{episode_id}{suffix}", headers=_auth(OWNER_TOKEN)
            )
            assert r.status_code == 404, (suffix, r.text)
            assert r.json()["detail"]["error"] == "episode_not_found"


async def test_receive_not_found_from_b_is_404(session_factory) -> None:
    async def not_found(session, episode_id, envelope, principal):
        raise RejectError("not_found")

    episode_id = await _seed_episode(session_factory)
    async with _client(session_factory, _modules(receive_command=not_found)) as client:
        r = await client.post(
            f"{PREFIX}/episodes/{episode_id}/commands", headers=_auth(OWNER_TOKEN), json=_comment()
        )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "episode_not_found"


async def test_comparing_incompatible_revisions_is_422(session_factory) -> None:
    class Incompatible:
        def compare_revisions(self, a: str, b: str) -> None:
            raise ValueError(f"cannot compare script with audio {SECRET}")

    episode_id = await _seed_episode(session_factory)
    a = await _seed_revision(session_factory, episode_id)
    b = await _seed_revision(session_factory, episode_id)
    async with _client(session_factory, _modules(read_model=lambda: Incompatible())) as client:
        r = await client.get(
            f"{PREFIX}/episodes/{episode_id}/revisions/compare",
            params={"a": a, "b": b},
            headers=_auth(OWNER_TOKEN),
        )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "revision_type_mismatch"
    assert SECRET not in r.text


async def test_resending_a_command_id_as_another_actor_is_409_without_naming_the_owner(
    session_factory,
) -> None:
    """同じ id・同じ内容でも、保存済みの主体が違えば再送として受け取らない。"""
    episode_id = await _seed_episode(session_factory)
    body = _comment(uuid.uuid4().hex)
    async with _client(session_factory, _modules()) as client:
        first = await client.post(
            f"{PREFIX}/episodes/{episode_id}/commands", headers=_auth(OWNER_TOKEN), json=body
        )
        assert first.status_code == 202
        other = await client.post(
            f"{PREFIX}/episodes/{episode_id}/commands", headers=_auth(AI_TOKEN), json=body
        )
        same = await client.post(
            f"{PREFIX}/episodes/{episode_id}/commands", headers=_auth(OWNER_TOKEN), json=body
        )
    assert other.status_code == 409
    assert other.json()["detail"]["error"] == "command_id_reused"
    assert "owner" not in other.text and "human" not in other.text
    assert same.status_code == 200  # 本人の再送は今まで通り
    assert len(await _commands_in_db(session_factory, episode_id)) == 1


# --------------------------------------------------------------------------- best-effort の start


@dataclass
class SlowStarter:
    async def start(self, command_id: str) -> None:
        await asyncio.sleep(30)


async def test_a_workflow_start_timeout_still_returns_202_received(session_factory, caplog) -> None:
    episode_id = await _seed_episode(session_factory)
    caplog.set_level(logging.DEBUG)
    modules = _modules(command_starter=SlowStarter())
    async with _client(session_factory, modules, start_timeout_seconds=0.05) as client:
        r = await asyncio.wait_for(
            client.post(
                f"{PREFIX}/episodes/{episode_id}/commands",
                headers=_auth(OWNER_TOKEN),
                json=_comment(),
            ),
            timeout=5,
        )
    assert r.status_code == 202
    assert r.json()["status"] == "received"
    rows = await _commands_in_db(session_factory, episode_id)
    assert len(rows) == 1 and rows[0].dispatched_at is None  # 再配送 Schedule が拾う
    assert "redelivery" in caplog.text


# --- Temporal client の使い回し


class _FakeClient:
    def __init__(self, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started: list[str] = []

    async def start_workflow(self, name: str, arg: Any, **kwargs: Any) -> None:
        if self.fail_start:
            raise RuntimeError("connection lost")
        self.started.append(kwargs["id"])


def _patch_connect(monkeypatch, make: Callable[[], _FakeClient]) -> list[_FakeClient]:
    from temporalio.client import Client

    made: list[_FakeClient] = []

    async def connect(*args: Any, **kwargs: Any) -> _FakeClient:
        client = make()
        made.append(client)
        return client

    monkeypatch.setattr(Client, "connect", staticmethod(connect))
    return made


def _starter(session_factory) -> TemporalCommandStarter:
    return TemporalCommandStarter(Settings(_env_file=None), session_factory)  # type: ignore[call-arg]


async def test_the_temporal_client_is_reused_within_a_loop(session_factory, monkeypatch) -> None:
    made = _patch_connect(monkeypatch, _FakeClient)
    starter = _starter(session_factory)
    await starter.start("cmd-00000001")
    await starter.start("cmd-00000002")
    assert len(made) == 1 and len(made[0].started) == 2


async def test_a_failed_start_drops_the_cached_client(session_factory, monkeypatch) -> None:
    clients = iter([_FakeClient(fail_start=True), _FakeClient()])
    made = _patch_connect(monkeypatch, lambda: next(clients))
    starter = _starter(session_factory)
    with pytest.raises(RuntimeError):
        await starter.start("cmd-00000001")
    await starter.start("cmd-00000001")  # つなぎ直して成功する
    assert len(made) == 2


def test_the_temporal_client_does_not_leak_across_event_loops(session_factory, monkeypatch) -> None:
    """別の event loop（テストごと・再起動後）に前の loop の client を渡さない。"""
    made = _patch_connect(monkeypatch, _FakeClient)
    starter = _starter(session_factory)
    asyncio.run(starter.start("cmd-00000001"))
    asyncio.run(starter.start("cmd-00000002"))
    assert len(made) == 2
