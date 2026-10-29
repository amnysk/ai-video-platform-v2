"""Render Activity の地図の結線（ADR-0084 / INV-139〜145）。SQLite + メモリストア + fake。

fal を呼ぶ経路は Render に無い（地図の部品は有料 provider を import しない: architecture テスト）。
ここでは「地図 PASS ならそのシーンの動画が差し替わる」「拒否なら差し替わらず証跡が残る」
「OFF（geo=None）は従来と同じ」を、実際の Render Activity で確かめる。
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from contracts.artifacts import parse_final_video
from contracts.geo_scene import GeoMapSpec, GeoMapSpecs, GeoRenderEvidence
from contracts.quality import QualityVerdict
from contracts.render_activities import RenderAdmitRequest
from domain.errors import GeoSourceInvalidError
from domain.geo.quality_checks import map_quality_checks
from infrastructure.geo.service import GeoRenderService, geo_evidence_key
from infrastructure.geo.source import GeoSourceData
from tests.support import geo_scene as gs
from tests.support.geo_scene import FakeEncoder
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import WF, Harness, _final_rows, _req
from tests.unit.test_render_activities_bgm import _input_hashes


class FakeGeoSource:
    def __init__(self, data: GeoSourceData | None = None, error: BaseException | None = None):
        self.data = data
        self.error = error
        self.calls = 0

    def load(self, episode_id: str) -> GeoSourceData | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.data


def data_for(*specs: GeoMapSpec, **over: Any) -> GeoSourceData:
    base: dict[str, Any] = {
        "specs": GeoMapSpecs(schema_version="1.0", specs=specs),
        "gazetteer": gs.GAZETTEER,
        "claims": gs.CLAIMS,
        "sources": gs.SOURCES,
    }
    base.update(over)
    return GeoSourceData(**base)


def sb2_spec(**over: Any) -> GeoMapSpec:
    """seed の sb2 は script scene s2 に属する。"""
    return GeoMapSpec(storyboard_scene_id="sb2", request=gs.request(**over))


class GeoHarness(Harness):
    """作業領域は描画後に消えるので、エンジンが受け取った時点の動画の中身を控える。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.captured: list[dict[str, bytes]] = []
        original = self.engine.on_render

        def capture(request) -> None:  # type: ignore[no-untyped-def]
            self.captured.append({k: v.read_bytes() for k, v in request.scene_video_paths.items()})
            if original is not None:
                original(request)

        self.engine.on_render = capture


def harness(session_factory, store, tmp_path, source=None, encoder=None) -> GeoHarness:
    tmp_path.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {}
    if source is not None:
        extra["geo"] = GeoRenderService(
            source=source, encoder=encoder or FakeEncoder(), font_path=None
        )
    return GeoHarness(session_factory, store, tmp_path, **extra)


async def admit_and_render(h: Harness, seed, run: str = "run-1"):
    await h.activities.admit(RenderAdmitRequest(seed.episode_id, WF, run))
    return await h.activities.render_final_video(_req(seed, run=run))


async def evidence_of(store, session_factory, episode_id: str) -> GeoRenderEvidence:
    rows = await _final_rows(session_factory, episode_id)
    final = parse_final_video(await store.get_json(rows[-1].object_key))
    input_hash = (await _input_hashes(session_factory, episode_id))[-1]
    key = geo_evidence_key(episode_id, final.media.sha256, input_hash)
    return GeoRenderEvidence.model_validate(await store.get_json(key))


