"""commit の後にだけ出すイベント（log-contract §9）。理由は docs/testing/logging-rationale.md。

予約台帳・成果物・認可障害・拒否のイベントは、その変更を含む commit が成功した後にだけ出る。
rollback・commit せずに閉じた transaction では出ない（「ログにある＝DB にある」を崩さない）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from contracts.states import ArtifactType, ProviderCall, RejectionCategory
from domain.errors import ProviderRejection
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderAuthIncidentRepository,
    ProviderRejectionRepository,
    ProviderReservationRepository,
)
from tests.support.log_capture import capture_json


async def _episode(session_factory) -> str:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
        return episode.id


async def _reserve(session, episode_id: str, *, round: int = 1):
    return await ProviderReservationRepository(session).reserve(
        episode_id=episode_id,
        provider=ProviderCall.FAL_IMAGE,
        idempotency_key=f"k-{uuid.uuid4().hex}",
        input_hash="h" * 64,
        round=round,
        scene_id="sb6",
    )


async def test_reservation_events_come_only_after_commit(session_factory) -> None:
    episode_id = await _episode(session_factory)
    with capture_json() as logs:
        async with session_factory() as session:
            reservation = await _reserve(session, episode_id, round=2)
            assert logs.names() == []  # flush しただけでは出ない
            await session.commit()
        [reserved] = logs.events("reservation.reserved")
    assert reserved["reservation_id"] == reservation.id
    assert reserved["reservation_status"] == "reserved"
    assert reserved["provider"] == "fal_image"
    assert reserved["provider_attempt"] == 2
    assert reserved["scene_id"] == "sb6" and reserved["episode_id"] == episode_id


async def test_rolled_back_or_uncommitted_changes_are_not_logged(session_factory) -> None:
    episode_id = await _episode(session_factory)
    with capture_json() as logs:
        async with session_factory() as session:
            await _reserve(session, episode_id)
            await session.rollback()
        async with session_factory() as session:
            await _reserve(session, episode_id)
            # commit せずに閉じる
        async with session_factory() as session:
            await EpisodeRepository(session).create(topic="other")
            await session.commit()  # 別の commit で古い保留が漏れ出さない
    assert logs.names() == []


async def test_dispatch_job_ref_and_spent_follow_the_ledger(session_factory) -> None:
    episode_id = await _episode(session_factory)
    async with session_factory() as session:
        reservation = await _reserve(session, episode_id)
        await session.commit()
    with capture_json() as logs:
        async with session_factory() as session:
            repo = ProviderReservationRepository(session)
            await repo.mark_dispatched(reservation.id)
            await session.commit()
        async with session_factory() as session:
            repo = ProviderReservationRepository(session)
            await repo.record_provider_job_ref(reservation.id, '{"v":1,"request_id":"r"}')
            await session.commit()
            # 同じ参照の再記録は no-op。イベントも出さない
            await repo.record_provider_job_ref(reservation.id, '{"v":1,"request_id":"r"}')
            await session.commit()
        async with session_factory() as session:
            await ProviderReservationRepository(session).mark_spent(
                reservation.id, raw_output_key="provider-raw/x", reconciled_by="evidence"
            )
            await session.commit()
    assert logs.names() == [
        "reservation.dispatched",
        "reservation.job_ref_recorded",
        "reservation.spent",
    ]
    spent = logs.events("reservation.spent")[0]
    assert spent["reservation_status"] == "spent"
    assert spent["attributes"]["reconciled_by"] == "evidence"


async def test_rerecording_the_same_upload_result_is_a_silent_no_op(session_factory) -> None:
    """並行する upload の2試行が同じ video id を記録すると、2つ目は no-op（レビュー I-20）。
    ``reservation.spent`` は実際に spent にした1回だけ出し、呼び出し側には書いたかを返す
    （書いていない試行は ``upload.succeeded`` ではなく ``upload.reused_existing`` を出す）。"""
    episode_id = await _episode(session_factory)
    async with session_factory() as session:
        reservation = await ProviderReservationRepository(session).reserve(
            episode_id=episode_id,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=f"k-{uuid.uuid4().hex}",
            input_hash="u" * 64,
            round=1,
        )
        await session.commit()
    with capture_json() as logs:
        async with session_factory() as session:
            repo = ProviderReservationRepository(session)
            first, wrote = await repo.record_upload_result_once(
                reservation.id, "vid-1", reconciled_by="upload_response"
            )
            await session.commit()
        async with session_factory() as session:
            repo = ProviderReservationRepository(session)
            again, wrote_again = await repo.record_upload_result_once(
                reservation.id, "vid-1", reconciled_by="status_query"
            )
            await session.commit()
    assert wrote and not wrote_again
    assert first.provider_result_ref == again.provider_result_ref == "vid-1"
    assert again.reconciled_by == "upload_response"  # 先に書いた試行の記録のまま
    assert logs.names() == ["reservation.spent"]


async def test_artifact_stored_and_superseded(session_factory) -> None:
    episode_id = await _episode(session_factory)

    async def record(sha: str):
        async with session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=episode_id,
                artifact_type=ArtifactType.SCENE_IMAGE,
                schema_version="1",
                bucket="b",
                object_key=f"k/{sha}",
                sha256=sha,
                input_hash="i" * 64,
                scene_id="sb6",
            )
            await session.commit()
            return meta

    with capture_json() as logs:
        first = await record("a" * 64)
        second = await record("b" * 64)
        await record("b" * 64)  # 同じ内容の再記録は変化なし
    assert logs.names() == ["artifact.stored", "artifact.superseded", "artifact.stored"]
    superseded = logs.events("artifact.superseded")[0]
    assert superseded["artifact_id"] == first.id and superseded["scene_id"] == "sb6"
    assert logs.events("artifact.stored")[1]["artifact_id"] == second.id


async def test_rejection_and_auth_incident(session_factory) -> None:
    episode_id = await _episode(session_factory)
    rejection = ProviderRejection(
        types=("file_download_error",),
        locs=("body.image_url",),
        reason=None,
        message="could not download",
        http_status=422,
        category=RejectionCategory.INPUT_UNREACHABLE,
    )
    with capture_json() as logs:
        async with session_factory() as session:
            await ProviderRejectionRepository(session).record(
                episode_id=episode_id,
                scene_id="sb6",
                provider=ProviderCall.FAL_VIDEO,
                reservation_id=None,
                input_hash="h" * 64,
                rejection=rejection,
                source_media_sha256=None,
            )
            await ProviderAuthIncidentRepository(session).record(
                provider=ProviderCall.FAL_VIDEO,
                http_status=403,
                episode_id=episode_id,
                now=datetime.now(UTC),
            )
            await session.commit()
    [rejected] = logs.events("scene.rejected")
    assert rejected["error_category"] == "input_unreachable"
    assert rejected["error_code"] == ["file_download_error"]
    assert rejected["http_status"] == 422
    assert rejected["classification_basis"] == "provider_error_type"
    [incident] = logs.events("provider.auth_incident.recorded")
    assert incident["error_category"] == "access_denied"
    assert incident["classification_basis"] == "http_status_only"
