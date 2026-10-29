"""供給元・下地・クリップ符号化・描画の下地（ADR-0084）。理由は docs/testing/v3-phase3-geo.md。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
from typing import Any

import pytest

from contracts.render import RENDER_PROFILES
from domain.errors import GeoSourceInvalidError, RenderEngineFailedError
from infrastructure.geo.clip import FfmpegMapClipEncoder
from infrastructure.geo.service import GeoRenderService, SceneSlot, render_spec_for
from infrastructure.geo.source import LocalDirectoryGeoSource, parse_basemap
from infrastructure.media.map_render import BaseMap, MapRenderSpec, render_map
from tests.support import geo_scene as gs
from tests.support.geo_scene import FakeEncoder

EPISODE = "11111111-1111-4111-8111-111111111111"
PROFILE = RENDER_PROFILES["shorts_vertical"]
PINNED_FFMPEG = pathlib.Path.home() / ".local/share/avp/ffmpeg/7.1.1/ffmpeg"
PINNED_SHA = "810f94020e76e2b58fb44759a322e86bea5d213ebededad7471f3a15b0bf2c5c"

# 経緯度の三角形 1 つ（Shikoku 東部あたりを覆う、テスト用の合成の陸地。実在の海岸線ではない）
TRIANGLE = {
    "type": "Feature",
    "properties": {},
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[133.0, 33.5], [135.5, 33.5], [134.2, 34.8], [133.0, 33.5]]],
    },
}


def write_source(root: pathlib.Path, *, specs: bool = True, **over: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / EPISODE).mkdir(exist_ok=True)
    gazetteer = {
        "schema_version": "1.0",
        "license": "test fixture (author-supplied, CC0)",
        "curated_by": "tests",
        "places": [p.model_dump(mode="json") for p in gs.GAZETTEER],
    }
    gazetteer.update(over.get("gazetteer", {}))
    (root / "gazetteer.json").write_text(json.dumps(gazetteer), encoding="utf-8")
    evidence = {
        "request_id": "22222222-2222-4222-8222-222222222222",
        "type": "research_evidence",
        "schema_version": "1.0",
        "policy_version": "p1",
        "as_of": "2026-10-01T00:00:00+00:00",
        "claims": [c.model_dump(mode="json") for c in gs.CLAIMS],
        "sources": [s.model_dump(mode="json") for s in gs.SOURCES],
        "links": [
            {
                "claim_id": "C-001",
                "source_id": "S-001",
                "stance": "supports",
                "locator": "p.1",
                "excerpt": "fixture excerpt",
                "excerpt_sha256": hashlib.sha256(b"fixture excerpt").hexdigest(),
            }
        ],
    }
    (root / EPISODE / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    if specs:
        body = {
            "schema_version": "1.0",
            "specs": [
                {"storyboard_scene_id": "sb2", "request": gs.request().model_dump(mode="json")}
            ],
        }
        (root / EPISODE / "specs.json").write_text(json.dumps(body), encoding="utf-8")


def test_a_directory_source_loads_specs_gazetteer_and_evidence(tmp_path: pathlib.Path) -> None:
    write_source(tmp_path)
    data = LocalDirectoryGeoSource(tmp_path).load(EPISODE)
    assert data is not None
    assert [s.storyboard_scene_id for s in data.specs.specs] == ["sb2"]
    assert len(data.gazetteer) == len(gs.GAZETTEER) and data.basemap_bytes is None
    assert [c.claim_id for c in data.claims] == ["C-001"]


def test_an_episode_without_specs_has_no_map(tmp_path: pathlib.Path) -> None:
    write_source(tmp_path, specs=False)
    assert LocalDirectoryGeoSource(tmp_path).load(EPISODE) is None


@pytest.mark.parametrize("broken", ["gazetteer.json", "specs.json", "evidence.json"])
def test_a_present_but_unreadable_file_is_an_error_not_no_map(
    tmp_path: pathlib.Path, broken: str
) -> None:
    write_source(tmp_path)
    target = tmp_path / broken if broken == "gazetteer.json" else tmp_path / EPISODE / broken
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(GeoSourceInvalidError):
        LocalDirectoryGeoSource(tmp_path).load(EPISODE)


def test_a_gazetteer_without_a_license_or_curator_is_refused(tmp_path: pathlib.Path) -> None:
    write_source(tmp_path, gazetteer={"license": ""})
    with pytest.raises(GeoSourceInvalidError):
        LocalDirectoryGeoSource(tmp_path).load(EPISODE)


def test_a_missing_gazetteer_is_an_error(tmp_path: pathlib.Path) -> None:
    write_source(tmp_path)
    (tmp_path / "gazetteer.json").unlink()
    with pytest.raises(GeoSourceInvalidError):
        LocalDirectoryGeoSource(tmp_path).load(EPISODE)


@pytest.mark.parametrize("bad", ["", "../x", ".hidden", "a/b"])
def test_an_unsafe_episode_id_never_reaches_the_filesystem(
    tmp_path: pathlib.Path, bad: str
) -> None:
    with pytest.raises(GeoSourceInvalidError):
        LocalDirectoryGeoSource(tmp_path).load(bad)


def test_basemap_keeps_only_rings_that_touch_the_region() -> None:
    far = {"type": "Polygon", "coordinates": [[[10, 10], [11, 10], [10, 11], [10, 10]]]}
    doc = {
        "type": "FeatureCollection",
        "features": [TRIANGLE, {"type": "Feature", "geometry": far}],
    }
    base = parse_basemap(json.dumps(doc).encode(), gs.KINKI_SHIKOKU_BBOX, sha256="a" * 64)
    assert len(base.rings) == 1 and base.caption


@pytest.mark.parametrize("raw", [b"nope", b'{"type":"Point","coordinates":[1,2]}', b'{"type":1}'])
def test_a_broken_basemap_is_an_error(raw: bytes) -> None:
    with pytest.raises(GeoSourceInvalidError):
        parse_basemap(raw, gs.KINKI_SHIKOKU_BBOX, sha256="a" * 64)


def test_a_basemap_changes_the_image_and_its_identity_only_when_given() -> None:
    manifest = gs.manifest_from(gs.spec())
    spec = MapRenderSpec(width=540, height=960, margin_px=32, font_px=30, marker_radius_px=5)
    plain = render_map(manifest, spec)
    base = parse_basemap(json.dumps(TRIANGLE).encode(), manifest.bbox, sha256="b" * 64)
    with_base = render_map(manifest, spec, None, base)
    assert plain.png != with_base.png
    assert plain.identity_sha256 != with_base.identity_sha256
    assert render_map(manifest, spec).png == plain.png  # 下地なしは従来と同じバイト列
    assert render_map(manifest, spec, None, base).png == with_base.png
    # 下地の出典の注記が描かれ、見た目の検査の対象になる
    assert any(t.target_id == manifest.scene_id for t in with_base.layout.labels[-1:])


def test_the_base_caption_is_part_of_the_layout() -> None:
    manifest = gs.manifest_from(gs.spec())
    spec = MapRenderSpec(width=540, height=960, margin_px=32, font_px=30, marker_radius_px=5)
    base = BaseMap(rings=(), sha256="b" * 64, caption="Coastline: modern")
    assert len(render_map(manifest, spec, None, base).layout.labels) == (
        len(render_map(manifest, spec).layout.labels) + 1
    )


def test_the_render_spec_follows_the_profile_resolution() -> None:
    spec = render_spec_for(PROFILE)
    assert (spec.width, spec.height) == (PROFILE.width, PROFILE.height) == (1080, 1920)
    assert spec.font_px >= 28  # 見た目の検査の最小フォント


def test_the_service_identity_changes_with_the_verdict_and_the_inputs(
    tmp_path: pathlib.Path,
) -> None:
    write_source(tmp_path)
    slots = [SceneSlot("sb2", "s2", 4000)]
    svc = GeoRenderService(
        source=LocalDirectoryGeoSource(tmp_path), encoder=FakeEncoder(), font_path=None
    )
    a = svc.prepare(EPISODE, slots, PROFILE).identity
    assert a == svc.prepare(EPISODE, slots, PROFILE).identity  # 決定論
    (tmp_path / "gazetteer.json").write_text(
        (tmp_path / "gazetteer.json").read_text().replace("34.36", "34.37"), encoding="utf-8"
    )
    assert svc.prepare(EPISODE, slots, PROFILE).identity != a  # gazetteer の変更は別の入力
    other_duration = svc.prepare(EPISODE, [SceneSlot("sb2", "s2", 5000)], PROFILE).identity
    assert other_duration != a  # クリップの尺は identity に入る


def test_the_service_refuses_an_out_of_range_scene_duration(tmp_path: pathlib.Path) -> None:
    write_source(tmp_path)
    svc = GeoRenderService(
        source=LocalDirectoryGeoSource(tmp_path), encoder=FakeEncoder(), font_path=None
    )
    with pytest.raises(GeoSourceInvalidError):
        svc.prepare(EPISODE, [SceneSlot("sb2", "s2", 10**9)], PROFILE)


@pytest.mark.skipif(
    not PINNED_FFMPEG.is_file()
    or hashlib.sha256(PINNED_FFMPEG.read_bytes()).hexdigest() != PINNED_SHA,
    reason="the pinned render ffmpeg is not installed on this host",
)
def test_the_real_encoder_makes_a_profile_sized_mp4_of_the_requested_length(
    tmp_path: pathlib.Path,
) -> None:
    from infrastructure.media.probe import PillowAvMediaProbe  # noqa: F401  (av が入っていること)

    manifest = gs.manifest_from(gs.spec())
    png = tmp_path / "map.png"
    png.write_bytes(render_map(manifest, render_spec_for(PROFILE)).png)
    out = tmp_path / "map.mp4"
    FfmpegMapClipEncoder(PINNED_FFMPEG).encode(png, out, 4000, PROFILE)
    ffprobe = PINNED_FFMPEG.with_name("ffprobe")
    info = json.loads(
        subprocess.run(
            [str(ffprobe), "-v", "error", "-print_format", "json", "-show_streams", str(out)],
            capture_output=True,
            check=True,
        ).stdout
    )
    (video,) = [s for s in info["streams"] if s["codec_type"] == "video"]
    assert (video["width"], video["height"], video["codec_name"]) == (1080, 1920, "h264")
    assert abs(float(video["duration"]) - 4.0) < 0.1
    # 同じ入力は同じバイト列（bitexact）
    again = tmp_path / "again.mp4"
    FfmpegMapClipEncoder(PINNED_FFMPEG).encode(png, again, 4000, PROFILE)
    assert again.read_bytes() == out.read_bytes()


def test_a_failing_encoder_is_a_retryable_render_error(tmp_path: pathlib.Path) -> None:
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    fake.chmod(0o755)
    png = tmp_path / "map.png"
    png.write_bytes(b"x")
    with pytest.raises(RenderEngineFailedError):
        FfmpegMapClipEncoder(fake).encode(png, tmp_path / "o.mp4", 1000, PROFILE)
    assert shutil.which("sh")


NOTO = pathlib.Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")


@pytest.mark.skipif(not NOTO.is_file(), reason="Noto Sans CJK is not installed on this host")
def test_japanese_labels_pass_with_the_pinned_cjk_font_and_the_font_is_in_the_identity(
    tmp_path: pathlib.Path,
) -> None:
    """Phase 2 §4-6（CJK フォント）: 日本語ラベルは豆腐にならず PASS し、フォントが記録に入る。"""
    from contracts.geo import GeoVerdict
    from contracts.geo_scene import GeoMapSpecs
    from infrastructure.geo.prepare import prepare_map
    from infrastructure.geo.source import GeoSourceData

    ja = gs.spec(
        places=(
            gs.place_req("gz:yashima", "屋島", label="屋島"),
            gs.place_req("gz:kyoto", "京都", label="京都"),
        )
    )
    data = GeoSourceData(
        specs=GeoMapSpecs(schema_version="1.0", specs=()),
        gazetteer=gs.GAZETTEER,
        claims=gs.CLAIMS,
        sources=gs.SOURCES,
    )
    spec = render_spec_for(PROFILE)
    ok = prepare_map(ja, data, spec, NOTO.read_bytes())
    assert ok.record.verdict is GeoVerdict.PASS and ok.applied
    assert ok.record.font_id and ok.record.font_id.startswith("font-sha256:")
    none = prepare_map(ja, data, spec, None)  # 既定フォントでは同じ依頼が豆腐になり使われない
    assert none.record.verdict is GeoVerdict.BLOCK and not none.applied
