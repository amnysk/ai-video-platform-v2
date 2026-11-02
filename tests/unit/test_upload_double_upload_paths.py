"""どの経路でも、投稿済み・送信済みの Episode は 2 本目にならない（ADR-0087 §9 / INV-158）。

予約台帳（spent / dispatched）・マーカー・upload key（``compute_upload_key``）が、品質 gate が
PASS でも止める。YouTube の fake の ``videos_created`` と ``sessions_started`` が増えないこと。
"""

from __future__ import annotations

import logging

import pytest
from temporalio.exceptions import ApplicationError

from contracts.pipeline import UploadGateRequest
from contracts.quality import QualityReason
from contracts.states import ReservationStatus
from contracts.upload import upload_marker
from contracts.upload_activities import UploadAwaitProcessingRequest, UploadMarkUploadedRequest
from infrastructure.db.repositories import ProviderReservationRepository
from tests.support.upload import TEST_CHUNK_BYTES, Crash, CrashingUploader
from tests.unit import test_upload_path_quality_gate as _paths
from tests.unit.test_quality_gate_persisted_wiring import NOW, World
from tests.unit.test_upload_path_quality_gate import Rig, StubService
from workers.pipeline.activities import PipelineActivities
from workers.upload.activities import OPERATOR_REUPLOAD_APPROVED

# 共有の fixture を再束縛して pytest に見せる
factory = _paths.factory
world = _paths.world
rig = _paths.rig

PASS = (True, QualityReason.UPLOAD_ALLOWED)
REFUSE = (False, QualityReason.REPORT_BLOCKED)


async def _uploaded_once(rig: Rig, world: World):
    await world.declare()
    acts = rig.acts(enforce=True, service=world.service())
    assert (await rig.admit(acts)).admitted
    result = await rig.upload(acts)
    assert rig.fake.videos_created == 1
    return acts, result


async def test_a_retried_activity_after_a_successful_upload_makes_no_second_video(
    rig: Rig, world: World
) -> None:
    acts, first = await _uploaded_once(rig, world)
    sessions = rig.fake.sessions_started
    again = await rig.upload(acts)  # Temporal の retry / 再配送を模す
    assert again.video_id == first.video_id
    assert rig.fake.videos_created == 1 and rig.fake.sessions_started == sessions
    (reservation,) = await rig.reservations()
    assert reservation.status is ReservationStatus.SPENT


async def test_a_spent_reservation_blocks_a_second_upload_even_when_the_gate_now_refuses(
    rig: Rig, world: World
) -> None:
    acts, first = await _uploaded_once(rig, world)
    refusing = rig.acts(enforce=True, service=StubService(*REFUSE))
    again = await rig.upload(refusing)  # YouTube を呼ばずに既存の受領を返す
    assert again.video_id == first.video_id and rig.fake.videos_created == 1


async def test_a_lost_response_then_a_retry_never_makes_a_second_video(
    rig: Rig, world: World
) -> None:
    """動画は作られたが応答の前に worker が落ちた。再実行はマーカーで見つけ、2 本目を作らない。"""
    await world.declare()
    crashing = CrashingUploader(rig.fake, crash_on="after_complete")
    acts = rig.acts(enforce=True, service=world.service(), uploader=crashing)
    assert (await rig.admit(acts)).admitted
    with pytest.raises(Crash):
        await rig.upload(acts)
    assert rig.fake.videos_created == 1
    retry = rig.acts(enforce=True, service=world.service())
    await rig.admit(retry, run="run-2")
    sessions = rig.fake.sessions_started
    adopted = await rig.upload(retry, run="run-2")  # 照会 / マーカーで既存の動画を見つける
    assert adopted.video_id in rig.fake.videos
    assert rig.fake.videos_created == 1 and rig.fake.sessions_started == sessions


async def test_a_dispatched_reservation_without_a_result_holds_even_with_a_passing_gate(
    rig: Rig, world: World
) -> None:
    await world.declare()
    acts = rig.acts(enforce=True, service=world.service())
    assert (await rig.admit(acts)).admitted
    rig.fake.expire_session_at_offset = 2 * TEST_CHUNK_BYTES
    with pytest.raises(ApplicationError) as hold:
        await rig.upload(acts)
    assert hold.value.type == "UploadOutcomeUnknownError"
    rig.fake.expire_session_at_offset = None
    created = rig.fake.videos_created
    for run in ("run-1", "run-1"):
        with pytest.raises(ApplicationError):
            await rig.upload(rig.acts(enforce=True, service=world.service()), run=run)
    # 再実行は同じ session の続き（または保留）で、新しい投稿の round を開かない
    assert rig.fake.videos_created <= created + 1
    rounds = [r.round for r in await rig.reservations()]
    assert rounds == [1]


async def test_an_existing_marker_video_is_adopted_not_uploaded_again(
    rig: Rig, world: World
) -> None:
    await world.declare()
    acts = rig.acts(enforce=True, service=world.service())
    assert (await rig.admit(acts)).admitted
    rig.fake.expire_session_at_offset = 2 * TEST_CHUNK_BYTES
    with pytest.raises(ApplicationError):
        await rig.upload(acts)
    rig.fake.expire_session_at_offset = None
    (row,) = [r for r in await rig.reservations() if r.status is ReservationStatus.RESERVED]
    async with world.factory() as s:
        await ProviderReservationRepository(s).abandon(
            row.id, reconciled_by=OPERATOR_REUPLOAD_APPROVED
        )
        await s.commit()
    existing = rig.fake.add_existing_video([upload_marker(row.idempotency_key)])
    sessions, videos = rig.fake.sessions_started, rig.fake.videos_created
    result = await rig.upload(rig.acts(enforce=True, service=world.service()))
    assert result.video_id == existing
    assert rig.fake.sessions_started == sessions and rig.fake.videos_created == videos


async def test_an_uploaded_episode_is_not_admitted_again(rig: Rig, world: World) -> None:
    """過去の Episode の再投稿: ``uploaded`` は UploadWorkflow の入場で止まり upload に届かない。"""
    acts, first = await _uploaded_once(rig, world)
    await acts.await_processing(
        UploadAwaitProcessingRequest(world.episode_id, _paths.WF, "run-1", first.video_id)
    )
    await acts.mark_uploaded(UploadMarkUploadedRequest(world.episode_id, _paths.WF, "run-1"))
    sessions = rig.fake.sessions_started
    admit = await rig.admit(acts, run="run-9")
    assert not admit.admitted and admit.status == "uploaded"
    assert rig.fake.sessions_started == sessions


# --- 自動の門（upload_gate）も spent / dispatched を先に止める -----------------------------------


async def test_upload_gate_refuses_a_spent_reservation_before_the_quality_service(
    rig: Rig, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    await _uploaded_once(rig, world)
    stub = StubService(*PASS)
    pipeline = PipelineActivities(
        session_factory=world.factory,
        paused_env=False,
        uploads_paused_env=False,
        quality_gate_enforce=True,
        quality_clock=lambda: NOW,
        quality_service=stub,  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.INFO):
        result = await pipeline.upload_gate(UploadGateRequest(world.episode_id))
    assert not result.allowed
    assert stub.calls == []  # 既存の拒否が先
