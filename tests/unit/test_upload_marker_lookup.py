"""照合の3状態（FOUND / NOT_FOUND / UNKNOWN）と、UNKNOWN で新しい投稿に進まないこと（INV-109）。

実 YouTube・ネットワークには出ない（fake と httpx.MockTransport だけ）。理由は
docs/testing/upload-marker-lookup-rationale.md。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio.exceptions import ApplicationError

from contracts.states import ProviderCall, ReservationStatus
from contracts.upload import MarkerLookup, MarkerLookupState, upload_marker
from contracts.upload_activities import UploadAdmitRequest, UploadFinalVideoRequest
from infrastructure.db.models import Base
from infrastructure.db.repositories import ProviderReservationRepository
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import WorkDirectory
from infrastructure.youtube.errors import (
    YouTubeAuthError,
    YouTubeQuotaError,
    YouTubeRateLimitError,
    YouTubeTransientError,
)
from tests.support.fake_youtube import FakeVideoUploader
from tests.support.upload import CHANNEL_ID, TEST_CHUNK_BYTES, seed_render_ready
from tests.unit.test_youtube_uploader import _lookup_handler, _uploader
from workers.upload.activities import OPERATOR_REUPLOAD_APPROVED, UploadActivities

WF = "episode-x-upload"


# ------------------------------------------------------------- 契約の型


def test_marker_lookup_states_are_three_and_distinct() -> None:
    assert {s.value for s in MarkerLookupState} == {"found", "not_found", "unknown"}
    assert MarkerLookup.not_found() != MarkerLookup.unknown("x")


def test_marker_lookup_shape_is_validated() -> None:
    assert MarkerLookup.found("abcdefghijk").video_id == "abcdefghijk"
    with pytest.raises(ValueError):
        MarkerLookup(MarkerLookupState.FOUND)  # video id の無い FOUND
    with pytest.raises(ValueError):
        MarkerLookup(MarkerLookupState.NOT_FOUND, video_id="abcdefghijk")
    with pytest.raises(ValueError):
        MarkerLookup(MarkerLookupState.NOT_FOUND, reason="x")


# ------------------------------------------------------------- adapter（httpx.MockTransport）


async def test_adapter_exhausting_the_playlist_is_not_found() -> None:
    uploader, _ = _uploader(_lookup_handler([["a"], ["b"]], {}, []), max_lookup_pages=5)
    assert (await uploader.lookup_video_by_marker("m")).state is MarkerLookupState.NOT_FOUND


async def test_adapter_page_limit_with_more_pages_is_unknown_not_not_found() -> None:
    uploader, _ = _uploader(_lookup_handler([["v"]] * 10, {}, []), max_lookup_pages=3)
    result = await uploader.lookup_video_by_marker("m")
    assert result.state is MarkerLookupState.UNKNOWN and result.reason == "page_limit_reached"


async def test_adapter_found_beyond_the_first_page() -> None:
    uploader, _ = _uploader(_lookup_handler([["a"], ["b"]], {"b": ["m"]}, []))
    assert await uploader.lookup_video_by_marker("m") == MarkerLookup.found("b")


@pytest.mark.parametrize("body", [{}, {"items": None}, {"items": "x"}, {"items": {"a": 1}}])
async def test_adapter_malformed_playlist_page_is_unknown(body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/channels" in url:
            uploads = {"relatedPlaylists": {"uploads": "UU1"}}
            return httpx.Response(200, json={"items": [{"contentDetails": uploads}]})
        return httpx.Response(200, json=body)

    uploader, _ = _uploader(handler)
    result = await uploader.lookup_video_by_marker("m")
    assert result.state is MarkerLookupState.UNKNOWN


async def test_adapter_malformed_videos_response_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/channels" in url:
            uploads = {"relatedPlaylists": {"uploads": "UU1"}}
            return httpx.Response(200, json={"items": [{"contentDetails": uploads}]})
        if "/playlistItems" in url:
            return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "a"}}]})
        return httpx.Response(200, json={"unexpected": True})

    uploader, _ = _uploader(handler)
    assert (await uploader.lookup_video_by_marker("m")).state is MarkerLookupState.UNKNOWN


async def test_adapter_http_failure_raises_instead_of_reading_as_not_found() -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(500))
    with pytest.raises(YouTubeTransientError):
        await uploader.lookup_video_by_marker("m")


# ------------------------------------------------------------- Activity


@pytest_asyncio.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'h.db'}", poolclass=NullPool, connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class AlwaysFailingLookup:
    """照合だけが毎回 ``exc`` で失敗する YouTube。投稿の操作は fake に委ねる。"""

    def __init__(self, inner: FakeVideoUploader, exc: BaseException) -> None:
        self.inner = inner
        self.exc = exc
        self.lookups = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def lookup_video_by_marker(self, marker_tag: str) -> MarkerLookup:
        self.lookups += 1
        raise self.exc


class MalformedLookup(AlwaysFailingLookup):
    """応答の形が崩れた照合（adapter が UNKNOWN を返す）。"""

    async def lookup_video_by_marker(self, marker_tag: str) -> MarkerLookup:
        self.lookups += 1
        return MarkerLookup.unknown("malformed_playlist_page")


class Harness:
    def __init__(self, factory, tmp_path: Path) -> None:
        self.factory = factory
        self.store = InMemoryArtifactStore()
        self.tmp_path = tmp_path
        self.fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
        self.episode_id = ""

    def acts(self, uploader: Any = None) -> UploadActivities:
        acts = UploadActivities(
            session_factory=self.factory,
            store=self.store,
            bucket="artifacts",
            workdir=WorkDirectory(self.tmp_path / "work", forbidden=()),
            uploader=uploader or self.fake,
            channel_id=CHANNEL_ID,
            chunk_bytes=TEST_CHUNK_BYTES,
            marker_lookup_attempts=2,
            marker_lookup_delay_seconds=0.0,
            transient_backoff_seconds=0.0,
            expiry_confirm_delay_seconds=0.0,
            heartbeat=lambda *_: None,
        )
        acts.heartbeat_interval_seconds = 0.05
        return acts

    async def admit(self, acts: UploadActivities):
        return await acts.admit(UploadAdmitRequest(self.episode_id, WF, "run-1"))

    async def upload(self, acts: UploadActivities):
        return await acts.upload_final_video(UploadFinalVideoRequest(self.episode_id, WF, "run-1"))

    async def reservations(self):
        async with self.factory() as s:
            return await ProviderReservationRepository(s).list_for_episode_provider(
                self.episode_id, ProviderCall.YOUTUBE_UPLOAD
            )


@pytest_asyncio.fixture
async def h(factory, tmp_path) -> Harness:
    harness = Harness(factory, tmp_path)
    harness.episode_id = await seed_render_ready(factory, harness.store, tmp_path)
    return harness


async def _error(awaitable) -> ApplicationError:
    with pytest.raises(ApplicationError) as info:
        await awaitable
    return info.value


async def _approved_round_two_pending(h: Harness) -> None:
    """dispatch 済みで結果不明 → 運用者が再投稿を承認して放棄、まで進める（round 2 の直前）。"""
    assert (await h.admit(h.acts())).admitted
    h.fake.expire_session_at_offset = 2 * TEST_CHUNK_BYTES
    assert (await _error(h.upload(h.acts()))).type == "UploadOutcomeUnknownError"
    h.fake.expire_session_at_offset = None
    (row,) = [r for r in await h.reservations() if r.status is ReservationStatus.RESERVED]
    async with h.factory() as s:
        await ProviderReservationRepository(s).abandon(
            row.id, reconciled_by=OPERATOR_REUPLOAD_APPROVED
        )
        await s.commit()


@pytest.mark.parametrize(
    "exc",
    [
        YouTubeTransientError("boom"),
        YouTubeQuotaError("quota"),
        YouTubeRateLimitError("rate"),
        YouTubeAuthError("auth"),
        TimeoutError(),
        RuntimeError("anything at all"),
        ValueError("unparseable"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_lookup_error_before_round_two_never_inserts(h: Harness, exc: Exception) -> None:
    await _approved_round_two_pending(h)
    sessions, videos = h.fake.sessions_started, h.fake.videos_created
    flaky = AlwaysFailingLookup(h.fake, exc)

    err = await _error(h.upload(h.acts(flaky)))

    assert err.type == "UploadOutcomeUnknownError" and err.non_retryable
    assert flaky.lookups == 2  # 設定した回数まで試した
    assert h.fake.sessions_started == sessions and h.fake.videos_created == videos
    assert [(r.round, r.status) for r in await h.reservations()] == [
        (1, ReservationStatus.ABANDONED)
    ]


async def test_malformed_or_partial_lookup_before_round_two_never_inserts(h: Harness) -> None:
    await _approved_round_two_pending(h)
    sessions = h.fake.sessions_started
    err = await _error(h.upload(h.acts(MalformedLookup(h.fake, RuntimeError()))))
    assert err.type == "UploadOutcomeUnknownError"
    assert h.fake.sessions_started == sessions and h.fake.videos_created == 0

    h.fake.lookup_partial = True  # fake 自身のページ打ち切り
    err = await _error(h.upload(h.acts()))
    assert err.type == "UploadOutcomeUnknownError"
    assert h.fake.sessions_started == sessions and h.fake.videos_created == 0


async def test_error_message_names_the_state_and_carries_no_secret_or_response(h: Harness) -> None:
    await _approved_round_two_pending(h)
    leak = "https://example.invalid/upload?upload_id=SECRET-TOKEN-123"
    err = await _error(h.upload(h.acts(AlwaysFailingLookup(h.fake, RuntimeError(leak)))))
    assert "unknown" in str(err) and "RuntimeError" in str(err)
    assert "SECRET-TOKEN-123" not in str(err)


async def test_one_failed_attempt_is_unknown_even_if_the_next_one_finds_nothing(h: Harness) -> None:
    await _approved_round_two_pending(h)
    h.fake.fail_lookup_with = YouTubeTransientError("one-off")  # 1回目だけ失敗、2回目は「無い」
    err = await _error(h.upload(h.acts()))
    assert err.type == "UploadOutcomeUnknownError"
    assert h.fake.videos_created == 0 and len(await h.reservations()) == 1


async def test_empty_search_success_inserts_exactly_once(h: Harness) -> None:
    await _approved_round_two_pending(h)
    before = h.fake.videos_created

    result = await h.upload(h.acts())

    assert h.fake.videos_created == before + 1
    assert result.reconciled_by == "upload_response"
    assert [(r.round, r.status) for r in await h.reservations()] == [
        (1, ReservationStatus.ABANDONED),
        (2, ReservationStatus.SPENT),
    ]


async def test_found_adopts_the_video_without_any_insert(h: Harness) -> None:
    await _approved_round_two_pending(h)
    (row,) = await h.reservations()
    existing = h.fake.add_existing_video([upload_marker(row.idempotency_key)])
    sessions, videos = h.fake.sessions_started, h.fake.videos_created

    result = await h.upload(h.acts())

    assert result.video_id == existing and result.reconciled_by == "marker_lookup"
    assert h.fake.sessions_started == sessions and h.fake.videos_created == videos


async def test_repeated_executions_after_an_unknown_make_at_most_one_video(h: Harness) -> None:
    await _approved_round_two_pending(h)
    base = h.fake.videos_created
    broken = AlwaysFailingLookup(h.fake, YouTubeTransientError("down"))

    for _ in range(3):  # retry / worker 再起動を模す
        assert (await _error(h.upload(h.acts(broken)))).type == "UploadOutcomeUnknownError"
    assert h.fake.videos_created == base

    first = await h.upload(h.acts())  # 復旧後
    again = await h.upload(h.acts())  # 再実行
    third = await h.upload(h.acts(broken))  # 台帳が spent なので照合すら要らない

    assert h.fake.videos_created == base + 1
    assert first.video_id == again.video_id == third.video_id
    assert broken.lookups == 6  # 3回 × 2試行。復旧後の2回は照合に行かない


async def test_lookup_error_after_bytes_were_sent_stays_a_hold_and_opens_no_session(
    h: Harness,
) -> None:
    assert (await h.admit(h.acts())).admitted
    h.fake.expire_session_at_offset = 2 * TEST_CHUNK_BYTES
    err = await _error(h.upload(h.acts(AlwaysFailingLookup(h.fake, RuntimeError("down")))))
    assert err.type == "UploadOutcomeUnknownError" and "could not be checked" in str(err)
    assert h.fake.sessions_started == 1 and h.fake.videos_created == 0


async def test_round_one_never_looks_up(h: Harness) -> None:
    assert (await h.admit(h.acts())).admitted
    flaky = AlwaysFailingLookup(h.fake, RuntimeError("must not be called"))
    await h.upload(h.acts(flaky))
    assert flaky.lookups == 0 and h.fake.videos_created == 1
