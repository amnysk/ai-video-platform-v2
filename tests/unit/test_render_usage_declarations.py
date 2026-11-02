"""Render が書く使用宣言・BGM 証跡の結び付け・発話余裕の実測（ADR-0086 §1〜§3 / INV-146）。

テストの理由は docs/testing/v3-phase3-wiring.md。SQLite + メモリストア + fake。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from contracts.artifacts import parse_final_video
from contracts.music_render import BgmRenderEvidence
from contracts.quality_records import UsageFeature
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import RenderEngineFailedError
from domain.music.intelligibility import SceneSpeechLevels
from domain.quality.records import CheckSubject
from infrastructure.db.models import QualityUsageDeclarationRow
from infrastructure.db.quality_repositories import QualityRecordRepository, SqlQualityRecordWriter
from infrastructure.geo.service import GeoRenderService, geo_evidence_key
from infrastructure.music.evidence import bgm_evidence_key
from tests.support import geo_scene as gs
from tests.support.bgm_catalog import NOW, make_track, sha
from tests.support.bgm_render import CREDIT_TEXT, cue, sheet
from tests.support.geo_scene import FakeEncoder
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import Harness, _final_rows
from tests.unit.test_render_activities_bgm import (
    FakeBgmSource,
    FakeLoudness,
    _admit_and_render,
    _input_hashes,
)
from tests.unit.test_render_activities_geo import FakeGeoSource, data_for, sb2_spec


class FailingWriter:
    async def write_check_set(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError

    async def declare_usage(self, *a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")


class FakeSpeechProbe:
    def __init__(self, levels=None, error: BaseException | None = None) -> None:
        self.levels = levels
        self.error = error
        self.calls = 0

    def measure(self, plan, bgm, voice_paths):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert set(voice_paths) == {v.script_scene_id for v in plan.voices}
        return self.levels


CREDIT = make_track(
    "bgm-credit", "f", attribution_required=True, exact_attribution_text=CREDIT_TEXT
)


def _bgm_source(tmp_path):
    cues = sheet(cue(track_id="bgm-credit", source_sha256=sha("f"), credits_text=CREDIT_TEXT))
    return FakeBgmSource(tmp_path, (CREDIT,), cues)


class Clock:
    def __call__(self):
        return NOW


def _h(session_factory, store, tmp_path, **extra) -> Harness:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Harness(session_factory, store, tmp_path, clock=Clock(), **extra)


async def _declarations(session_factory) -> dict[str, QualityUsageDeclarationRow]:
    async with session_factory() as s:
        rows = (await s.execute(select(QualityUsageDeclarationRow))).scalars().all()
    return {r.feature: r for r in rows}


async def _final(store, session_factory, episode_id: str):
    rows = await _final_rows(session_factory, episode_id)
    input_hash = (await _input_hashes(session_factory, episode_id))[-1]
    return (
        rows[-1],
        parse_final_video(await store.get_json(rows[-1].object_key)),
        input_hash,
    )


def _levels(voice=-1600, music=-3000):
    return (
        SceneSpeechLevels("s1", voice, music),
        SceneSpeechLevels("s2", voice, music),
    )


async def test_without_a_writer_nothing_is_declared(
    session_factory, artifact_store, tmp_path
) -> None:
    """flag OFF（writer なし）は宣言の行を 1 つも書かない（INV-146 / 導入前と同一）。"""
    seed = await seed_render_inputs(session_factory, artifact_store)
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        bgm_source=_bgm_source(tmp_path),
        loudness_probe=FakeLoudness(),
    )
    await _admit_and_render(h, seed)
    assert await _declarations(session_factory) == {}


async def test_with_a_writer_but_no_bgm_or_map_both_are_declared_not_used(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    await _admit_and_render(h, seed)
    rows = await _declarations(session_factory)
    final_row, _, input_hash = await _final(artifact_store, session_factory, seed.episode_id)
    assert set(rows) == {"bgm", "map"}
    assert all(not r.used and r.evidence_ref is None for r in rows.values())
    assert all(r.final_video_sha256 == final_row.sha256 for r in rows.values())
    assert all(r.input_hash == input_hash for r in rows.values())


async def test_bgm_used_is_declared_with_the_digest_of_the_evidence_at_the_binding_key(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        bgm_source=_bgm_source(tmp_path),
        loudness_probe=FakeLoudness(),
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    await _admit_and_render(h, seed)
    row, final, input_hash = await _final(artifact_store, session_factory, seed.episode_id)
    key = bgm_evidence_key(seed.episode_id, final.media.sha256, input_hash)
    payload = await artifact_store.get_json(key)
    assert BgmRenderEvidence.model_validate(payload).credit_lines == (CREDIT_TEXT,)
    rows = await _declarations(session_factory)
    assert rows["bgm"].used
    assert rows["bgm"].evidence_ref == sha256_hex(canonical_json_bytes(payload))
    assert not rows["map"].used


async def test_a_map_is_declared_used_even_when_it_was_refused(
    session_factory, artifact_store, tmp_path
) -> None:
    """拒否された地図も「使う」と宣言する。そうしないと gate が検査せず BLOCK に届かない。"""
    seed = await seed_render_inputs(session_factory, artifact_store)
    bad = sb2_spec(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),))
    geo = GeoRenderService(
        source=FakeGeoSource(data_for(bad)), encoder=FakeEncoder(), font_path=None
    )
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        geo=geo,
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    await _admit_and_render(h, seed)
    row, final, input_hash = await _final(artifact_store, session_factory, seed.episode_id)
    payload = await artifact_store.get_json(
        geo_evidence_key(seed.episode_id, final.media.sha256, input_hash)
    )
    rows = await _declarations(session_factory)
    assert rows["map"].used
    assert rows["map"].evidence_ref == sha256_hex(canonical_json_bytes(payload))


async def test_geo_on_without_specs_declares_map_not_used(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    geo = GeoRenderService(source=FakeGeoSource(None), encoder=FakeEncoder(), font_path=None)
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        geo=geo,
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    await _admit_and_render(h, seed)
    assert not (await _declarations(session_factory))["map"].used


async def test_a_failing_declaration_does_not_fail_the_render(
    session_factory, artifact_store, tmp_path, caplog
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    h = _h(session_factory, artifact_store, tmp_path, quality_writer=FailingWriter())
    with caplog.at_level("WARNING"):
        result = await _admit_and_render(h, seed)
    assert not result.skipped
    assert len(await _final_rows(session_factory, seed.episode_id)) == 1
    assert "quality usage not declared" in caplog.text
    assert "db down" not in caplog.text  # 例外の本文は出さない（型だけ）


async def test_the_declaration_is_visible_to_the_gate_repository(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        quality_writer=SqlQualityRecordWriter(session_factory),
    )
    await _admit_and_render(h, seed)
    async with session_factory() as s:
        current = await QualityRecordRepository(s).current_final_video(seed.episode_id)
        assert current is not None
        subject = CheckSubject(seed.episode_id, current.final_video_sha256, current.input_hash)
        declared = await QualityRecordRepository(s).list_usage(subject)
    assert {d.feature for d in declared} == {UsageFeature.BGM, UsageFeature.MAP}


# --- 発話余裕の実測 -----------------------------------------------------------------------


async def test_speech_margin_is_measured_and_stored_in_the_evidence(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    probe = FakeSpeechProbe(_levels())
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        bgm_source=_bgm_source(tmp_path),
        loudness_probe=FakeLoudness(),
        speech_probe=probe,
    )
    await _admit_and_render(h, seed)
    row, final, input_hash = await _final(artifact_store, session_factory, seed.episode_id)
    evidence = BgmRenderEvidence.model_validate(
        await artifact_store.get_json(
            bgm_evidence_key(seed.episode_id, final.media.sha256, input_hash)
        )
    )
    assert probe.calls == 1
    assert evidence.speech is not None and evidence.speech.passed
    assert evidence.speech.min_margin_centi_lu == 1400


async def test_a_unmeasurable_margin_keeps_the_render_and_leaves_the_evidence_unmeasured(
    session_factory, artifact_store, tmp_path, caplog
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    probe = FakeSpeechProbe(error=RenderEngineFailedError("ffmpeg died"))
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        bgm_source=_bgm_source(tmp_path),
        loudness_probe=FakeLoudness(),
        speech_probe=probe,
    )
    with caplog.at_level("WARNING"):
        await _admit_and_render(h, seed)
    row, final, input_hash = await _final(artifact_store, session_factory, seed.episode_id)
    evidence = BgmRenderEvidence.model_validate(
        await artifact_store.get_json(
            bgm_evidence_key(seed.episode_id, final.media.sha256, input_hash)
        )
    )
    assert evidence.speech is None
    assert "speech margin not measured" in caplog.text
    assert "ffmpeg died" in caplog.text  # 型名だけでは原因が分からない


async def test_too_loud_music_is_stored_as_a_failed_margin(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    probe = FakeSpeechProbe(_levels(voice=-1600, music=-1800))
    h = _h(
        session_factory,
        artifact_store,
        tmp_path,
        bgm_source=_bgm_source(tmp_path),
        loudness_probe=FakeLoudness(),
        speech_probe=probe,
    )
    await _admit_and_render(h, seed)
    row, final, input_hash = await _final(artifact_store, session_factory, seed.episode_id)
    evidence = BgmRenderEvidence.model_validate(
        await artifact_store.get_json(
            bgm_evidence_key(seed.episode_id, final.media.sha256, input_hash)
        )
    )
    assert evidence.speech is not None and not evidence.speech.passed


async def test_no_speech_probe_or_no_bgm_means_no_measurement(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    probe = FakeSpeechProbe(_levels())
    h = _h(session_factory, artifact_store, tmp_path, speech_probe=probe)  # BGM 無し
    await _admit_and_render(h, seed)
    assert probe.calls == 0
    assert await _input_hashes(session_factory, seed.episode_id)
