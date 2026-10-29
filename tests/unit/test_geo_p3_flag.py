"""GEO_MAPS_ENABLED の OFF（既定）が従来と同じであること、ON の前提（ADR-0084 §9 / INV-143）。"""

from __future__ import annotations

import pytest

from domain.render.identity import render_input_hash
from infrastructure.config import Settings
from infrastructure.geo.guard import require_quality_gate
from tests.unit.test_bgm_off_golden import GOLDEN_INPUT_HASH, GOLDEN_INPUT_HASH_THREADS4
from tests.unit.test_render_identity import BASE


def test_geo_none_keeps_the_pre_geo_input_hash() -> None:
    """期待値は地図結線の前（BGM 結線前の実測値）。geo=None は hash に何も足さない。"""
    assert render_input_hash(**BASE, geo=None) == GOLDEN_INPUT_HASH
    assert render_input_hash(**BASE, engine_threads=4, geo=None) == GOLDEN_INPUT_HASH_THREADS4


def test_any_geo_identity_changes_the_hash_even_when_it_lists_no_map() -> None:
    marked = render_input_hash(**BASE, geo={"version": "1", "maps": []})
    assert marked != GOLDEN_INPUT_HASH
    assert marked != render_input_hash(**BASE, geo={"version": "1", "maps": [{"x": 1}]})


def test_the_geo_identity_and_the_bgm_identity_do_not_collide() -> None:
    same = {"version": "1"}
    assert render_input_hash(**BASE, geo=same) != render_input_hash(**BASE, bgm=same)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in ("GEO_MAPS_ENABLED", "AVP_QUALITY_GATE_ENFORCE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_maps_cannot_be_enabled_while_the_quality_gate_is_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = _settings(monkeypatch, GEO_MAPS_ENABLED="true")
    assert on.geo_maps_enabled and not on.quality_gate_enforce
    with pytest.raises(SystemExit):
        require_quality_gate(on)


def test_maps_may_be_enabled_with_the_gate_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _settings(monkeypatch, GEO_MAPS_ENABLED="true", AVP_QUALITY_GATE_ENFORCE="true")
    require_quality_gate(ok)


def test_the_default_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).geo_maps_enabled is False
