"""Codex の model 設定の早期診断（docs/operations/codex-model-config.md）。

本物の codex は起動しない（INV-18）。起きた事故: ChatGPT アカウントで使えない model が
config.toml に入り、日次が 7 日連続で失敗した。原因が活動の失敗ログの奥にしか出なかった。
"""

from __future__ import annotations

import logging

import pytest

from contracts.codex_models import extract_unsupported_model, parse_allowlist
from domain.errors import UnsupportedModelError
from infrastructure.providers.codex_model_check import (
    configured_model_from_toml,
    effective_model,
    verify_codex_model,
)

REJECTED = "The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account."


def test_extract_unsupported_model_reads_the_name() -> None:
    assert extract_unsupported_model(REJECTED) == "gpt-6-sol"
    assert extract_unsupported_model("something else") is None


def test_parse_allowlist_is_empty_by_default() -> None:
    assert parse_allowlist("") == frozenset()
    assert parse_allowlist(" a, b ,,c ") == frozenset({"a", "b", "c"})


def test_configured_model_reads_only_the_top_level_key(tmp_path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text('model = "gpt-6-sol"\n[projects."/x"]\nmodel = "nested"\n', encoding="utf-8")
    assert configured_model_from_toml(toml) == "gpt-6-sol"
    assert configured_model_from_toml(tmp_path / "missing.toml") is None
    toml.write_text("not = [valid", encoding="utf-8")
    assert configured_model_from_toml(toml) is None


def test_effective_model_prefers_the_explicit_setting(tmp_path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text('model = "from-file"\n', encoding="utf-8")
    assert effective_model("explicit", toml) == "explicit"
    assert effective_model("", toml) == "from-file"
    assert effective_model("", tmp_path / "none.toml") is None


def test_verify_without_allowlist_never_claims_the_model_works(tmp_path, caplog) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text('model = "gpt-6-sol"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        verify_codex_model(model_setting="", allowed=frozenset(), config_toml=toml)
    assert "gpt-6-sol" in caplog.text
    assert "unverified" in caplog.text


def test_verify_rejects_a_model_outside_the_allowlist(tmp_path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text('model = "gpt-6-sol"\n', encoding="utf-8")
    with pytest.raises(UnsupportedModelError) as excinfo:
        verify_codex_model(model_setting="", allowed=frozenset({"gpt-5.6-sol"}), config_toml=toml)
    assert excinfo.value.model == "gpt-6-sol"
    assert "gpt-5.6-sol" in str(excinfo.value)


def test_verify_accepts_an_allowed_model(tmp_path) -> None:
    verify_codex_model(
        model_setting="gpt-5.6-sol",
        allowed=frozenset({"gpt-5.6-sol"}),
        config_toml=tmp_path / "none.toml",
    )
