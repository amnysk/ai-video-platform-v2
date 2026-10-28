"""Render Activity の BGM 結線（ADR-0077 / INV-113〜116）。SQLite + メモリストア + fake。

fal を呼ぶ経路は Render に無いことを、fal クライアントの全メソッドを「呼ばれたら失敗」にした上で
予約台帳・production の Artifact・job の種類が変わらないことで示す（INV-116）。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from contracts.artifacts import parse_final_video
from contracts.music import BgmTrack, MusicCueSheet
from contracts.music_render import LoudnessMeasurement
from contracts.render_activities import RenderAdmitRequest
from contracts.states import ArtifactType, JobType
from domain.render.ports import BgmSourceData, BgmSourceError
from infrastructure.db.models import ArtifactMetadataRow, ProviderReservationRow
from infrastructure.db.repositories import ArtifactMetadataRepository, JobRepository
from tests.support.bgm_catalog import NOW, make_track, sha
from tests.support.bgm_render import CREDIT_TEXT, cue, sheet
from tests.support.render_activity import seed_render_inputs
from tests.unit.test_render_activities import WF, Harness, _final_rows, _req

GOOD_LOUDNESS = LoudnessMeasurement(
    integrated_lufs_centi=-1400, true_peak_dbtp_centi=-300, lra_centi=500
)


class FakeBgmSource:
    def __init__(
        self, tmp_path: Path, tracks: tuple[BgmTrack, ...], cue_sheet: MusicCueSheet | None
    ):
        self.tracks = tracks
        self.cue_sheet = cue_sheet
        self.error: BaseException | None = None
        self.calls = 0
        self._dir = tmp_path / "bgm-files"
        self._dir.mkdir(exist_ok=True)

    def load(self, episode_id: str) -> BgmSourceData:
        self.calls += 1
        if self.error is not None:
            raise self.error
        paths = {}
        for t in self.tracks:
            p = self._dir / f"{t.track_id}.bin"
            p.write_bytes(b"audio")
            paths[t.track_id] = p
        return BgmSourceData(
            cue_sheet=self.cue_sheet,
            catalog=self.tracks,
            track_paths=paths,
            file_sha256_by_track={t.track_id: t.file_sha256 for t in self.tracks},
        )


class FakeLoudness:
    def __init__(self, measured: LoudnessMeasurement = GOOD_LOUDNESS) -> None:
        self.measured = measured
        self.calls = 0

    def measure_loudness(self, path: str) -> LoudnessMeasurement:
        self.calls += 1
        return self.measured


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


def _harness(session_factory, store, tmp_path, source=None, loudness=None, clock=None) -> Harness:
    tmp_path.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {}
    if source is not None:
        extra = {"bgm_source": source, "loudness_probe": loudness or FakeLoudness()}
    extra["clock"] = clock or Clock()
    return Harness(session_factory, store, tmp_path, **extra)


async def _input_hashes(session_factory, episode_id: str) -> list[str]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(ArtifactMetadataRow)
            .where(
                ArtifactMetadataRow.episode_id == _uuid(episode_id),
                ArtifactMetadataRow.artifact_type == ArtifactType.FINAL_VIDEO.value,
            )
            .order_by(ArtifactMetadataRow.created_at, ArtifactMetadataRow.id)
        )
        return [r.input_hash for r in rows]


def _uuid(value: str):
    import uuid

    return uuid.UUID(value)


async def _admit_and_render(h: Harness, seed, run: str = "run-1"):
    await h.activities.admit(RenderAdmitRequest(seed.episode_id, WF, run))
    return await h.activities.render_final_video(_req(seed, run=run))


# --------------------------------------------------------------------------- INV-113


async def test_no_cue_sheet_renders_exactly_like_no_bgm_support(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    off = _harness(session_factory, artifact_store, tmp_path / "off")
    await _admit_and_render(off, seed)
    expected = await _input_hashes(session_factory, seed.episode_id)

    source = FakeBgmSource(tmp_path, (make_track(),), None)
    loudness = FakeLoudness()
    on = _harness(session_factory, artifact_store, tmp_path / "on", source, loudness)
    again = await _admit_and_render(on, seed, run="run-2")

    # 設定 ON でも cue sheet が無ければ同じ入力 hash になり、現行の final_video が再利用される
    assert source.calls == 1 and loudness.calls == 0
    assert again.skipped and on.engine.requests == []
    assert await _input_hashes(session_factory, seed.episode_id) == expected


# --------------------------------------------------------------------------- 適用・証跡


async def test_bgm_changes_identity_and_stores_evidence_credits_and_loudness(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    plain = _harness(session_factory, artifact_store, tmp_path / "plain")
    await _admit_and_render(plain, seed)
    plain_hash = (await _input_hashes(session_factory, seed.episode_id))[0]

    track = make_track(
        "bgm-credit", "f", attribution_required=True, exact_attribution_text=CREDIT_TEXT
    )
    cues = sheet(cue(track_id="bgm-credit", source_sha256=sha("f"), credits_text=CREDIT_TEXT))
    source = FakeBgmSource(tmp_path, (track,), cues)
    h = _harness(session_factory, artifact_store, tmp_path / "bgm", source)
    result = await _admit_and_render(h, seed, run="run-2")

    assert not result.skipped and result.version == 2
    hashes = await _input_hashes(session_factory, seed.episode_id)
    assert hashes[0] == plain_hash and hashes[1] != plain_hash
    request = h.engine.requests[0]
    assert request.bgm is not None and request.bgm.mix.cues[0].level_db == -18

    rows = await _final_rows(session_factory, seed.episode_id)
    final = parse_final_video(await artifact_store.get_json(rows[-1].object_key))
    checks = {c.check: c for c in final.technical_qa.checks}
    assert checks["bgm_loudness"].passed and "ok" in checks["bgm_loudness"].detail

    evidence = await _evidence(artifact_store, seed.episode_id)
    assert evidence["final_video_sha256"] == final.media.sha256
    assert evidence["input_hash"] == hashes[1]
    assert evidence["credit_lines"] == [CREDIT_TEXT]
    assert evidence["tracks"] == [
        {"track_id": "bgm-credit", "source_sha256": sha("f"), "license_evidence_hash": sha("e")}
    ]
    assert evidence["loudness"]["reason_code"] == "ok"


async def _evidence(store, episode_id: str) -> dict[str, Any]:
    prefix = f"artifacts/{episode_id}/bgm_render_evidence/"
    keys = [k for k in store._objects if k.startswith(prefix)]  # type: ignore[attr-defined]
    assert len(keys) == 1, keys
    return await store.get_json(keys[0])


# --------------------------------------------------------------------------- INV-114


async def test_expired_or_unknown_license_renders_without_bgm_and_logs_the_reason(
    session_factory, artifact_store, tmp_path, caplog
) -> None:
    for i, track in enumerate(
        (
            make_track("bgm-approved", "a", license_status="expired"),
            make_track(
                "bgm-approved",
                "a",
                license_status="unverified",
                license_evidence_sha256=None,
                license_checked_at=None,
            ),
            make_track("bgm-approved", "a", valid_until=NOW - timedelta(seconds=1)),
        )
    ):
        seed = await seed_render_inputs(session_factory, artifact_store)
        (tmp_path / str(i)).mkdir()
        source = FakeBgmSource(tmp_path / str(i), (track,), sheet())
        loudness = FakeLoudness()
        h = _harness(session_factory, artifact_store, tmp_path / f"h{i}", source, loudness)
        with caplog.at_level(logging.WARNING):
            await _admit_and_render(h, seed)
        assert h.engine.requests[0].bgm is None and loudness.calls == 0
        assert "bgm not used" in caplog.text and "license_rejected" in caplog.text
        assert not [k for k in artifact_store._objects if "bgm_render_evidence" in k]  # type: ignore[attr-defined]


async def test_license_rejected_render_has_the_same_identity_as_no_bgm(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    plain = _harness(session_factory, artifact_store, tmp_path / "plain")
    await _admit_and_render(plain, seed)
    # 同じ Episode・同じ入力: BGM を断られた描画は BGM 無しの現行 final_video を再利用する
    source = FakeBgmSource(tmp_path, (make_track(license_status="revoked"),), sheet())
    h = _harness(session_factory, artifact_store, tmp_path / "h", source)
    again = await _admit_and_render(h, seed, run="run-2")
    assert again.skipped and h.engine.requests == []


async def test_a_track_that_expires_after_a_bgm_render_is_replaced_by_a_bgm_less_render(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    track = make_track(valid_until=NOW + timedelta(hours=1))
    clock = Clock()
    source = FakeBgmSource(tmp_path, (track,), sheet())
    h = _harness(session_factory, artifact_store, tmp_path / "h", source, clock=clock)
    first = await _admit_and_render(h, seed)
    assert h.engine.requests[0].bgm is not None

    clock.now = NOW + timedelta(hours=1)  # 失効
    second = await _admit_and_render(h, seed, run="run-2")

    assert not second.skipped and second.version == first.version + 1
    assert h.engine.requests[1].bgm is None


async def test_unreadable_bgm_source_renders_without_bgm(
    session_factory, artifact_store, tmp_path
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    source = FakeBgmSource(tmp_path, (make_track(),), sheet())
    source.error = BgmSourceError("broken catalog")
    h = _harness(session_factory, artifact_store, tmp_path / "h", source)
    await _admit_and_render(h, seed)
    assert h.engine.requests[0].bgm is None


# --------------------------------------------------------------------------- INV-115


@pytest.mark.parametrize(
    "measured",
    [
        LoudnessMeasurement(integrated_lufs_centi=-600, true_peak_dbtp_centi=-300, lra_centi=1),
        LoudnessMeasurement(integrated_lufs_centi=-3000, true_peak_dbtp_centi=-300, lra_centi=1),
        LoudnessMeasurement(integrated_lufs_centi=-1400, true_peak_dbtp_centi=0, lra_centi=1),
        LoudnessMeasurement(integrated_lufs_centi=-7000, true_peak_dbtp_centi=None, lra_centi=0),
    ],
)
async def test_out_of_range_loudness_is_a_structured_failure_and_nothing_is_stored(
    session_factory, artifact_store, tmp_path, measured: LoudnessMeasurement
) -> None:
    seed = await seed_render_inputs(session_factory, artifact_store)
    source = FakeBgmSource(tmp_path, (make_track(),), sheet())
    h = _harness(session_factory, artifact_store, tmp_path / "h", source, FakeLoudness(measured))
    await h.activities.admit(RenderAdmitRequest(seed.episode_id, WF, "run-1"))
    with pytest.raises(ApplicationError) as err:
        await h.activities.render_final_video(_req(seed))
    assert err.value.type == "FinalVideoValidationError"
    assert "bgm_loudness" in str(err.value)
    assert await _final_rows(session_factory, seed.episode_id) == []
    assert not [k for k in artifact_store._objects if "bgm_render_evidence" in k]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- INV-116


async def test_bgm_only_changes_rerender_only_and_never_touch_production_or_fal(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    from infrastructure.providers import fal_queue, fal_storage

    fal_calls: list[str] = []

    def spy(name: str):
        async def _fail(*args: object, **kwargs: object):
            fal_calls.append(name)
            raise AssertionError(f"fal must not be called from render: {name}")

        return _fail

    for cls in (fal_queue.FalQueueClient, fal_storage.FalStorageClient):
        for attr in ("submit", "status", "result", "download", "upload", "upload_file"):
            if hasattr(cls, attr):
                monkeypatch.setattr(cls, attr, spy(f"{cls.__name__}.{attr}"))

    seed = await seed_render_inputs(session_factory, artifact_store)
    plain = _harness(session_factory, artifact_store, tmp_path / "plain")
    await _admit_and_render(plain, seed)

    async def production_state():
        async with session_factory() as session:
            artifacts = [
                (a.id, a.artifact_type.value, a.version, a.sha256)
                for a in await ArtifactMetadataRepository(session).list_for_episode(seed.episode_id)
                if a.artifact_type is not ArtifactType.FINAL_VIDEO
            ]
            reservations = await session.scalar(
                select(func.count()).select_from(ProviderReservationRow)
            )
            jobs = [j.type for j in await JobRepository(session).list_for_episode(seed.episode_id)]
        return artifacts, reservations, jobs

    before = await production_state()
    source = FakeBgmSource(tmp_path, (make_track(),), sheet())
    h = _harness(session_factory, artifact_store, tmp_path / "bgm", source)
    h.engine.payload = b"video-with-bgm-a"  # 実エンジンは音声が変われば出力も変わる
    first = await _admit_and_render(h, seed, run="run-2")
    h.engine.payload = b"video-with-bgm-b"
    source.cue_sheet = sheet(cue(target_relative_level_db=-24))  # BGM だけの変更
    second = await _admit_and_render(h, seed, run="run-3")
    third = await _admit_and_render(h, seed, run="run-4")  # 変更なし = 再利用

    assert not first.skipped and not second.skipped and third.skipped
    assert len({first.artifact_id, second.artifact_id}) == 2
    artifacts, reservations, jobs = await production_state()
    assert (artifacts, reservations) == (before[0], before[1]) and reservations == 0
    assert set(jobs) == {JobType.RENDER_FINAL_VIDEO}
    assert fal_calls == []  # count == 0
    assert len(h.engine.requests) == 2  # 描画したのは render だけ（2 回）
    hashes = await _input_hashes(session_factory, seed.episode_id)
    assert len(set(hashes)) == 3
