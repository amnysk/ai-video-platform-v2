"""Render Activity の自動選曲・梯子・BGM 無しへのフォールバック（ADR-0087 §5〜§7、INV-153〜155）。

DB はテスト用 SQLite、音源は InMemory の ArtifactStore。fal を呼ぶ経路は無いことを、
fal クライアントの全メソッドを「呼ばれたら失敗」にし、production の成果物・予約台帳の不変で示す。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from temporalio.exceptions import ApplicationError

from contracts.music import BgmTrack
from contracts.music_render import LoudnessMeasurement
from contracts.music_selection import FallbackReason, FallbackStep
from contracts.render_activities import RenderAdmitRequest
from domain.errors import MediaValidationError
from domain.music.intelligibility import SceneSpeechLevels
from infrastructure.db.bgm_repositories import SqlBgmCatalogRepository
from infrastructure.db.models import ArtifactMetadataRow, ProviderReservationRow
from infrastructure.music.db_source import DbBgmSource
from tests.support.bgm_catalog import NOW, make_track
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import WF, _final_rows, _req
from tests.unit.test_render_activities_bgm import (
    FakeLoudness,
    _admit_and_render,
    _harness,
    _input_hashes,
)

GOOD = LoudnessMeasurement(integrated_lufs_centi=-1400, true_peak_dbtp_centi=-300, lra_centi=500)
BAD = LoudnessMeasurement(integrated_lufs_centi=-7000, true_peak_dbtp_centi=None, lra_centi=0)


class SequencedLoudness:
    """呼ばれた順に測定値を返す（尽きたら最後の値）。"""

    def __init__(self, *measured: LoudnessMeasurement) -> None:
        self.measured = list(measured)
        self.calls = 0

    def measure_loudness(self, path: str) -> LoudnessMeasurement:
        value = self.measured[min(self.calls, len(self.measured) - 1)]
        self.calls += 1
        return value


class FakeSpeechProbe:
    """呼ばれた順に「最小の差（1/100 LU）」を返す。尽きたら最後の値。"""

    def __init__(self, *margins: int | None) -> None:
        self.margins = list(margins) or [1500]
        self.calls = 0

    def measure(self, plan, bgm, voice_paths) -> tuple[SceneSpeechLevels, ...]:
        margin = self.margins[min(self.calls, len(self.margins) - 1)]
        self.calls += 1
        voice = -1650
        return tuple(
            SceneSpeechLevels(v.script_scene_id, voice, None if margin is None else voice - margin)
            for v in plan.voices
        )


class CountingSource(DbBgmSource):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.prepared: list[tuple[str, int]] = []
        self.loads = 0

    async def load_for_render(self, ctx):
        self.loads += 1
        return await super().load_for_render(ctx)

    async def prepare_candidate(self, ctx, track_id, *, extra_gain_offset_db):
        self.prepared.append((track_id, extra_gain_offset_db))
        return await super().prepare_candidate(
            ctx, track_id, extra_gain_offset_db=extra_gain_offset_db
        )


def _audio(tag: str) -> tuple[bytes, str]:
    body = f"audio-{tag}".encode()
    return body, hashlib.sha256(body).hexdigest()


async def _seed_track(
    repo, store, track_id: str, *, store_original: bool = True, **overrides: Any
) -> BgmTrack:
    """承認済みの曲を DB に置き、原本を MinIO 相当に置く（sha256 は本物）。"""
    body, digest = _audio(track_id)
    base: dict[str, Any] = {
        "file_sha256": digest,
        "object_key": f"music/originals/{digest}.mp3",
        "loudness_lufs": -14.5,
        "mood_tags": ("calm",),
        "energy": "low",
        "license_approver_kind": "owner",
    }
    base.update(overrides)
    track = make_track(track_id, "a", **base)
    quarantined = track.model_copy(
        update={
            "status": "quarantined",
            "license_status": "unverified",
            "license_evidence_sha256": None,
            "license_checked_at": None,
        }
    )
    await repo.register_quarantined(
        quarantined,
        acquired_from="owner_supplied",
        acquired_at=NOW,
        evidence_object_key=None,
        analysis={},
        inferred_tags={},
    )
    await repo.promote(track, verified_at=NOW)
    if store_original:
        await store.put_bytes(track.object_key, body, "audio/mpeg")
    return track


async def _setup(session_factory, store, tmp_path, *, probe=None, loudness=None, source_cls=None):
    seed = await seed_render_inputs(session_factory, store)
    repo = SqlBgmCatalogRepository(session_factory)
    source = (source_cls or CountingSource)(repo, store)
    speech = probe or FakeSpeechProbe()
    h = _harness(session_factory, store, tmp_path / "h", source, loudness or FakeLoudness(GOOD))
    h.activities._speech_probe = speech
    return seed, repo, source, speech, h


def _spy_on_fal(monkeypatch) -> Counter[str]:
    from infrastructure.providers import fal_queue, fal_storage

    calls: Counter[str] = Counter()

    def spy(name: str):
        async def _fail(*args: object, **kwargs: object):
            calls[name] += 1
            raise AssertionError(f"fal must not be called from render: {name}")

        return _fail

    for cls in (fal_queue.FalQueueClient, fal_storage.FalStorageClient):
        for attr in ("submit", "status", "result", "download", "upload", "upload_file"):
            if hasattr(cls, attr):
                monkeypatch.setattr(cls, attr, spy(f"{cls.__name__}.{attr}"))
    return calls


async def _production_state(session_factory):
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    ArtifactMetadataRow.id,
                    ArtifactMetadataRow.artifact_type,
                    ArtifactMetadataRow.sha256,
                    ArtifactMetadataRow.input_hash,
                ).where(ArtifactMetadataRow.artifact_type != "final_video")
            )
        ).all()
        reservations = await session.scalar(
            select(func.count()).select_from(ProviderReservationRow)
        )
    return sorted(map(tuple, rows)), reservations


async def _plain_hash(session_factory, store, tmp_path, seed) -> str:
    plain = _harness(session_factory, store, tmp_path / "plain")
    await _admit_and_render(plain, seed, run="plain")
    return (await _input_hashes(session_factory, seed.episode_id))[-1]


def _levels(request) -> list[int]:
    assert request.bgm is not None
    return [c.level_db for c in request.bgm.mix.cues]


def _events(decision) -> list[tuple[str, str | None, str | None]]:
    return [
        (e.step.value, e.track_id, e.reason.value if e.reason else None) for e in decision.fallback
    ]


# --------------------------------------------------------------------------- 選曲・確定


async def test_the_first_render_selects_finalizes_and_applies_the_owner_offset(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    fal = _spy_on_fal(monkeypatch)
    seed, repo, source, speech, h = await _setup(session_factory, artifact_store, tmp_path)
    await _seed_track(repo, artifact_store, "bgm-cc0-ambient-ck61", mix_gain_offset_db=6)
    before = await _production_state(session_factory)

    await _admit_and_render(h, seed)

    # CK61 相当（loudness -14.5, +6 dB）: 全 cue が上限の -12 dB（owner が試聴で承認した混合）
    assert _levels(h.engine.requests[-1]) == [-12, -12, -12]
    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None and await repo.is_selection_finalized(seed.episode_id)
    assert decision.track_id == "bgm-cc0-ambient-ck61"
    assert decision.applied_gain_offset_db == 6
    assert decision.min_speech_margin_centi_lu == 1500
    assert decision.cue_sheet is not None and decision.cue_sheet_sha256 is not None
    assert speech.calls == 1  # 描画の前の 1 回だけ。証跡は同じ測定を使う
    assert [k for k in artifact_store._objects if "bgm_render_evidence" in k]  # type: ignore[attr-defined]
    assert fal == Counter() and await _production_state(session_factory) == before


async def test_a_retry_after_finalize_reuses_the_decision_and_the_same_input_hash(
    session_factory, artifact_store, tmp_path
) -> None:
    seed, repo, source, speech, h = await _setup(session_factory, artifact_store, tmp_path)
    await _seed_track(repo, artifact_store, "bgm-cc0-ambient-ck61", mix_gain_offset_db=6)
    h.probe.error = MediaValidationError("probe crashed after the decision was finalized")
    await h.activities.admit(RenderAdmitRequest(seed.episode_id, WF, "run-1"))
    with pytest.raises(ApplicationError):
        await h.activities.render_final_video(_req(seed))
    assert await repo.is_selection_finalized(seed.episode_id)
    assert await _final_rows(session_factory, seed.episode_id) == []
    ladder_calls = list(source.prepared)

    h.probe.error = None
    await h.activities.admit(RenderAdmitRequest(seed.episode_id, WF, "run-2"))
    await h.activities.render_final_video(_req(seed, run="run-2"))
    assert source.prepared == ladder_calls  # 梯子は走らない（保存した決定を使う。INV-153）
    first_request, second_request = h.engine.requests[0], h.engine.requests[-1]
    assert first_request.bgm is not None and second_request.bgm is not None
    assert first_request.bgm.mix.identity_payload() == second_request.bgm.mix.identity_payload()

    # 3 回目: final_video が既にある -> 描画も選曲もしない（render skip）
    count = len(h.engine.requests)
    again = await _admit_and_render(h, seed, run="run-3")
    assert again.skipped and len(h.engine.requests) == count
    assert source.prepared == ladder_calls
    hashes = await _input_hashes(session_factory, seed.episode_id)
    assert len(hashes) == 1


# --------------------------------------------------------------------------- 梯子


async def test_too_small_a_margin_attenuates_once_then_passes(
    session_factory, artifact_store, tmp_path
) -> None:
    seed, repo, source, speech, h = await _setup(
        session_factory, artifact_store, tmp_path, probe=FakeSpeechProbe(1100, 1300)
    )
    await _seed_track(repo, artifact_store, "bgm-a", mix_gain_offset_db=6)
    await _admit_and_render(h, seed)
    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None
    assert [(t, g) for t, g in source.prepared] == [("bgm-a", 0), ("bgm-a", -3)]
    assert decision.track_id == "bgm-a" and decision.applied_gain_offset_db == 3
    assert (FallbackStep.GAIN_REDUCED.value, "bgm-a", "speech_margin_too_small") in _events(
        decision
    )
    assert decision.min_speech_margin_centi_lu == 1300
    assert speech.calls == 2
    # 3 dB 下がった分だけ、頭打ちだった cue が下がる（intro -12、body は -15）
    assert _levels(h.engine.requests[-1]) == [-12, -15, -13]


async def test_when_a_track_cannot_pass_the_next_ranked_track_is_tried_once(
    session_factory, artifact_store, tmp_path
) -> None:
    seed, repo, source, speech, h = await _setup(
        session_factory, artifact_store, tmp_path, probe=FakeSpeechProbe(900, 900, 1500)
    )
    await _seed_track(repo, artifact_store, "bgm-a", mood_tags=("calm",))
    await _seed_track(repo, artifact_store, "bgm-b", mood_tags=("calm",))
    await _seed_track(repo, artifact_store, "bgm-c", mood_tags=("calm",))
    await _admit_and_render(h, seed)
    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None and decision.track_id == "bgm-b"
    tried = {t for t, _ in source.prepared}
    assert tried == {"bgm-a", "bgm-b"}  # 最大 FALLBACK_MAX_TRACKS 曲。3 曲目には進まない
    assert ("next_track", "bgm-a", "speech_margin_too_small") in _events(decision)


async def test_when_every_track_fails_the_video_has_no_bgm_and_says_so(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    fal = _spy_on_fal(monkeypatch)
    seed, repo, source, speech, h = await _setup(
        session_factory, artifact_store, tmp_path, probe=FakeSpeechProbe(800)
    )
    await _seed_track(repo, artifact_store, "bgm-a")
    await _seed_track(repo, artifact_store, "bgm-b")
    expected = await _plain_hash(session_factory, artifact_store, tmp_path, seed)
    before = await _production_state(session_factory)
    # 既存の BGM 無し動画を消せないので、別 Episode ではなく同じ Episode で hash が一致するか見る
    again = await _admit_and_render(h, seed, run="run-2")

    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None and decision.track_id is None and decision.cue_sheet is None
    assert decision.fallback[-1].step is FallbackStep.NO_BGM
    assert decision.fallback[-1].reason is FallbackReason.SPEECH_MARGIN_TOO_SMALL
    assert await repo.is_selection_finalized(seed.episode_id)
    # BGM 無しの識別子は導入前と同じ hash（= 現行の BGM 無し動画を再利用する）
    assert again.skipped and (await _input_hashes(session_factory, seed.episode_id)) == [expected]
    assert fal == Counter() and await _production_state(session_factory) == before
    assert len(source.prepared) == 4  # 2 曲 x (通常 + 減衰)


async def test_an_unreadable_or_altered_original_falls_to_the_next_track(
    session_factory, artifact_store, tmp_path
) -> None:
    seed, repo, source, speech, h = await _setup(session_factory, artifact_store, tmp_path)
    await _seed_track(repo, artifact_store, "bgm-a", store_original=False)  # 原本が MinIO に無い
    await _seed_track(repo, artifact_store, "bgm-b")
    await _admit_and_render(h, seed)
    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None and decision.track_id == "bgm-b"
    assert ("next_track", "bgm-a", "source_unreadable") in _events(decision)


async def test_an_original_with_the_wrong_sha256_is_never_mixed(
    session_factory, artifact_store, tmp_path
) -> None:
    seed, repo, source, speech, h = await _setup(session_factory, artifact_store, tmp_path)
    track = await _seed_track(repo, artifact_store, "bgm-a", store_original=False)
    await artifact_store.put_bytes(track.object_key, b"tampered", "audio/mpeg")
    await _admit_and_render(h, seed)
    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None and decision.track_id is None
    assert h.engine.requests[-1].bgm is None
    assert ("next_track", "bgm-a", "source_unreadable") in _events(decision)


async def test_an_expired_track_is_rejected_at_the_recheck_before_rendering(
    session_factory, artifact_store, tmp_path
) -> None:
    from domain.render.ports import BgmRenderContext
    from tests.support.bgm_render import placement_plan

    seed, repo, source, speech, h = await _setup(session_factory, artifact_store, tmp_path)
    await _seed_track(repo, artifact_store, "bgm-a", valid_until=NOW + timedelta(seconds=1))

    def ctx(now) -> BgmRenderContext:
        return BgmRenderContext(
            episode_id=seed.episode_id,
            plan=placement_plan(),
            script=None,  # type: ignore[arg-type]  (prepare_candidate は台本を読まない)
            storyboard=None,  # type: ignore[arg-type]
            work_dir=tmp_path / "wd",
            now=now,
        )

    ok = await source.prepare_candidate(ctx(NOW), "bgm-a", extra_gain_offset_db=0)
    assert ok.failure is None and ok.data is not None
    # 選曲の後に失効した曲は、描画の直前の再検査で落ちる（次の曲 / BGM 無しへ）
    late = await source.prepare_candidate(
        ctx(NOW + timedelta(seconds=5)), "bgm-a", extra_gain_offset_db=0
    )
    assert late.failure is FallbackReason.LICENSE_RECHECK_FAILED and late.data is None


async def test_a_finalized_decision_is_rechecked_before_it_is_reused(
    session_factory, artifact_store, tmp_path
) -> None:
    """F3（独立レビュー）: 確定済みの決定を再利用する経路も、描画直前の門を通る。"""
    from domain.render.ports import BgmRenderContext
    from tests.support.bgm_render import placement_plan

    seed, repo, source, speech, h = await _setup(session_factory, artifact_store, tmp_path)
    await _seed_track(repo, artifact_store, "bgm-a", valid_until=NOW + timedelta(hours=1))
    await _admit_and_render(h, seed, run="run-1")
    assert await repo.is_selection_finalized(seed.episode_id)

    def ctx(now) -> BgmRenderContext:
        return BgmRenderContext(
            episode_id=seed.episode_id,
            plan=placement_plan(),
            script=None,  # type: ignore[arg-type]  (確定済みの経路は台本を読まない)
            storyboard=None,  # type: ignore[arg-type]
            work_dir=tmp_path / "wd2",
            now=now,
        )

    fresh = await source.load_for_render(ctx(NOW))
    assert fresh.finalized and fresh.data is not None and fresh.data.cue_sheet is not None
    late = await source.load_for_render(ctx(NOW + timedelta(hours=2)))
    assert late.finalized and late.data is not None and late.data.cue_sheet is None


# --------------------------------------------------------------------------- 候補なし・故障


async def test_no_approved_track_renders_without_bgm_and_queues_a_demand_without_waiting(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    fal = _spy_on_fal(monkeypatch)
    seed, repo, source, speech, h = await _setup(session_factory, artifact_store, tmp_path)
    await _admit_and_render(h, seed)
    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None and decision.track_id is None and decision.acquisition_requested
    assert await repo.is_selection_finalized(seed.episode_id)
    assert h.engine.requests[-1].bgm is None and speech.calls == 0 and source.prepared == []
    pending = await repo.pending_demands(limit=5)
    assert [d for d, _ in pending] == [decision.requirements_sha256]
    assert fal == Counter()
    assert not [k for k in artifact_store._objects if "bgm_render_evidence" in k]  # type: ignore[attr-defined]
    # BGM 無しの hash は導入前と同じ
    assert (await _input_hashes(session_factory, seed.episode_id)) == [
        await _plain_hash(session_factory, artifact_store, tmp_path, seed)
    ]


async def test_a_selection_database_failure_renders_without_bgm_and_does_not_finalize(
    session_factory, artifact_store, tmp_path
) -> None:
    class Broken(CountingSource):
        async def load_for_render(self, ctx):
            raise OperationalError("select", {}, Exception("db down"))

    seed, repo, source, speech, h = await _setup(
        session_factory, artifact_store, tmp_path, source_cls=Broken
    )
    await _seed_track(repo, artifact_store, "bgm-a")
    await _admit_and_render(h, seed)
    assert h.engine.requests[-1].bgm is None
    assert await repo.get_selection(seed.episode_id) is None


async def test_unreadable_selection_for_the_brand_mixes_nothing_when_music_is_disabled(
    session_factory, artifact_store, tmp_path
) -> None:
    from contracts.music_selection import BrandMusicPolicy

    seed = await seed_render_inputs(session_factory, artifact_store)
    repo = SqlBgmCatalogRepository(session_factory)
    await _seed_track(repo, artifact_store, "bgm-a")
    source = DbBgmSource(
        repo, artifact_store, brand=BrandMusicPolicy(brand_id="quiet", music_enabled=False)
    )
    h = _harness(session_factory, artifact_store, tmp_path / "h", source, FakeLoudness(GOOD))
    await _admit_and_render(h, seed)
    decision = await repo.get_selection(seed.episode_id)
    assert (
        decision is not None and decision.fallback[0].reason is FallbackReason.BRAND_MUSIC_DISABLED
    )
    assert not decision.acquisition_requested and h.engine.requests[-1].bgm is None


# --------------------------------------------------------------------------- 完成動画のラウドネス


async def test_final_loudness_out_of_range_rerenders_once_without_bgm_and_records_why(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    fal = _spy_on_fal(monkeypatch)
    # 1 回目（BGM 入り）は範囲外、2 回目（BGM 無し）は範囲内
    seed, repo, source, speech, h = await _setup(
        session_factory, artifact_store, tmp_path, loudness=SequencedLoudness(BAD, GOOD)
    )
    await _seed_track(repo, artifact_store, "bgm-a")
    before = await _production_state(session_factory)
    await _admit_and_render(h, seed)

    assert len(h.engine.requests) == 2
    assert h.engine.requests[0].bgm is not None and h.engine.requests[1].bgm is None
    decision = await repo.get_selection(seed.episode_id)
    assert decision is not None and decision.track_id is None and decision.cue_sheet is None
    assert decision.fallback[-1].step is FallbackStep.NO_BGM_AFTER_LOUDNESS
    assert decision.fallback[-1].reason is FallbackReason.FINAL_LOUDNESS_OUT_OF_RANGE
    # 保存された動画は BGM 無し: 入力 hash に BGM の識別子が無く、証跡も無い
    assert (await _input_hashes(session_factory, seed.episode_id)) == [
        await _plain_hash(session_factory, artifact_store, tmp_path, seed)
    ]
    assert not [k for k in artifact_store._objects if "bgm_render_evidence" in k]  # type: ignore[attr-defined]
    assert fal == Counter() and await _production_state(session_factory) == before


async def test_when_the_bgm_less_video_is_also_out_of_range_it_is_still_needs_input(
    session_factory, artifact_store, tmp_path
) -> None:
    seed, repo, source, speech, h = await _setup(
        session_factory, artifact_store, tmp_path, loudness=SequencedLoudness(BAD)
    )
    await _seed_track(repo, artifact_store, "bgm-a")
    await h.activities.admit(RenderAdmitRequest(seed.episode_id, WF, "run-1"))
    with pytest.raises(ApplicationError) as err:
        await h.activities.render_final_video(_req(seed))
    assert err.value.type == "FinalVideoValidationError" and "bgm_loudness" in str(err.value)
    assert len(h.engine.requests) == 2  # 上限つき: BGM 入り 1 回 + BGM 無し 1 回
    assert await _final_rows(session_factory, seed.episode_id) == []
    assert not [k for k in artifact_store._objects if "bgm_render_evidence" in k]  # type: ignore[attr-defined]
