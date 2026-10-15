"""Agentic の Feature Flag（ADR-0043 §5、INV-55）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.intervention import AgentRole
from infrastructure.config import Settings, enabled_agent_roles


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in ("JPMAS2_AGENTIC_WORKFLOWS", "JPMAS2_ENABLED_AGENTS", "JPMAS2_AGENT_LLM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_every_agentic_flag_defaults_to_off(monkeypatch) -> None:
    s = _settings(monkeypatch)
    assert s.jpmas2_application_api is False
    assert s.jpmas2_agentic_workflows is False
    assert s.jpmas2_enabled_agents == ""
    assert enabled_agent_roles(s) == frozenset()
    assert s.jpmas2_agent_llm == "codex"


def test_enabled_agents_are_parsed_from_a_comma_separated_list(monkeypatch) -> None:
    s = _settings(monkeypatch, JPMAS2_ENABLED_AGENTS=" strategy, learning ,,")
    assert enabled_agent_roles(s) == {AgentRole.STRATEGY, AgentRole.LEARNING}


def test_an_unknown_agent_name_fails_at_load_with_the_known_names(monkeypatch) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(monkeypatch, JPMAS2_ENABLED_AGENTS="strategy,wizard")
    assert "wizard" in str(exc.value) and "creative_director" in str(exc.value)


def test_the_agent_llm_is_codex_or_fake_only(monkeypatch) -> None:
    assert _settings(monkeypatch, JPMAS2_AGENT_LLM="fake").jpmas2_agent_llm == "fake"
    with pytest.raises(ValidationError):
        _settings(monkeypatch, JPMAS2_AGENT_LLM="openai")