async def test_flag_off_writes_no_geo_evidence_and_keeps_the_same_identity(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    off = harness(session_factory, artifact_store, tmp_path / "off")
    await admit_and_render(off, seed)
    assert not [k for k in artifact_store._objects if "geo_map_evidence" in k]
    assert off.captured[0]["sb2"].startswith(b"fake-mp4")


async def test_flag_on_without_specs_writes_an_explicit_not_used_evidence(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    off = harness(session_factory, artifact_store, tmp_path / "off")
    await admit_and_render(off, seed)
    off_hash = (await _input_hashes(session_factory, seed.episode_id))[0]

    on = harness(session_factory, artifact_store, tmp_path / "on", FakeGeoSource(None))
    again = await admit_and_render(on, seed, run="run-2")
    # ON の印が identity に入るので、証跡の無い従来の動画を再利用せず 1 回描き直して証跡を書く
    assert not again.skipped
    assert (await _input_hashes(session_factory, seed.episode_id))[-1] != off_hash
    evidence = await evidence_of(artifact_store, session_factory, seed.episode_id)
    assert evidence.records == () and evidence.intended_scene_ids == ()
    (check,) = map_quality_checks(
        final_video_sha256=evidence.final_video_sha256,
        records=evidence.records,
        intended_scene_ids=evidence.intended_scene_ids,
    )
    assert check.verdict is QualityVerdict.PASS


async def test_a_verified_map_replaces_that_scenes_video_and_is_bound_to_the_final_video(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    encoder = FakeEncoder()
    h = harness(
        session_factory, artifact_store, tmp_path, FakeGeoSource(data_for(sb2_spec())), encoder
    )
    await admit_and_render(h, seed)

    seen = h.captured[0]
    assert seen["sb2"].startswith(b"fake-map-clip:")
    assert seen["sb1"].startswith(b"fake-mp4")  # 他のシーンは従来のまま
    assert encoder.calls == [(4000, "shorts_vertical")]  # 尺はシーン動画の実尺

    evidence = await evidence_of(artifact_store, session_factory, seed.episode_id)
    (record,) = evidence.records
    assert record.clip_sha256 is not None and record.final_video_sha256 is not None
    assert evidence.intended_scene_ids == ("s2",)
    (check,) = map_quality_checks(
        final_video_sha256=evidence.final_video_sha256,
        records=evidence.records,
        intended_scene_ids=evidence.intended_scene_ids,
    )
    assert check.verdict is QualityVerdict.PASS and check.scene_id == "s2"


async def test_a_refused_map_keeps_the_original_video_and_the_evidence_blocks_the_upload(
    session_factory, artifact_store, tmp_path, caplog
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    encoder = FakeEncoder()
    bad = sb2_spec(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),))  # 1185 年に "Tokyo"
    h = harness(session_factory, artifact_store, tmp_path, FakeGeoSource(data_for(bad)), encoder)
    with caplog.at_level("WARNING"):
        await admit_and_render(h, seed)

    assert h.captured[0]["sb2"].startswith(b"fake-mp4")
    assert encoder.calls == []
    assert "map refused" in caplog.text
    evidence = await evidence_of(artifact_store, session_factory, seed.episode_id)
    (check,) = map_quality_checks(
        final_video_sha256=evidence.final_video_sha256,
        records=evidence.records,
        intended_scene_ids=evidence.intended_scene_ids,
    )
    assert check.verdict is QualityVerdict.BLOCK
    assert "Tokyo" not in check.detail


async def test_applying_or_refusing_a_map_changes_the_render_identity(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    good = harness(
        session_factory, artifact_store, tmp_path / "g", FakeGeoSource(data_for(sb2_spec()))
    )
    await admit_and_render(good, seed)
    bad_spec = sb2_spec(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),))
    bad = harness(
        session_factory, artifact_store, tmp_path / "b", FakeGeoSource(data_for(bad_spec))
    )
    again = await admit_and_render(bad, seed, run="run-2")
    # 別の判定（適用 → 拒否）は別の入力: 現行の動画を再利用せず描き直す
    assert not again.skipped and len(bad.engine.requests) == 1


async def test_the_same_inputs_are_not_rendered_twice(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    src = FakeGeoSource(data_for(sb2_spec()))
    first = harness(session_factory, artifact_store, tmp_path / "a", src)
    await admit_and_render(first, seed)
    second = harness(session_factory, artifact_store, tmp_path / "b", src)
    again = await admit_and_render(second, seed, run="run-2")
    assert again.skipped and second.engine.requests == []


async def test_a_spec_that_points_at_the_wrong_scene_stops_the_render(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    wrong = GeoMapSpec(storyboard_scene_id="sb1", request=gs.request())  # sb1 は s1 のシーン
    h = harness(session_factory, artifact_store, tmp_path, FakeGeoSource(data_for(wrong)))
    with pytest.raises(ApplicationError) as err:
        await admit_and_render(h, seed)
    assert err.value.type == "GeoSourceInvalidError"
    assert h.engine.requests == []


async def test_an_unreadable_source_stops_the_render_instead_of_meaning_no_map(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    src = FakeGeoSource(error=GeoSourceInvalidError("specs.json is not readable"))
    h = harness(session_factory, artifact_store, tmp_path, src)
    with pytest.raises(ApplicationError) as err:
        await admit_and_render(h, seed)
    assert err.value.type == "GeoSourceInvalidError"
    rows = await _final_rows(session_factory, seed.episode_id)
    assert rows == []


async def test_no_final_video_row_exists_without_its_geo_evidence(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    h = harness(session_factory, artifact_store, tmp_path, FakeGeoSource(data_for(sb2_spec())))
    await admit_and_render(h, seed)
    rows = await _final_rows(session_factory, seed.episode_id)
    hashes = await _input_hashes(session_factory, seed.episode_id)
    assert len(rows) == len(hashes) == 1
    final = parse_final_video(await artifact_store.get_json(rows[0].object_key))
    assert await artifact_store.exists(
        geo_evidence_key(seed.episode_id, final.media.sha256, hashes[0])
    )
