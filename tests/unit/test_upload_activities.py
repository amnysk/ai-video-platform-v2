"""Upload Activity（ADR-0020）。SQLite（ファイル）+ メモリストア + FakeVideoUploader。

最重要の検査は ``fake.videos_created``: 失敗・再実行・並行でも動画は1本だけ（INV-14）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio.exceptions import ApplicationError

from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
    ProviderCall,
    ReservationStatus,
)
from contracts.upload import parse_upload_receipt, upload_marker
from contracts.upload_activities import (
    UploadAdmitRequest,
    UploadFinalVideoRequest,
    UploadMarkUploadedRequest,
    UploadRecordFailureRequest,
)
from domain.artifact.hashing import canonical_json_bytes
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import WorkDirectory
from infrastructure.youtube.errors import YouTubeAuthError, YouTubeQuotaError
from tests.support.fake_youtube import FAKE_SESSION_PREFIX, FakeVideoUploader
from tests.support.upload import (
    CHANNEL_ID,
    FINAL_PAYLOAD,
    TEST_CHUNK_BYTES,
    Crash,
    CrashingUploader,
    ExpireOnQueryUploader,
    seed_render_ready,
)
from workers.upload.activities import OPERATOR_REUPLOAD_APPROVED, UploadActivities

WF = "episode-x-upload"


@pytest_asyncio.fixture
async def factory(tmp_path: Path):
    """並行する試行が別々の接続を持てるよう、ファイルの SQLite を使う。"""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'upload.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Harness:
    def __init__(self, factory, store, tmp_path: Path) -> None:
        self.factory = factory
        self.store = store
        self.tmp_path = tmp_path
        self.fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
        self.paused = False
        self.heartbeats: list[tuple[Any, ...]] = []
        self.episode_id = ""

    def activities(self, uploader: Any = None, **overrides: Any) -> UploadActivities:
        kwargs: dict[str, Any] = {
            "session_factory": self.factory,
            "store": self.store,
            "bucket": "artifacts",
            "workdir": WorkDirectory(self.tmp_path / "upload-work", forbidden=()),
            "uploader": uploader or self.fake,
            "channel_id": CHANNEL_ID,
            "uploads_paused": lambda: self.paused,
            "chunk_bytes": TEST_CHUNK_BYTES,
            "marker_lookup_attempts": 2,
            "marker_lookup_delay_seconds": 0.0,
            "transient_backoff_seconds": 0.0,
            "expiry_confirm_delay_seconds": 0.0,
            "heartbeat": lambda *d: self.heartbeats.append(d),
        }
        kwargs.update(overrides)
        acts = UploadActivities(**kwargs)
        acts.heartbeat_interval_seconds = 0.05
        return acts

    async def admit(self, acts: UploadActivities, run: str = "run-1"):
        return await acts.admit(UploadAdmitRequest(self.episode_id, WF, run))

    async def upload(self, acts: UploadActivities, run: str = "run-1"):
        return await acts.upload_final_video(UploadFinalVideoRequest(self.episode_id, WF, run))

    async def reservations(self):
        async with self.factory() as session:
            repo = ProviderReservationRepository(session)
            rows = []
            for n in (1, 2, 3):
                from workers.upload.activities import reservation_key

                key = await self._upload_key()
                found = await repo.find_by_key(reservation_key(key, n))
                if found is not None:
                    rows.append(found)
            return rows

    async def _upload_key(self) -> str:
        async with self.factory() as session:
            receipt_or_final = await ArtifactMetadataRepository(session).find_current_by_type(
                self.episode_id, ArtifactType.FINAL_VIDEO
            )
        assert receipt_or_final is not None
        from contracts.artifacts import parse_final_video
        from domain.upload.keys import compute_upload_key

        final = parse_final_video(await self.store.get_json(receipt_or_final.object_key))
        return compute_upload_key(
            episode_id=self.episode_id,
            final_video_sha256=final.media.sha256,
            destination_id=CHANNEL_ID,
        )

    async def status(self) -> EpisodeStatus:
        async with self.factory() as session:
            episode = await EpisodeRepository(session).get(self.episode_id)
        assert episode is not None
        return episode.status

    async def jobs(self):
        async with self.factory() as session:
            return [
                j
                for j in await JobRepository(session).list_for_episode(self.episode_id)
                if j.type is JobType.UPLOAD_FINAL_VIDEO
            ]


@pytest_asyncio.fixture
async def h(factory, tmp_path) -> Harness:
    harness = Harness(factory, InMemoryArtifactStore(), tmp_path)
    harness.episode_id = await seed_render_ready(factory, harness.store, tmp_path)
    return harness


async def _admitted(h: Harness, acts: UploadActivities | None = None) -> UploadActivities:
    acts = acts or h.activities()
    result = await h.admit(acts)
    assert result.admitted, result
    return acts


async def _error(awaitable) -> ApplicationError:
    with pytest.raises(ApplicationError) as info:
        await awaitable
    return info.value


# --------------------------------------------------------------------------- 成功


async def test_upload_creates_one_private_video_receipt_and_spent_reservation(h: Harness) -> None:
    acts = await _admitted(h)
    admit_again = await h.admit(acts)
    assert admit_again.upload_timeout_seconds > 0

    result = await h.upload(acts)

    assert h.fake.videos_created == 1 and h.fake.sessions_started == 1
    video = h.fake.videos[result.video_id]
    assert video.data == FINAL_PAYLOAD
    assert not result.skipped and result.reconciled_by == "upload_response"
    # INV-19: 送ったメタデータは private だけ
    (session,) = h.fake.sessions.values()
    assert session.metadata["status"]["privacyStatus"] == "private"
    assert session.metadata["status"]["containsSyntheticMedia"] is True
    assert set(h.fake.content_types) == {"video/mp4"}

    (reservation,) = await h.reservations()
    assert reservation.provider is ProviderCall.YOUTUBE_UPLOAD
    assert reservation.status is ReservationStatus.SPENT
    assert reservation.provider_result_ref == result.video_id
    assert reservation.dispatched_at is not None and reservation.round == 1
    assert reservation.outcome_artifact_id == result.artifact_id
    assert upload_marker(reservation.idempotency_key) in video.tags

    async with h.factory() as s:
        meta = await ArtifactMetadataRepository(s).get(result.artifact_id)
    assert meta is not None and meta.artifact_type is ArtifactType.UPLOAD_RECEIPT
    async with h.factory() as s:
        by_key = await ArtifactMetadataRepository(s).find_current(
            h.episode_id, ArtifactType.UPLOAD_RECEIPT, reservation.idempotency_key
        )
    assert by_key is not None and by_key.id == meta.id
    payload = await h.store.get_json(meta.object_key)
    receipt = parse_upload_receipt(payload)
    assert receipt.video_id == result.video_id and receipt.privacy_status == "private"
    assert receipt.destination.channel_id == CHANNEL_ID
    # INV-20: 受領に session URI・token が無い
    raw = canonical_json_bytes(payload).decode()
    assert FAKE_SESSION_PREFIX not in raw and "token" not in raw.lower()
    assert all(FAKE_SESSION_PREFIX not in json.dumps(d, default=str) for d in h.heartbeats)

    (job,) = await h.jobs()
    assert job.status is JobStatus.SUCCEEDED
    marked = await acts.mark_uploaded(UploadMarkUploadedRequest(h.episode_id, WF, "run-1"))
    assert marked.status == EpisodeStatus.UPLOADED.value
    assert (await h.admit(acts, "run-2")).admitted is False


async def test_rerun_after_success_reuses_the_receipt_without_calling_youtube(h: Harness) -> None:
    acts = await _admitted(h)
    first = await h.upload(acts)
    calls = (h.fake.sessions_started, h.fake.chunk_sends, h.fake.status_queries)

    again = await h.upload(h.activities())

    assert again.skipped and again.video_id == first.video_id
    assert again.artifact_id == first.artifact_id
    assert (h.fake.sessions_started, h.fake.chunk_sends, h.fake.status_queries) == calls
    assert h.fake.videos_created == 1


async def test_spent_reservation_without_receipt_writes_the_receipt_without_calling_youtube(
    h: Harness,
) -> None:
    """video id を記録した直後（受領を書く前）に crash した状態からの再実行。"""
    acts = await _admitted(h)
    first = await h.upload(acts)
    async with h.factory() as s:
        meta = await ArtifactMetadataRepository(s).get(first.artifact_id)
    assert meta is not None
    h.store._objects.pop(meta.object_key)  # 受領の本体が無い（書く前に落ちた）
    sessions = h.fake.sessions_started

    again = await h.upload(h.activities())

    assert again.video_id == first.video_id and again.skipped
    assert h.fake.sessions_started == sessions and h.fake.videos_created == 1
    assert parse_upload_receipt(await h.store.get_json(meta.object_key)).video_id == first.video_id


# --------------------------------------------------------------------------- 並行


async def test_concurrent_attempts_on_the_same_key_create_one_video(h: Harness) -> None:
    await _admitted(h)
    first, second = h.activities(), h.activities()

    results = await asyncio.gather(
        h.upload(first, "run-1"), h.upload(second, "run-1"), return_exceptions=True
    )

    video_ids = {r.video_id for r in results if not isinstance(r, BaseException)}
    assert h.fake.videos_created == 1
    assert len(video_ids) == 1, results
    (reservation,) = await h.reservations()
    assert reservation.status is ReservationStatus.SPENT


# --------------------------------------------------------------------------- 失敗からの再開


async def test_transient_failure_mid_chunk_resumes_the_same_session(h: Harness) -> None:
    acts = await _admitted(h)
    h.fake.fail_chunk_sends = 2

    result = await h.upload(acts)

    assert h.fake.sessions_started == 1 and h.fake.videos_created == 1
    assert h.fake.videos[result.video_id].data == FINAL_PAYLOAD


async def test_transient_failures_beyond_the_budget_raise_retryable_and_keep_the_session(
    h: Harness,
) -> None:
    acts = await _admitted(h, h.activities(transient_retries=1))
    h.fake.fail_chunk_sends = 10

    err = await _error(h.upload(acts))

    assert err.type == "TransientError" and not err.non_retryable
    (reservation,) = await h.reservations()
    assert reservation.provider_job_ref and reservation.dispatched_at is not None
    h.fake.fail_chunk_sends = 0
    await h.upload(h.activities())
    assert h.fake.sessions_started == 1 and h.fake.videos_created == 1


async def test_crash_after_session_saved_resumes_that_session(h: Harness) -> None:
    await _admitted(h)
    crashing = CrashingUploader(h.fake, crash_on="first_send")

    with pytest.raises(Crash):
        await h.upload(h.activities(crashing))
    (reservation,) = await h.reservations()
    assert reservation.provider_job_ref is not None and reservation.dispatched_at is not None

    result = await h.upload(h.activities())

    assert h.fake.sessions_started == 1 and h.fake.videos_created == 1
    assert result.reconciled_by == "upload_response"


async def test_crash_after_completion_before_the_id_is_saved_reconciles_by_status_query(
    h: Harness,
) -> None:
    await _admitted(h)
    with pytest.raises(Crash):
        await h.upload(h.activities(CrashingUploader(h.fake, crash_on="after_complete")))
    (reservation,) = await h.reservations()
    assert reservation.status is ReservationStatus.RESERVED
    assert reservation.provider_result_ref is None

    result = await h.upload(h.activities())

    assert h.fake.videos_created == 1 and h.fake.sessions_started == 1
    assert result.reconciled_by == "status_query"


async def test_lost_completion_response_is_reconciled_in_the_same_attempt(h: Harness) -> None:
    acts = await _admitted(h)
    h.fake.lose_completion_responses = 1

    result = await h.upload(acts)

    assert h.fake.videos_created == 1 and h.fake.sessions_started == 1
    assert result.reconciled_by == "status_query"


async def test_session_expired_after_completion_is_found_by_marker(h: Harness) -> None:
    await _admitted(h)
    h.fake.lose_completion_responses = 1

    result = await h.upload(h.activities(ExpireOnQueryUploader(h.fake)))

    assert result.reconciled_by == "marker_lookup"
    assert h.fake.videos_created == 1 and h.fake.sessions_started == 1
    (reservation,) = await h.reservations()
    assert reservation.reconciled_by == "marker_lookup"
    assert reservation.provider_result_ref == result.video_id


async def test_session_expired_after_bytes_without_marker_blocks_and_never_reopens(
    h: Harness, caplog
) -> None:
    # アプリのログに session URI が出ないこと（DB ドライバの SQL echo は対象外）
    for name in ("workers", "infrastructure", "temporalio"):
        caplog.set_level(logging.DEBUG, logger=name)
    acts = await _admitted(h)
    h.fake.expire_session_at_offset = 2 * TEST_CHUNK_BYTES

    err = await _error(h.upload(acts))

    assert err.type == "UploadOutcomeUnknownError" and err.non_retryable
    assert h.fake.videos_created == 0 and h.fake.sessions_started == 1
    assert h.fake.marker_lookups == 2
    (job,) = await h.jobs()
    assert job.failure_class is FailureClass.NEEDS_INPUT
    assert FAKE_SESSION_PREFIX not in str(err)
    app_logs = [r for r in caplog.records if r.name.split(".")[0] != "aiosqlite"]
    assert all(FAKE_SESSION_PREFIX not in r.getMessage() for r in app_logs)

    # 再 POST（再実行）でも新しい session を開かない
    h.fake.expire_session_at_offset = None
    again = await _error(h.upload(h.activities()))
    assert again.type == "UploadOutcomeUnknownError"
    assert h.fake.sessions_started == 1 and h.fake.videos_created == 0

    # 運用者がチャンネルを確認して予約を放棄した後だけ、新しいラウンドで投稿できる
    (reservation,) = await h.reservations()
    async with h.factory() as s:
        # dispatch 済みなので、ただの放棄ではなく再投稿の承認が要る
        await ProviderReservationRepository(s).abandon(
            reservation.id, reconciled_by=OPERATOR_REUPLOAD_APPROVED
        )
        await s.commit()
    result = await h.upload(h.activities())
    assert h.fake.videos_created == 1 and h.fake.sessions_started == 2
    rows = await h.reservations()
    assert [(r.round, r.status) for r in rows] == [
        (1, ReservationStatus.ABANDONED),
        (2, ReservationStatus.SPENT),
    ]
    assert rows[1].provider_result_ref == result.video_id


async def test_expired_session_before_any_byte_may_open_a_new_session(h: Harness) -> None:
    await _admitted(h)
    # session を保存した直後（dispatched の前）に crash した状態を作る
    key = await h._upload_key()
    session_ref = await h.fake.start_session({"status": {}}, len(FINAL_PAYLOAD), "video/mp4")
    async with h.factory() as s:
        repo = ProviderReservationRepository(s)
        row = await repo.reserve(
            episode_id=h.episode_id,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=key,
            input_hash=key,
            round=1,
        )
        assert await repo.record_upload_session(row.id, session_ref.uri)
        await s.commit()
    h.fake.expire_all_sessions()

    result = await h.upload(h.activities())

    assert h.fake.sessions_started == 2 and h.fake.videos_created == 1
    (reservation,) = await h.reservations()
    assert reservation.provider_job_ref != session_ref.uri
    assert reservation.provider_result_ref == result.video_id


# --------------------------------------------------------------------------- 呼ばない


async def test_auth_error_is_needs_input(h: Harness) -> None:
    acts = await _admitted(h)
    h.fake.fail_start_with = YouTubeAuthError("oauth token refresh rejected: invalid_grant")

    err = await _error(h.upload(acts))

    assert err.type == "UploadAuthError" and err.non_retryable
    assert h.fake.videos_created == 0


async def test_quota_error_is_retryable(h: Harness) -> None:
    acts = await _admitted(h)
    h.fake.fail_start_with = YouTubeQuotaError("start upload session: HTTP 403 (quotaExceeded)")

    err = await _error(h.upload(acts))

    assert err.type == "UploadQuotaExceededError" and not err.non_retryable
    (job,) = await h.jobs()
    assert job.status is JobStatus.RETRYABLE_FAILED


async def test_final_video_sha_mismatch_never_calls_youtube(h: Harness) -> None:
    acts = await _admitted(h)
    async with h.factory() as s:
        meta = await ArtifactMetadataRepository(s).find_current_by_type(
            h.episode_id, ArtifactType.FINAL_VIDEO
        )
    assert meta is not None
    from contracts.artifacts import parse_final_video

    final = parse_final_video(await h.store.get_json(meta.object_key))
    h.store._objects[final.media.object_key] = b"tampered" + FINAL_PAYLOAD[8:]

    err = await _error(h.upload(acts))

    assert err.type == "UploadIntegrityError" and err.non_retryable
    assert h.fake.sessions_started == 0 and h.fake.chunk_sends == 0
    assert await h.reservations() == []


async def test_uploads_paused_never_calls_youtube(h: Harness) -> None:
    acts = await _admitted(h)
    h.paused = True

    err = await _error(h.upload(acts))

    assert err.type == "UploadsPausedError" and err.non_retryable
    assert (h.fake.sessions_started, h.fake.status_queries, h.fake.marker_lookups) == (0, 0, 0)


async def test_missing_final_video_is_needs_input(factory, tmp_path) -> None:
    store = InMemoryArtifactStore()
    h = Harness(factory, store, tmp_path)
    h.episode_id = await seed_render_ready(factory, store, tmp_path)
    acts = await _admitted(h)
    async with factory() as s:
        meta = await ArtifactMetadataRepository(s).find_current_by_type(
            h.episode_id, ArtifactType.FINAL_VIDEO
        )
    assert meta is not None
    store._objects.pop(meta.object_key)

    err = await _error(h.upload(acts))

    assert err.type == "UploadInputMissingError"


# --------------------------------------------------------------------------- 入場・失敗


async def test_admit_refuses_uploaded_and_other_stage_blocked(h: Harness) -> None:
    acts = await _admitted(h)
    result = await acts.record_failure(
        UploadRecordFailureRequest(h.episode_id, WF, "run-1", "needs_input", "x", False)
    )
    assert result.episode_status == EpisodeStatus.BLOCKED.value
    # 他 workflow の id では再開できない
    assert not (
        await acts.admit(UploadAdmitRequest(h.episode_id, "episode-y-upload", "r"))
    ).admitted
    resumed = await h.admit(acts, "run-2")
    assert resumed.admitted and resumed.status == EpisodeStatus.IN_PROGRESS.value


async def test_record_failure_blocks_after_retry_exhaustion(h: Harness) -> None:
    acts = await _admitted(h)
    outcome = await acts.record_failure(
        UploadRecordFailureRequest(
            h.episode_id, WF, "run-1", "retryable", "quota fake-youtube://session/9", True
        )
    )
    assert outcome.episode_status == EpisodeStatus.BLOCKED.value
    (job,) = await h.jobs()
    assert job.status is JobStatus.RETRYABLE_FAILED or job.status is JobStatus.TERMINAL_FAILED


async def test_admit_is_refused_for_render_owned_in_progress(h: Harness) -> None:
    """render が走っている in_progress は upload から引き継がない。"""
    acts = await _admitted(h)
    other = await acts.admit(UploadAdmitRequest(h.episode_id, "episode-z-upload", "r"))
    assert not other.admitted and other.status == EpisodeStatus.IN_PROGRESS.value
