"""flag OFF（既定）では render / storyboard / production の出力が接続前と同じ（ADR-0078、INV-120）。

理由は docs/testing/geo-maps-rationale.md。
"""

from __future__ import annotations

import hashlib

import pytest

import infrastructure.media.map_scene_visual as scene_visual
from domain.render.identity import render_plan_sha256
from infrastructure.config import Settings
from infrastructure.media.map_render import MapRenderSpec
from infrastructure.media.map_scene_visual import render_map_scene_visual
from infrastructure.render.ffmpeg_graph import build_filtergraph
from tests.support import fake_gazetteer as fg
from tests.support.render_plans import make_plan

#: 本ブランチの前（6d14c30 の系統）の同じ入力から得た値。変わったら既存の出力が変わっている。
GOLDEN = {
    "shorts_vertical": (
        "155f7465af610280186d7f40c223e95e85acc08c588d6d843c1e2a4ee89e39dd",
        "0bef17a906d33f8000936787c55f9abf3b9e8095b11ad708adb0b785e03f20a6",
    ),
    "long_form_horizontal": (
        "a1e5fa2882dc6a365ab48b8e286ceb48cc2319a88fbf3b9f693f4a9482069cd4",
        "022bd8cea59aa141c18181526b3d26766aef90ff1db16e02751f963faddcbca9",
    ),
}


def test_the_flag_defaults_to_off_and_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings.model_fields["geo_maps_enabled"].default is False
    monkeypatch.delenv("GEO_MAPS_ENABLED", raising=False)
    assert Settings(_env_file=None).geo_maps_enabled is False  # type: ignore[call-arg]
    monkeypatch.setenv("GEO_MAPS_ENABLED", "true")
    assert Settings(_env_file=None).geo_maps_enabled is True  # type: ignore[call-arg]


def test_with_the_flag_off_nothing_is_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def spy(*args: object, **kwargs: object) -> None:
        calls.append("render_map")

    monkeypatch.setattr(scene_visual, "render_map", spy)
    result = render_map_scene_visual(
        enabled=False, manifest=fg.manifest(), spec=MapRenderSpec(width=320, height=240)
    )
    assert result is None
    assert calls == []


def test_with_the_flag_on_a_map_is_rendered() -> None:
    result = render_map_scene_visual(
        enabled=True, manifest=fg.manifest(), spec=MapRenderSpec(width=320, height=240)
    )
    assert result is not None and result.png.startswith(b"\x89PNG")


@pytest.mark.parametrize("profile", list(GOLDEN))
def test_render_identity_and_filtergraph_are_byte_identical_to_before(profile: str) -> None:
    plan = make_plan(
        profile=profile, scenes=((1200, 1000), (800, 1000)), voices=((0, 900), (1100, 700))
    )
    plan_sha, graph_sha = GOLDEN[profile]
    assert render_plan_sha256(plan) == plan_sha
    assert hashlib.sha256(build_filtergraph(plan).encode()).hexdigest() == graph_sha
