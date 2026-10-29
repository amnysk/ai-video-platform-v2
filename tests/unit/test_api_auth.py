"""API の変更系ルートの認証（ADR-0080 §B / INV-124・125）。

2026-10-29 の監査: ``POST /episodes/{id}/upload`` ほか全ての変更系ルートが無認証のまま
``0.0.0.0:8000`` に公開されていた。
このファイルは実際の認証を使う（``real_api_auth``。他の API テストは所有者として認証済みで走る）。
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from apps.api.auth import (
    Actor,
    ApiTokens,
    authenticate,
    load_api_tokens,
)
from contracts.quality import QUALITY_GATE_ENFORCE_ENV, QualityVerdict
from infrastructure.config import Settings
from tests.unit.test_quality_gate_wiring import (
    NOW,
    _check,
    _provider,
    _render_ready_with_owner,
)

pytestmark = pytest.mark.real_api_auth

OWNER_TOKEN = "o" * 40
AGENT_TOKEN = "a" * 40
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class _Starter:
    """ワークフロー起動の偽物。1つでも呼ばれたら副作用が起きた印。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        async def record(*args: Any, **kwargs: Any) -> str:
            self.calls.append((name, args, kwargs))
            return "wf"

        return record


def _write_tokens(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "tokens"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest_asyncio.fixture
async def secured(session_factory, tmp_path: Path):
    from apps.api.auth import get_api_tokens
    from apps.api.dependencies import (
        get_quality_clock,
        get_quality_providers,
        get_session_factory,
        get_settings,
        get_workflow_starter,
    )
    from apps.api.main import create_app

    state: dict[str, Any] = {
        "tokens": ApiTokens.from_pairs([(Actor.OWNER, OWNER_TOKEN), (Actor.AGENT, AGENT_TOKEN)]),
        "providers": [],
        "enforce": True,
    }
    starter = _Starter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    app.dependency_overrides[get_api_tokens] = lambda: state["tokens"]
    app.dependency_overrides[get_quality_providers] = lambda: state["providers"]
    app.dependency_overrides[get_quality_clock] = lambda: lambda: NOW
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,  # type: ignore[call-arg]
        **{QUALITY_GATE_ENFORCE_ENV: state["enforce"]},  # type: ignore[arg-type]
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield app, client, starter, state


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mutating_routes(app: Any) -> list[tuple[str, str]]:
    """OpenAPI から全ルートを列挙する（include された router も含む。ルート表の型に依存しない）。"""
    found = []
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            if method.upper() in MUTATING:
                found.append((method.upper(), path))
    return sorted(found)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid.uuid4()), path)


# ---- INV-124: 全ての変更系ルートは認証を要求し、未設定なら閉じる


async def test_there_are_mutating_routes_to_protect(secured) -> None:
    app, *_ = secured
    paths = {path for _m, path in _mutating_routes(app)}
    assert {"/episodes", "/episodes/{episode_id}/upload", "/episodes/{episode_id}/resume"} <= paths
    assert len(_mutating_routes(app)) >= 8


async def test_every_mutating_route_refuses_when_no_token_is_configured(secured) -> None:
    app, client, starter, state = secured
    state["tokens"] = ApiTokens.from_pairs([])
    for method, path in _mutating_routes(app):
        response = await client.request(method, _concrete(path), json={})
        assert response.status_code == 503, (method, path, response.text)
    assert starter.calls == []  # 何も起動していない


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Basic {OWNER_TOKEN}"},
        {"Authorization": OWNER_TOKEN},
        {"Authorization": "Bearer " + "x" * 40},
        {"Authorization": f"Bearer {OWNER_TOKEN} "},
        {"Authorization": f"bearer {OWNER_TOKEN[:-1]}"},
    ],
)
async def test_every_mutating_route_refuses_a_missing_or_wrong_credential(
    secured, headers: dict[str, str]
) -> None:
    app, client, starter, _state = secured
    for method, path in _mutating_routes(app):
        response = await client.request(method, _concrete(path), json={}, headers=headers)
        assert response.status_code == 401, (method, path, headers, response.text)
        assert response.headers["www-authenticate"].lower().startswith("bearer")
    assert starter.calls == []


async def test_a_valid_token_reaches_the_handler(secured) -> None:
    _app, client, starter, _state = secured
    response = await client.post("/episodes", json={"topic": "x"}, headers=_bearer(OWNER_TOKEN))
    assert response.status_code == 202, response.text
    assert len(starter.calls) == 1
    agent = await client.post("/episodes", json={"topic": "y"}, headers=_bearer(AGENT_TOKEN))
    assert agent.status_code == 202


async def test_read_only_routes_stay_open(secured) -> None:
    _app, client, starter, state = secured
    for tokens in (state["tokens"], ApiTokens.from_pairs([])):
        state["tokens"] = tokens
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get(f"/episodes/{uuid.uuid4()}")).status_code == 404
    assert starter.calls == []


async def test_the_credential_never_appears_in_a_refusal(secured) -> None:
    _app, client, _starter, _state = secured
    response = await client.post("/episodes", json={}, headers=_bearer("z" * 40))
    assert "z" * 40 not in response.text and OWNER_TOKEN not in response.text


# ---- トークンの読み込みと比較


def test_tokens_load_from_a_private_file(tmp_path: Path) -> None:
    path = _write_tokens(tmp_path, f"# c\nowner:{OWNER_TOKEN}\n\nagent:{AGENT_TOKEN}\n")
    tokens = load_api_tokens(str(path))
    assert authenticate(f"Bearer {OWNER_TOKEN}", tokens) is Actor.OWNER
    assert authenticate(f"Bearer {AGENT_TOKEN}", tokens) is Actor.AGENT
    assert authenticate("Bearer nope", tokens) is None
    assert authenticate(None, tokens) is None


