"""BGM の設定キー（ADR-0077 §1）。既定は OFF。"""

from __future__ import annotations

import pytest

from contracts.music_render import BGM_SETTING_KEY
from infrastructure.config import Settings


def test_default_is_off_and_the_key_name_is_single_sourced() -> None:
    assert BGM_SETTING_KEY in Settings.model_fields
    assert Settings.model_fields[BGM_SETTING_KEY].default is False


def test_env_var_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BGM_ENABLED", "true")
    assert Settings(_env_file=None).bgm_enabled is True  # type: ignore[call-arg]
    monkeypatch.delenv("BGM_ENABLED")
    assert Settings(_env_file=None).bgm_enabled is False  # type: ignore[call-arg]
