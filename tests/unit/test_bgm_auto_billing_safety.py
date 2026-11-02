"""BGM の再描画・gate の拒否・投稿の再試行は、fal の予約台帳を増やさない（INV-137 / INV-158）。

ledger（``provider_reservations``）と workflow の層で見る。描画の中身は別のテスト
（``test_bgm_only_change.py`` / 描画のフォールバックの枝）が持つ。ここは、BGM の描画を何度回しても・
gate が拒否しても・投稿を再試行しても、台帳に増えるのは YouTube の投稿の予約だけで、fal の呼び出しは
0 回、であること。
"""

from __future__ import annotations

from sqlalchemy import select

from contracts.render_activities import RenderMarkReadyRequest
from contracts.states import ProviderCall
from contracts.upload_activities import UploadAdmitRequest, UploadFinalVideoRequest
from domain.music.placement import plan_cue_sheet
from infrastructure.db.models import ProviderReservationRow
from infrastructure.workdir import WorkDirectory
from tests.support.bgm_catalog import make_track
from tests.support.fake_youtube import FakeVideoUploader
from tests.support.render_activity import seed_render_inputs
from tests.support.upload import CHANNEL_ID, TEST_CHUNK_BYTES
from tests.unit.test_bgm_only_change import LOUD, _spy_on_fal
from tests.unit.test_render_activities_bgm import WF as RENDER_WF
from tests.unit.test_render_activities_bgm import FakeBgmSource, _admit_and_render, _harness
from tests.unit.test_upload_path_quality_gate import REFUSALS, StubService
from workers.upload.activities import UploadActivities

WF = "episode-x-upload"


async def _ledger(session_factory) -> list[tuple[str, str, int]]:
    async with session_factory() as session:
        rows = (await session.scalars(select(ProviderReservationRow))).all()
    return sorted((str(r.provider), str(r.status), r.round) for r in rows)


def _upload_acts(session_factory, store, tmp_path, fake, service) -> UploadActivities:
    acts = UploadActivities(
        session_factory=session_factory,
        store=store,
        bucket="artifacts",
        workdir=WorkDirectory(tmp_path / "upload-work", forbidden=()),
        uploader=fake,
        channel_id=CHANNEL_ID,
        chunk_bytes=TEST_CHUNK_BYTES,
        marker_lookup_attempts=2,
        marker_lookup_delay_seconds=0.0,
        transient_backoff_seconds=0.0,
        expiry_confirm_delay_seconds=0.0,
        heartbeat=lambda *_: None,
        quality_enforce=True,
        quality_service=service,
    )
    acts.heartbeat_interval_seconds = 0.05
    return acts


async def test_bgm_rerenders_gate_refusals_and_upload_retries_never_grow_the_fal_ledger(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    import pytest

    fal_calls = _spy_on_fal(monkeypatch)
    seed = await seed_render_inputs(session_factory, artifact_store)
    plain = _harness(session_factory, artifact_store, tmp_path / "plain")
    await _admit_and_render(plain, seed)
    plan = plain.engine.requests[-1].plan
    assert await _ledger(session_factory) == []  # 描画だけでは台帳に何も足されない

    track_a, track_b = make_track("bgm-a", "a", **LOUD), make_track("bgm-b", "b", **LOUD)
    source = FakeBgmSource(tmp_path, (track_a, track_b), None)
    h = _harness(session_factory, artifact_store, tmp_path / "bgm", source)
    for i, track in enumerate((track_a, track_b, track_a), start=2):
        result = plan_cue_sheet(plan, track)
        assert result.cue_sheet is not None
        source.cue_sheet = result.cue_sheet
        h.engine.payload = f"video-with-bgm-{i}".encode()
        await _admit_and_render(h, seed, run=f"run-{i}")
    assert await _ledger(session_factory) == []

    ready = await h.activities.mark_ready(
        RenderMarkReadyRequest(seed.episode_id, RENDER_WF, "run-1")
    )
    assert ready.status == "render_ready"

    fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
    for reason in REFUSALS:  # gate が拒否するたびに再試行しても、台帳は 0 行のまま
        acts = _upload_acts(
            session_factory, artifact_store, tmp_path, fake, StubService(False, reason)
        )
        await acts.admit(UploadAdmitRequest(seed.episode_id, WF, "run-1"))
        with pytest.raises(Exception):  # noqa: B017, PT011 - ApplicationError
            await acts.upload_final_video(UploadFinalVideoRequest(seed.episode_id, WF, "run-1"))
    assert await _ledger(session_factory) == [] and fake.sessions_started == 0

    from contracts.quality import QualityReason

    ok = _upload_acts(
        session_factory,
        artifact_store,
        tmp_path,
        fake,
        StubService(True, QualityReason.UPLOAD_ALLOWED),
    )
    await ok.admit(UploadAdmitRequest(seed.episode_id, WF, "run-1"))
    for _ in range(3):  # 成功と再配送
        await ok.upload_final_video(UploadFinalVideoRequest(seed.episode_id, WF, "run-1"))

    ledger = await _ledger(session_factory)
    assert [p for p, _s, _r in ledger] == [str(ProviderCall.YOUTUBE_UPLOAD)]  # fal の行は 0
    assert fake.videos_created == 1
    assert sum(fal_calls.values()) == 0
