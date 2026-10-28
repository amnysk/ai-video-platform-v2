"""地図の決定論的描画と ffmpeg 入力（ADR-0078、INV-119）。fal・ネットワークに出ない。

理由は docs/testing/geo-maps-rationale.md。
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import socket
import subprocess
import sys

import pytest
from PIL import Image

from contracts.geo import GeoVerdict
from contracts.geo_map import PresentationReasonCode
from contracts.render import get_render_profile
from domain.geo.map_presentation import check_map_presentation
from infrastructure.media.map_clip import build_map_clip_argv
from infrastructure.media.map_render import MapRenderSpec, render_map
from tests.support import fake_gazetteer as fg

SPEC = MapRenderSpec(width=640, height=480)


def _manifest(**over: object):
    places = [
        fg.place(),
        fg.place(
            place_id="gz:osaka-castle",
            display_name="Osaka Castle",
            lat=34.6873,
            lon=135.5262,
            feature_type="structure",
            valid_start_year=1583,
        ),
        fg.place(
            place_id="gz:kyoto",
            display_name="Kyoto",
            lat=35.0116,
            lon=135.7681,
            feature_type="settlement",
            valid_start_year=None,
        ),
    ]
    labels = [{"place_id": p["place_id"], "text": p["display_name"]} for p in places]
    kansai = {"min_lon": 134.5, "min_lat": 34.3, "max_lon": 137.5, "max_lat": 35.8}
    return fg.manifest(places=places, labels=labels, bbox=kansai, **over)


def test_the_same_manifest_renders_to_identical_bytes() -> None:
    a = render_map(_manifest(), SPEC)
    b = render_map(_manifest(), SPEC)
    assert a.png == b.png
    assert a.sha256 == b.sha256 == hashlib.sha256(a.png).hexdigest()
    assert a.layout == b.layout


def test_rendering_in_a_fresh_process_gives_the_same_hash() -> None:
    """プロセス間（ハッシュシード・import 順）で変わらない。"""
    code = (
        "from tests.support import fake_gazetteer as fg\n"
        "from infrastructure.media.map_render import MapRenderSpec, render_map\n"
        "from tests.unit.test_geo_map_render import _manifest\n"
        "print(render_map(_manifest(), MapRenderSpec(width=640, height=480)).sha256)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": "random", "PATH": ""},
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
    ).stdout.strip()
    assert out == render_map(_manifest(), SPEC).sha256


def test_the_order_of_places_and_labels_does_not_change_the_image() -> None:
    m = _manifest()
    reordered = m.model_copy(update={"places": m.places[::-1], "labels": m.labels[::-1]})
    assert render_map(reordered, SPEC).png == render_map(m, SPEC).png


def test_a_different_manifest_gives_a_different_image() -> None:
    moved = _manifest()
    places = (moved.places[0].model_copy(update={"lat": 36.5}), *moved.places[1:])
    other = moved.model_copy(update={"places": places})
    assert render_map(other, SPEC).sha256 != render_map(moved, SPEC).sha256


def test_the_png_is_a_plain_rgb_image_of_the_requested_size_without_metadata() -> None:
    rendered = render_map(_manifest(), SPEC)
    image = Image.open(io.BytesIO(rendered.png))
    assert (image.format, image.mode, image.size) == ("PNG", "RGB", (640, 480))
    assert set(image.info) <= {"dpi"} or not image.info
    assert b"tIME" not in rendered.png and b"tEXt" not in rendered.png


def test_the_image_actually_draws_something() -> None:
    image = Image.open(io.BytesIO(render_map(_manifest(), SPEC).png))
    colors = image.getcolors(maxcolors=100000)
    assert colors is not None and len(colors) > 3


def test_a_marker_lands_where_the_projection_says() -> None:
    rendered = render_map(_manifest(), SPEC)
    image = Image.open(io.BytesIO(rendered.png))
    px, py = rendered.marker_px["gz:sekigahara"]
    assert image.getpixel((px, py)) == (255, 200, 0)
    # 東にある関ヶ原は大坂城より右、北にあるので上。
    ox, oy = rendered.marker_px["gz:osaka-castle"]
    assert px > ox and py < oy


def test_the_font_identity_is_part_of_the_render_identity() -> None:
    a = render_map(_manifest(), SPEC)
    assert a.font_id.startswith("pillow-default:")
    assert len(a.identity_sha256) == 64
    assert a.identity_sha256 != a.sha256


def test_the_layout_of_a_clean_map_passes_the_presentation_check() -> None:
    rendered = render_map(_manifest(), SPEC)
    assert check_map_presentation(rendered.layout).verdict is GeoVerdict.PASS


def test_crowded_labels_are_reported_by_the_presentation_check_not_hidden() -> None:
    m = _manifest()
    crowded = m.model_copy(
        update={"places": tuple(p.model_copy(update={"lat": 35.0, "lon": 136.0}) for p in m.places)}
    )
    rendered = render_map(crowded, SPEC)
    result = check_map_presentation(rendered.layout)
    assert result.verdict is GeoVerdict.REVIEW


def test_a_schematic_draws_its_disclaimer_inside_the_canvas() -> None:
    m = fg.manifest(
        map_kind="schematic",
        schematic_disclaimer="Not to scale",
        places=[fg.place(claim_id=None, source_ids=[])],
        labels=[],
    )
    rendered = render_map(m, SPEC)
    assert rendered.layout.disclaimer_required
    assert rendered.layout.disclaimer is not None
    assert check_map_presentation(rendered.layout).verdict is GeoVerdict.PASS


def test_a_schematic_without_a_disclaimer_is_blocked_by_the_presentation_check() -> None:
    m = fg.manifest(map_kind="schematic", schematic_disclaimer=None, places=[], labels=[])
    rendered = render_map(m, SPEC)
    assert check_map_presentation(rendered.layout).verdict is GeoVerdict.BLOCK


def test_a_font_must_be_supplied_as_bytes_and_changes_the_identity() -> None:
    with pytest.raises(ValueError):
        render_map(_manifest(), SPEC, font_bytes=b"not a font")


# ------------------------------------------------------------------ fal 0 回


def test_rendering_makes_no_network_or_fal_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """fal の呼び出し数 0: 描画と argv 組み立てはネットワークに出ない。"""
    calls: list[str] = []

    def spy(name: str):
        def _spy(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} must not be called")

        return _spy

    monkeypatch.setattr(socket.socket, "connect", spy("socket.connect"))
    monkeypatch.setattr(socket, "getaddrinfo", spy("getaddrinfo"))
    render_map(_manifest(), SPEC)
    build_map_clip_argv("ffmpeg", "map.png", "map.mp4", 3000, get_render_profile("shorts_vertical"))
    assert calls == []


def test_a_fresh_process_that_renders_a_map_never_loads_a_provider_module() -> None:
    code = (
        "import sys\n"
        "from infrastructure.media.map_render import MapRenderSpec, render_map\n"
        "from infrastructure.media.map_clip import build_map_clip_argv\n"
        "from tests.unit.test_geo_map_render import _manifest\n"
        "render_map(_manifest(), MapRenderSpec(width=320, height=240))\n"
        "bad = sorted(m for m in sys.modules if m.startswith('infrastructure.providers') "
        "or m.split('.')[0] in {'httpx', 'fal_client'})\n"
        "print(len(bad), bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
    ).stdout.strip()
    assert out == "0 []", out


# ------------------------------------------------------------------ ffmpeg


def test_the_clip_argv_is_deterministic_and_uses_the_existing_fit_and_fps() -> None:
    profile = get_render_profile("shorts_vertical")
    a = build_map_clip_argv("ffmpeg", "map.png", "map.mp4", 3500, profile)
    assert a == build_map_clip_argv("ffmpeg", "map.png", "map.mp4", 3500, profile)
    joined = " ".join(a)
    assert "-loop 1" in joined and "map.png" in joined and joined.endswith("-f mp4 map.mp4")
    assert "-t 3.500" in joined
    assert "+bitexact" in joined and "-map_metadata -1" in joined
    assert f"{profile.width}:{profile.height}" in joined
    assert "-an" in a


def test_the_clip_argv_rejects_a_non_positive_duration() -> None:
    profile = get_render_profile("shorts_vertical")
    with pytest.raises(ValueError):
        build_map_clip_argv("ffmpeg", "map.png", "map.mp4", 0, profile)


# ------------------------------------------------------------------ グリフ欠落（INV-119 / INV-118）


def _japanese_manifest():
    m = _manifest()
    labels = (m.labels[0].model_copy(update={"text": "関ヶ原"}), *m.labels[1:])
    return m.model_copy(update={"labels": labels})


def test_a_non_latin_label_without_a_capable_font_is_not_a_pass() -> None:
    """既定フォントに無い字は豆腐になる。箱の大きさが同じでも PASS にしない。"""
    rendered = render_map(_japanese_manifest(), SPEC)
    assert [t.target_id for t in rendered.layout.labels if t.glyphs_missing] == ["gz:sekigahara"]
    result = check_map_presentation(rendered.layout)
    assert result.verdict is GeoVerdict.BLOCK
    assert (PresentationReasonCode.FONT_MISSING_GLYPHS, GeoVerdict.BLOCK) in {
        (f.reason_code, f.verdict) for f in result.findings
    }


def test_ascii_labels_are_not_reported_as_missing_glyphs() -> None:
    rendered = render_map(_manifest(), SPEC)
    assert not any(t.glyphs_missing for t in rendered.layout.labels)


def test_a_disclaimer_with_missing_glyphs_is_blocked() -> None:
    m = fg.manifest(
        map_kind="schematic", schematic_disclaimer="模式図・縮尺不同", places=[], labels=[]
    )
    rendered = render_map(m, SPEC)
    assert check_map_presentation(rendered.layout).verdict is GeoVerdict.BLOCK


def test_the_rendered_map_exposes_the_hashes_a_result_is_bound_to() -> None:
    m = _manifest()
    rendered = render_map(m, SPEC)
    assert len(rendered.manifest_sha256) == 64
    assert rendered.manifest_sha256 == render_map(m, SPEC).manifest_sha256
    other = m.model_copy(update={"as_of": "1601"})
    assert render_map(other, SPEC).manifest_sha256 != rendered.manifest_sha256
