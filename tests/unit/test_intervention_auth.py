"""介入 API の principal 設定（JP-MAS-001 / ADR-0041 §8、INV-47）。

理由は ``docs/testing/intervention-c-rationale.md``。
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

from apps.api.auth import (
    PrincipalConfigError,
    load_principals,
    parse_principals,
    token_sha256,
)
from contracts.intervention import HUMAN_ONLY_SCOPES, ActorType, Scope

HUMAN_TOKEN = "human-token-value-0123456789"
AI_TOKEN = "ai-assistant-token-value-abcdef"


def _entry(actor_type: str, actor_id: str, scopes: list[str]) -> dict[str, object]:
    return {"actor_type": actor_type, "actor_id": actor_id, "scopes": scopes}


def _config() -> dict[str, object]:
    return {
        token_sha256(HUMAN_TOKEN): _entry("human", "owner", ["read", "comment", "set_mode"]),
        token_sha256(AI_TOKEN): _entry("ai_assistant", "chat", ["read", "comment", "propose"]),
    }


def test_token_hash_is_plain_sha256_hex() -> None:
    assert token_sha256("abc") == hashlib.sha256(b"abc").hexdigest()


def test_authenticate_maps_a_token_to_its_principal() -> None:
    registry = parse_principals(json.dumps(_config()))
    human = registry.authenticate(HUMAN_TOKEN)
    assert human is not None
    assert human.actor_type is ActorType.HUMAN
    assert human.actor_id == "owner"
    assert human.scopes == frozenset({Scope.READ, Scope.COMMENT, Scope.SET_MODE})
    ai = registry.authenticate(AI_TOKEN)
    assert ai is not None and ai.actor_type is ActorType.AI_ASSISTANT


def test_unknown_or_empty_token_is_not_authenticated() -> None:
    registry = parse_principals(json.dumps(_config()))
    assert registry.authenticate("nope") is None
    assert registry.authenticate("") is None
    # hash そのものを token として出しても通らない（保存しているのは hash だけ）
    assert registry.authenticate(token_sha256(HUMAN_TOKEN)) is None


@pytest.mark.parametrize("actor_type", ["ai_assistant", "agent"])
@pytest.mark.parametrize("scope", sorted(s.value for s in HUMAN_ONLY_SCOPES))
def test_ai_principals_cannot_be_granted_human_only_scopes(actor_type: str, scope: str) -> None:
    """INV-47: AI（ai_assistant / agent）に人専用 scope を与える設定は読み込めない。"""
    config = {token_sha256(AI_TOKEN): _entry(actor_type, "bot", ["read", scope])}
    with pytest.raises(PrincipalConfigError, match="human-only"):
        parse_principals(json.dumps(config))


def test_system_actor_cannot_be_an_api_principal() -> None:
    config = {token_sha256(AI_TOKEN): _entry("system", "sys", ["read"])}
    with pytest.raises(PrincipalConfigError):
        parse_principals(json.dumps(config))


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        json.dumps({"short": _entry("human", "a", ["read"])}),
        # 平文の token を key に置いた（hash でない）
        json.dumps({HUMAN_TOKEN: _entry("human", "a", ["read"])}),
        json.dumps({token_sha256("x").upper(): _entry("human", "a", ["read"])}),
        json.dumps({token_sha256("x"): _entry("human", "a", ["read", "set_budget"])}),
        json.dumps({token_sha256("x"): {**_entry("human", "a", ["read"]), "extra": 1}}),
        json.dumps({token_sha256("x"): _entry("human", "", ["read"])}),
    ],
)
def test_malformed_configuration_is_rejected(bad: str) -> None:
    with pytest.raises(PrincipalConfigError):
        parse_principals(bad)


def test_config_errors_never_echo_the_key_or_a_clear_token() -> None:
    """平文 token を誤って key に置いても、例外の文言にその値を出さない（ログに残る）。"""
    with pytest.raises(PrincipalConfigError) as info:
        parse_principals(json.dumps({HUMAN_TOKEN: _entry("human", "a", ["read"])}))
    assert HUMAN_TOKEN not in str(info.value)
    good_hash = token_sha256(AI_TOKEN)
    with pytest.raises(PrincipalConfigError) as info2:
        parse_principals(json.dumps({good_hash: _entry("agent", "a", ["set_mode"])}))
    assert good_hash not in str(info2.value)


def test_load_returns_none_when_unconfigured() -> None:
    assert load_principals(None) is None
    assert load_principals("") is None
    assert load_principals("   ") is None


def test_load_accepts_inline_json_or_a_file_path(tmp_path) -> None:
    inline = load_principals(json.dumps(_config()))
    assert inline is not None and inline.authenticate(HUMAN_TOKEN) is not None
    path = tmp_path / "principals.json"
    path.write_text(json.dumps(_config()), encoding="utf-8")
    from_file = load_principals(str(path))
    assert from_file is not None and from_file.authenticate(AI_TOKEN) is not None


def test_load_rejects_a_missing_file(tmp_path) -> None:
    with pytest.raises(PrincipalConfigError):
        load_principals(str(tmp_path / "missing.json"))


def test_settings_hide_the_principal_setting_in_dumps() -> None:
    from infrastructure.config import Settings

    raw = json.dumps(_config())
    settings = Settings(intervention_principals=raw)  # type: ignore[call-arg]
    assert settings.intervention_principals is not None
    assert settings.intervention_principals.get_secret_value() == raw
    assert raw not in str(settings.model_dump())


def test_settings_default_is_unconfigured(monkeypatch) -> None:
    from infrastructure.config import Settings

    monkeypatch.delenv("INTERVENTION_PRINCIPALS", raising=False)
    assert Settings(_env_file=None).intervention_principals is None  # type: ignore[call-arg]


def test_parsing_does_not_log_secrets(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    parse_principals(json.dumps(_config()))
    with pytest.raises(PrincipalConfigError):
        parse_principals(json.dumps({HUMAN_TOKEN: _entry("human", "a", ["read"])}))
    assert HUMAN_TOKEN not in caplog.text
    assert token_sha256(HUMAN_TOKEN) not in caplog.text