@pytest.mark.parametrize(
    "content",
    [
        "",
        "owner:short",  # 短すぎる
        f"root:{OWNER_TOKEN}",  # 未知の actor
        f"owner:{OWNER_TOKEN}\nagent:{OWNER_TOKEN}",  # 同じ token を2つの actor に
        f"owner {OWNER_TOKEN}",
        f"owner:{OWNER_TOKEN}\nbroken",
    ],
)
def test_a_missing_or_malformed_token_file_means_nothing_is_authenticated(
    tmp_path: Path, content: str
) -> None:
    tokens = load_api_tokens(str(_write_tokens(tmp_path, content)))
    assert tokens.is_empty()
    assert authenticate(f"Bearer {OWNER_TOKEN}", tokens) is None


def test_an_unset_or_absent_path_means_not_configured(tmp_path: Path) -> None:
    assert load_api_tokens(None).is_empty()
    assert load_api_tokens("").is_empty()
    assert load_api_tokens(str(tmp_path / "missing")).is_empty()


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o604])
def test_a_token_file_readable_by_others_is_refused(tmp_path: Path, mode: int) -> None:
    path = _write_tokens(tmp_path, f"owner:{OWNER_TOKEN}\n", mode=mode)
    assert load_api_tokens(str(path)).is_empty()


def test_comparison_is_constant_time_and_checks_every_entry(monkeypatch) -> None:
    import apps.api.auth as auth

    seen: list[int] = []
    real = auth.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append(1)
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    tokens = ApiTokens.from_pairs([(Actor.OWNER, OWNER_TOKEN), (Actor.AGENT, AGENT_TOKEN)])
    assert authenticate(f"Bearer {OWNER_TOKEN}", tokens) is Actor.OWNER
    assert len(seen) == 2  # 先頭で一致しても残りも比較する（打ち切りで長さ・順序を漏らさない）


# ---- INV-125: 手動投稿は品質ゲートを迂回できず、REVIEW の承認は所有者トークンだけ


async def test_manual_upload_cannot_bypass_a_block_with_any_token(secured, session_factory) -> None:
    _app, client, starter, state = secured
    state["providers"] = [_provider(_check_block())]
    ep = await _render_ready_with_owner(session_factory)
    for token in (OWNER_TOKEN, AGENT_TOKEN):
        res = await client.post(f"/episodes/{ep}/upload", headers=_bearer(token))
        assert res.status_code == 409 and res.json()["detail"] == "quality: report_blocked"
    assert starter.calls == []


async def test_review_is_approved_by_the_owner_token_but_never_by_the_agent_token(
    secured, session_factory, caplog
) -> None:
    _app, client, starter, state = secured
    state["providers"] = []  # 検査なし = REVIEW
    ep = await _render_ready_with_owner(session_factory)
    refused = await client.post(f"/episodes/{ep}/upload", headers=_bearer(AGENT_TOKEN))
    assert refused.status_code == 409
    assert refused.json()["detail"] == "quality: review_requires_owner"
    assert starter.calls == []
    with caplog.at_level(logging.INFO):
        allowed = await client.post(f"/episodes/{ep}/upload", headers=_bearer(OWNER_TOKEN))
    assert allowed.status_code == 202, allowed.text
    text = repr([r.__dict__ for r in caplog.records])
    assert "'review_approved': True" in text and "'actor': 'owner'" in text


async def test_a_passing_report_lets_the_agent_token_upload(secured, session_factory) -> None:
    _app, client, starter, state = secured
    state["providers"] = [_provider(_check(QualityVerdict.PASS))]
    ep = await _render_ready_with_owner(session_factory)
    res = await client.post(f"/episodes/{ep}/upload", headers=_bearer(AGENT_TOKEN))
    assert res.status_code == 202 and len(starter.calls) == 1


async def test_every_authenticated_mutation_logs_its_actor_without_the_token(
    secured, caplog
) -> None:
    _app, client, _starter, _state = secured
    with caplog.at_level(logging.INFO):
        await client.post("/episodes", json={"topic": "x"}, headers=_bearer(AGENT_TOKEN))
    text = repr([r.__dict__ for r in caplog.records])
    assert "'actor': 'agent'" in text
    assert AGENT_TOKEN not in text and OWNER_TOKEN not in text


def _check_block() -> Any:
    return _check(QualityVerdict.BLOCK)


# ---- 呼び出し元（ADR-0080 §B: 認証なしで API を叩いていた運用スクリプトを同じ枝で更新する）


def test_every_operator_script_that_posts_to_the_api_sends_a_token() -> None:
    root = Path(__file__).resolve().parents[2]
    posting = [
        (path, line)
        for path in sorted((root / "scripts").glob("*.sh"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "-X POST" in line and "$API" in line
    ]
    assert len(posting) >= 8  # 走査が空振りしていない
    missing = [
        f"{p.name}: {line.strip()}" for p, line in posting if "Authorization: Bearer" not in line
    ]
    assert not missing, missing


async def test_the_resume_client_sends_the_token_and_never_logs_it() -> None:
    import httpx

    from infrastructure.production.provider_credits import ApiResumeClient

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(202, json={"workflow_id": "w:1", "target_stage": "production"})

    transport = httpx.MockTransport(handler)
    await ApiResumeClient("http://api.test", token=OWNER_TOKEN, transport=transport).resume("e")
    await ApiResumeClient("http://api.test", transport=transport).resume("e")
    assert seen == [f"Bearer {OWNER_TOKEN}", None]
