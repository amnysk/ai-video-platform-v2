"""Upload テストの共通部品（ADR-0020）。

``render_ready`` の Episode は本物の RenderActivities（fake エンジン）で作る。
final_video の契約・版・読み戻しを手書きで再現しないため。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.render_activities import (
    RenderAdmitRequest,
    RenderFinalVideoRequest,
    RenderMarkReadyRequest,
)
from domain.artifact.hashing import sha256_hex
from domain.upload.ports import UploadCompleted, UploadProgress, UploadSessionRef
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.fake_render_engine import FakeFinalVideoProbe, FakeRenderEngine
from tests.support.fake_youtube import FakeVideoUploader
from tests.support.render_activity import PassingSourceProbe, seed_render_inputs
from workers.render.activities import RenderActivities

CHANNEL_ID = "UC" + "a" * 22
#: テスト用のチャンク長（fake は最後以外このちょうどの長さを要求する）
TEST_CHUNK_BYTES = 8
#: 5 チャンク + 端数 = 複数チャンクの送信になる長さ
FINAL_PAYLOAD = b"final-video-bytes-for-upload-tests-01234"


async def seed_render_ready(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    tmp_path: Path,
    *,
    payload: bytes = FINAL_PAYLOAD,
) -> str:
    """``render_ready`` の Episode（現行 final_video・台本つき）を作り、id を返す。"""
    seed = await seed_render_inputs(factory, store)
    font = tmp_path / "seed-font.ttc"
    font.write_bytes(b"seed font")
    probe = FakeFinalVideoProbe()
    engine = FakeRenderEngine(payload=payload, on_render=lambda r: setattr(probe, "plan", r.plan))
    render = RenderActivities(
        session_factory=factory,
        store=store,
        bucket="artifacts",
        workdir=WorkDirectory(tmp_path / "render-work", forbidden=()),
        engine=engine,
        probe=probe,
        source_probe=PassingSourceProbe(),
        font_path=font,
        font_sha256=sha256_hex(b"seed font"),
        render_timeout_seconds=60,
        min_free_bytes=0,
        heartbeat=lambda *_: None,
    )
    wf = f"episode-{seed.episode_id}-render"
    admitted = await render.admit(RenderAdmitRequest(seed.episode_id, wf, "seed-run"))
    assert admitted.admitted, admitted
    await render.render_final_video(
        RenderFinalVideoRequest(seed.episode_id, wf, "seed-run", "shorts_vertical")
    )
    ready = await render.mark_ready(RenderMarkReadyRequest(seed.episode_id, wf, "seed-run"))
    assert ready.status == "render_ready", ready
    return seed.episode_id


class Crash(BaseException):  # noqa: N818 - プロセス停止の模擬（Exception で捕まらない）
    """worker プロセスが落ちたことの模擬。Activity の ``except Exception`` を素通りする。"""


class CrashingUploader:
    """``FakeVideoUploader`` を包み、指定した地点で ``Crash`` を投げる。"""

    def __init__(self, inner: FakeVideoUploader, *, crash_on: str) -> None:
        self.inner = inner
        #: "first_send"（最初の PUT の前）/ "after_complete"（動画作成の直後、応答を返す前）
        self.crash_on = crash_on
        self.crashed = False

    async def start_session(self, metadata_json: Any, total_bytes: int, content_type: str):
        return await self.inner.start_session(metadata_json, total_bytes, content_type)

    async def query_status(self, session: UploadSessionRef) -> UploadProgress:
        return await self.inner.query_status(session)

    async def send_chunk(
        self, session: UploadSessionRef, offset: int, chunk: bytes, total_bytes: int
    ) -> UploadProgress:
        if self.crash_on == "first_send" and not self.crashed:
            self.crashed = True
            raise Crash()
        progress = await self.inner.send_chunk(session, offset, chunk, total_bytes)
        if (
            self.crash_on == "after_complete"
            and not self.crashed
            and isinstance(progress, UploadCompleted)
        ):
            self.crashed = True
            raise Crash()
        return progress

    async def find_video_by_marker(self, marker_tag: str) -> str | None:
        return await self.inner.find_video_by_marker(marker_tag)


class ExpireOnQueryUploader:
    """完了応答を失った直後に session が失効する YouTube（status query は 404）。"""

    def __init__(self, inner: FakeVideoUploader) -> None:
        self.inner = inner

    async def start_session(self, metadata_json: Any, total_bytes: int, content_type: str):
        return await self.inner.start_session(metadata_json, total_bytes, content_type)

    async def query_status(self, session: UploadSessionRef) -> UploadProgress:
        self.inner.expire_all_sessions()
        return await self.inner.query_status(session)

    async def send_chunk(
        self, session: UploadSessionRef, offset: int, chunk: bytes, total_bytes: int
    ) -> UploadProgress:
        return await self.inner.send_chunk(session, offset, chunk, total_bytes)

    async def find_video_by_marker(self, marker_tag: str) -> str | None:
        return await self.inner.find_video_by_marker(marker_tag)
