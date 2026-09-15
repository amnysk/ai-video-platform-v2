"""Upload の二重投稿の追加の穴（レビュー指摘 1-10、ADR-0020）。どれも ``videos_created`` <= 1。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio.exceptions import ApplicationError

from contracts.states import EpisodeStatus, ProviderCall, ReservationStatus
from contracts.upload import build_youtube_metadata, upload_marker
from contracts.upload_activities import UploadAdmitRequest, UploadFinalVideoRequest
from domain.episode.transitions import EpisodeEvent
from domain.errors import InvalidTransitionError
from domain.upload.ports import UploadExpired, UploadProgress, UploadSessionRef
from infrastructure.db.models import Base, JobRow
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.fake_youtube import FAKE_SESSION_PREFIX, FakeVideoUploader
from tests.support.upload import (
    CHANNEL_ID,
    TEST_CHUNK_BYTES,
    Crash,
    CrashingUploader,
    seed_render_ready,
)
from workers.upload.activities import OPERATOR_REUPLOAD_APPROVED, UploadActivities

WF = "episode-x-upload"
KEY = "f" * 64


@pytest_asyncio.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'h.db'}", poolclass=NullPool, connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Delegate:
    """FakeVideoUploader を包む。サブクラスで一部の操作だけ差し替える。"""

    def __init__(self, inner: FakeVideoUploader) -> None:
        self.inner = inner

    async def start_session(self, metadata_json: Any, total_bytes: int, content_type: str):
        return await self.inner.start_session(metadata_json, total_bytes, content_type)

    async def query_status(self, session: UploadSessionRef) -> UploadProgress:
        return await self.inner.query_status(session)

    async def send_chunk(self, session, offset, chunk, total_bytes) -> UploadProgress:
        return await self.inner.send_chunk(session, offset, chunk, total_bytes)

    async def find_video_by_marker(self, marker_tag: str) -> str | None:
        return await self.inner.find_video_by_marker(marker_tag)

    async def own_channel_id(self) -> str:
        return await self.inner.own_channel_id()


class H:
    def __init__(self, factory, tmp_path: Path) -> None:
        self.factory = factory
        self.store = InMemoryArtifactStore()
        self.tmp_path = tmp_path
        self.fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
        self.episode_id = ""

    def acts(self, uploader: Any = None, **overrides: Any) -> UploadActivities:
        kwargs: dict[str, Any] = {
            "session_factory": self.factory,
            "store": self.store,
            "bucket": "artifacts",
            "workdir": WorkDirectory(self.tmp_path / "work", forbidden=()),
            "uploader": uploader or self.fake,
            "channel_id": CHANNEL_ID,
            "chunk_bytes": TEST_CHUNK_BYTES,
            "marker_lookup_attempts": 2,
            "marker_lookup_delay_seconds": 0.0,
            "transient_backoff_seconds": 0.0,
            "expiry_confirm_delay_seconds": 0.0,
            "heartbeat": lambda *_: None,
        }
        kwargs.update(overrides)
        acts = UploadActivities(**kwargs)
        acts.heartbeat_interval_seconds = 0.05
        return acts

    async def admit(self, acts: UploadActivities, wf: str = WF, run: str = "run-1"):
        return await acts.admit(UploadAdmitRequest(self.episode_id, wf, run))

    async def upload(self, acts: UploadActivities, wf: str = WF, run: str = "run-1"):
        return await acts.upload_final_video(UploadFinalVideoRequest(self.episode_id, wf, run))

    async def reservations(self):
        async with self.factory() as s:
            return await ProviderReservationRepository(s).list_for_episode_provider(
                self.episode_id, ProviderCall.YOUTUBE_UPLOAD
            )


@pytest_asyncio.fixture
async def h(factory, tmp_path) -> H:
    harness = H(factory, tmp_path)
    harness.episode_id = await seed_render_ready(factory, harness.store, tmp_path)
    return harness


async def _error(awaitable) -> ApplicationError:
    with pytest.raises(ApplicationError) as info:
        await awaitable
    return info.value


async def _dispatched_then_unknown(h: H) -> None:
    """dispatch 済みで結果不明（blocked）の予約を1つ作る。"""
    assert (await h.admit(h.acts())).admitted
    h.fake.expire_session_at_offset = 2 * TEST_CHUNK_BYTES
    err = await _error(h.upload(h.acts()))
    assert err.type == "UploadOutcomeUnknownError"
    h.fake.expire_session_at_offset = None


async def _abandon(h: H, reconciled_by: str) -> None:
    (row,) = [r for r in await h.reservations() if r.status is ReservationStatus.RESERVED]
    async with h.factory() as s:
        await ProviderReservationRepository(s).abandon(row.id, reconciled_by=reconciled_by)
        await s.commit()


# ------------------------------------------------------------- 1. 状態遷移の CAS と所有権


async def test_apply_event_is_compare_and_set(h: H) -> None:
    async with h.factory() as first, h.factory() as second:
        a, b = EpisodeRepository(first), EpisodeRepository(second)
        assert (await a.get(h.episode_id)).status is EpisodeStatus.RENDER_READY  # type: ignore[union-attr]
        assert (await b.get(h.episode_id)).status is EpisodeStatus.RENDER_READY  # type: ignore[union-attr]
        await a.apply_event(h.episode_id, EpisodeEvent.STAGE_ADMITTED)
        await first.commit()
        with pytest.raises(InvalidTransitionError, match="rejected"):
            await b.apply_event(h.episode_id, EpisodeEvent.STAGE_ADMITTED)


async def test_apply_event_on_a_stale_read_hits_the_compare_and_set(h: H, monkeypatch) -> None:
    """読んだ状態が DB と食い違っていたら、表が許す遷移でも書かない（CAS の 0 行）。"""
    from sqlalchemy.orm.attributes import set_committed_value

    # 別の実行が先に入場した
    async with h.factory() as session:
        await EpisodeRepository(session).apply_event(h.episode_id, EpisodeEvent.STAGE_ADMITTED)
        await session.commit()

    real_row = EpisodeRepository._row

    async def stale_row(self, episode_id):
        row = await real_row(self, episode_id)
        if row is not None:
            # この呼び出し側は入場前に読んだ ``render_ready`` を持っている（dirty にしない）
            set_committed_value(row, "status", EpisodeStatus.RENDER_READY.value)
        return row

    monkeypatch.setattr(EpisodeRepository, "_row", stale_row)
    async with h.factory() as session:
        with pytest.raises(InvalidTransitionError, match="concurrent"):
            await EpisodeRepository(session).apply_event(h.episode_id, EpisodeEvent.STAGE_ADMITTED)
    monkeypatch.undo()
    async with h.factory() as session:
        episode = await EpisodeRepository(session).get(h.episode_id)
    assert episode is not None and episode.status is EpisodeStatus.IN_PROGRESS


async def test_concurrent_admits_with_different_workflows_admit_only_one(h: H) -> None:
    results = await asyncio.gather(
        h.admit(h.acts(), "episode-x-upload", "r1"),
        h.admit(h.acts(), "episode-x-upload-dup", "r2"),
        return_exceptions=True,
    )
    admitted = [r for r in results if not isinstance(r, BaseException) and r.admitted]
    assert len(admitted) == 1, results


async def test_upload_by_a_non_owner_never_starts_a_session(h: H) -> None:
    assert (await h.admit(h.acts())).admitted
    async with h.factory() as s:
        await EpisodeRepository(s).set_workflow_id(h.episode_id, "episode-x-upload:other-run")
        await s.commit()

    err = await _error(h.upload(h.acts()))

    assert err.type == "UploadOwnershipLostError" and err.non_retryable
    assert h.fake.sessions_started == 0 and h.fake.videos_created == 0


async def test_ownership_is_rechecked_before_dispatch(h: H) -> None:
    assert (await h.admit(h.acts())).admitted
    factory = h.factory

    class StealAfterStart(Delegate):
        async def start_session(self, metadata_json, total_bytes, content_type):
            ref = await self.inner.start_session(metadata_json, total_bytes, content_type)
            async with factory() as s:
                await EpisodeRepository(s).set_workflow_id(h.episode_id, "someone:else")
                await s.commit()
            return ref

    err = await _error(h.upload(h.acts(StealAfterStart(h.fake))))

    assert err.type == "UploadOwnershipLostError"
    assert h.fake.chunk_sends == 0 and h.fake.videos_created == 0
    (row,) = await h.reservations()
    assert row.dispatched_at is None


# ------------------------------------------------------------- 2. Episode 単位の検査


@pytest.mark.parametrize("dispatched", [True, False])
async def test_other_upload_key_already_spent_or_dispatched_blocks(h: H, dispatched) -> None:
    assert (await h.admit(h.acts())).admitted
    async with h.factory() as s:
        repo = ProviderReservationRepository(s)
        other = await repo.reserve(
            episode_id=h.episode_id,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=KEY,
            input_hash=KEY,  # 別の final_video / チャンネル
            round=1,
        )
        assert await repo.record_upload_session(other.id, "fake-youtube://session/old")
        if dispatched:
            assert (await repo.mark_upload_dispatched(other.id, "fake-youtube://session/old"))[0]
        else:
            await repo.record_upload_result(
                other.id, "vidOLD00001", reconciled_by="upload_response"
            )
        await s.commit()

    err = await _error(h.upload(h.acts()))

    assert err.type == "UploadOutcomeUnknownError" and "different" in str(err)
    assert h.fake.sessions_started == 0


# ------------------------------------------------------------- 3. dispatch 済みの放棄


async def test_plain_abandon_of_a_dispatched_reservation_does_not_open_round_two(h: H) -> None:
    await _dispatched_then_unknown(h)
    await _abandon(h, "operator:alice")

    err = await _error(h.upload(h.acts()))

    assert err.type == "UploadOutcomeUnknownError" and OPERATOR_REUPLOAD_APPROVED in str(err)
    assert h.fake.sessions_started == 1 and len(await h.reservations()) == 1


async def test_approved_reupload_records_a_video_found_by_marker_instead_of_uploading(h: H) -> None:
    await _dispatched_then_unknown(h)
    (row,) = await h.reservations()
    existing = h.fake.add_existing_video([upload_marker(row.idempotency_key)])
    await _abandon(h, OPERATOR_REUPLOAD_APPROVED)

    result = await h.upload(h.acts())

    assert result.video_id == existing and result.reconciled_by == "marker_lookup"
    assert h.fake.sessions_started == 1 and h.fake.videos_created == 1
    rounds = [(r.round, r.status) for r in await h.reservations()]
    assert rounds == [(1, ReservationStatus.ABANDONED), (2, ReservationStatus.SPENT)]


async def test_approved_reupload_without_marker_uploads_once_in_round_two(h: H) -> None:
    await _dispatched_then_unknown(h)
    await _abandon(h, OPERATOR_REUPLOAD_APPROVED)
    lookups = h.fake.marker_lookups

    await h.upload(h.acts())

    assert h.fake.marker_lookups > lookups
    assert h.fake.sessions_started == 2 and h.fake.videos_created == 1


# ------------------------------------------------------------- 4. 送信中の予約の再確認


async def test_reservation_closed_mid_upload_stops_sending(h: H) -> None:
    assert (await h.admit(h.acts())).admitted
    factory = h.factory

    class AbandonAfterFirstChunk(Delegate):
        sent = 0

        async def send_chunk(self, session, offset, chunk, total_bytes):
            progress = await self.inner.send_chunk(session, offset, chunk, total_bytes)
            self.sent += 1
            if self.sent == 1:
                async with factory() as s:
                    repo = ProviderReservationRepository(s)
                    (row,) = await repo.list_for_episode_provider(
                        h.episode_id, ProviderCall.YOUTUBE_UPLOAD
                    )
                    await repo.abandon(row.id, reconciled_by="operator:test")
                    await s.commit()
            return progress

    uploader = AbandonAfterFirstChunk(h.fake)
    err = await _error(h.upload(h.acts(uploader, reservation_check_every_chunks=1)))

    assert err.type == "UploadOutcomeUnknownError"
    assert uploader.sent == 1 and h.fake.videos_created == 0


# ------------------------------------------------------------- 5. チャンネルの照合


async def test_channel_mismatch_is_auth_error_before_any_session(h: H) -> None:
    assert (await h.admit(h.acts())).admitted
    h.fake.channel_id = "UC" + "z" * 22

    err = await _error(h.upload(h.acts()))

    assert err.type == "UploadAuthError" and err.non_retryable
    assert h.fake.sessions_started == 0


async def test_channel_is_checked_once_per_worker(h: H) -> None:
    assert (await h.admit(h.acts())).admitted
    acts = h.acts()
    await h.upload(acts)
    await h.upload(acts)  # 再利用（YouTube は呼ばない）
    assert h.fake.channel_lookups == 1


async def test_worker_startup_refuses_a_different_channel() -> None:
    from workers.upload.run_worker import verify_channel

    fake = FakeVideoUploader()
    await verify_channel(fake, fake.channel_id)
    with pytest.raises(SystemExit, match="different channel"):
        await verify_channel(fake, "UC" + "q" * 22)


# ------------------------------------------------------------- 6. secret の漏えい


def test_engines_hide_sql_parameters() -> None:
    from infrastructure.db.session import build_session_factory

    factory = build_session_factory("sqlite+aiosqlite:///:memory:")
    bind = factory.kw["bind"]
    assert bind.sync_engine.hide_parameters is True


async def test_db_error_with_the_session_uri_does_not_leak(h: H, monkeypatch) -> None:
    assert (await h.admit(h.acts())).admitted

    async def broken(self, reservation_id, session_ref):
        raise OperationalError(
            f"UPDATE ... {session_ref}", {"ref": session_ref}, Exception(session_ref)
        )

    monkeypatch.setattr(ProviderReservationRepository, "mark_upload_dispatched", broken)

    err = await _error(h.upload(h.acts()))

    assert FAKE_SESSION_PREFIX not in str(err) and err.type == "TransientError"
    async with h.factory() as s:
        rows = [r for r in (await s.execute(JobRow.__table__.select())).all()]
    summaries = " ".join(str(r.error_summary) for r in rows)
    assert "database unavailable" in summaries and FAKE_SESSION_PREFIX not in summaries


# ------------------------------------------------------------- 7. dispatch を立てた試行が 0 から


async def test_mark_upload_dispatched_reports_who_changed_the_row(h: H) -> None:
    async with h.factory() as s:
        repo = ProviderReservationRepository(s)
        row = await repo.reserve(
            episode_id=h.episode_id,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=KEY,
            input_hash=KEY,
            round=1,
        )
        assert await repo.record_upload_session(row.id, "s1")
        assert await repo.mark_upload_dispatched(row.id, "s1") == (True, True)
        assert await repo.mark_upload_dispatched(row.id, "s1") == (True, False)
        assert await repo.mark_upload_dispatched(row.id, "s2") == (False, False)


# ------------------------------------------------------------- 8. 404 は2回目の照会で確かめる


async def test_a_single_spurious_404_is_confirmed_before_treating_the_session_as_expired(
    h: H,
) -> None:
    assert (await h.admit(h.acts())).admitted
    with pytest.raises(Crash):
        await h.upload(h.acts(CrashingUploader(h.fake, crash_on="first_send")))

    class OneSpurious404(Delegate):
        seen = False

        async def query_status(self, session):
            if not self.seen:
                self.seen = True
                return UploadExpired()
            return await self.inner.query_status(session)

    result = await h.upload(h.acts(OneSpurious404(h.fake)))

    assert result.reconciled_by == "upload_response"
    assert h.fake.sessions_started == 1 and h.fake.videos_created == 1
    assert h.fake.marker_lookups == 0


# ------------------------------------------------------------- 9. description のマーカー


def test_metadata_description_ends_with_the_marker_even_when_truncated() -> None:
    meta = build_youtube_metadata(
        upload_key=KEY, title="t", hook="h", narration="あ" * 5000, language="ja"
    )
    assert meta.description.splitlines()[-1] == upload_marker(KEY)
    assert len(meta.description.encode("utf-8")) <= 5000


async def test_fake_marker_lookup_matches_description_lines() -> None:
    fake = FakeVideoUploader()
    video = fake.add_existing_video([], description=f"body\n\n{upload_marker(KEY)}")
    assert await fake.find_video_by_marker(upload_marker(KEY)) == video


# ------------------------------------------------------------- 10. cancel は送信の終了を待つ


async def test_cancel_waits_for_the_in_flight_chunk_to_stop(h: H) -> None:
    assert (await h.admit(h.acts())).admitted
    started = asyncio.Event()

    class SlowSend(Delegate):
        stopped = False

        async def send_chunk(self, session, offset, chunk, total_bytes):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # 通信の後始末
                self.stopped = True
                raise
            return await self.inner.send_chunk(session, offset, chunk, total_bytes)

    uploader = SlowSend(h.fake)
    task = asyncio.ensure_future(h.upload(h.acts(uploader)))
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert uploader.stopped and h.fake.videos_created == 0
