"""Upload 工程の Activity（ADR-0020）。

- 入場（admit）/ 投稿（upload_final_video）/ 完了（mark_uploaded）/ 失敗（record_failure）
- **次に何をするかは決めない**（INV-4）。順序は ``UploadWorkflow`` が持つ
- YouTube は注入した ``VideoUploader`` port 越しにだけ呼ぶ（INV-18）

二重投稿の防止（INV-14）は予約台帳（ADR-0013）の1行と、条件付き UPDATE の不変条件で守る:

1. 現行 ``final_video`` を PostgreSQL → MinIO JSON（sha256）→ 契約で読み、upload key を作る
2. 同じ key の予約が ``spent`` + video id → YouTube を呼ばず受領を作る / 再利用する
3. ``UPLOADS_PAUSED`` → 何も呼ばずに止める
4. 本体を作業領域へ流して sha256 を照合（不一致なら YouTube を呼ばない）
5. 予約（``reserved``）を読むか作る（自動で開くラウンドは1つ。abandoned の後だけ次ラウンド）
6. session なし → ``start_session`` → session を予約へ **commit** → dispatched を **commit** → bytes
   session あり・未 dispatch → status query。失効なら session を差し替え（bytes 未送信なので安全）
   session あり・dispatch 済み → status query。未完了なら続きから、完了なら記録、
   失効ならマーカー照合。見つからなければ ``UploadOutcomeUnknownError``（新しい session は開かない）
7. 完了 → video id + ``spent`` を **commit** → 受領 Artifact → job 成功

session URI はログ・例外・Artifact・heartbeat に出さない（INV-20）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.artifacts import ScriptArtifact, parse_script_artifact
from contracts.render import FinalVideoArtifact
from contracts.states import (
    JOB_TERMINAL_STATUSES,
    RETRYABLE_FAILURE_CLASSES,
    UPLOAD_ADMISSIBLE_STATUSES,
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
    ProviderCall,
    ReservationStatus,
)
from contracts.upload import (
    DEFAULT_UPLOAD_CHUNK_BYTES,
    DEFAULT_UPLOAD_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS,
    DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS,
    UPLOAD_ARTIFACT_SCHEMA_VERSION,
    UPLOAD_CONTENT_TYPE,
    YouTubeVideoMetadata,
    build_upload_receipt,
    build_youtube_metadata,
    parse_upload_receipt,
    upload_marker,
    upload_timeout_seconds,
)
from contracts.upload_activities import (
    UPLOAD_ADMIT,
    UPLOAD_FINAL_VIDEO,
    UPLOAD_MARK_UPLOADED,
    UPLOAD_RECORD_FAILURE,
    UploadAdmitRequest,
    UploadAdmitResult,
    UploadFailureOutcome,
    UploadFinalVideoRequest,
    UploadFinalVideoResult,
    UploadMarkUploadedRequest,
    UploadMarkUploadedResult,
    UploadRecordFailureRequest,
)
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    DomainError,
    TransientError,
    UploadInputMissingError,
    UploadIntegrityError,
    UploadOutcomeUnknownError,
    UploadsPausedError,
    classify_failure,
)
from domain.job.transitions import JobEvent, episode_event_for_failure, job_event_for_failure
from domain.production.identity import idempotency_key
from domain.upload.keys import compute_upload_key
from domain.upload.ports import (
    UploadCompleted,
    UploadExpired,
    UploadProgress,
    UploadSessionRef,
    VideoUploader,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservation,
    ProviderReservationRepository,
)
from infrastructure.production.activity_errors import raise_activity_error, translate_error
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.temporal.run_inspector import WorkflowRunInspector
from infrastructure.workdir import WorkDirectory
from infrastructure.youtube.errors import (
    YouTubeQuotaError,
    YouTubeRateLimitError,
    YouTubeTransientError,
)
from infrastructure.youtube.metadata import insert_body
from workers.upload.errors import scrub, translate_youtube_error

logger = logging.getLogger(__name__)

ADMISSIBLE_STATUSES = UPLOAD_ADMISSIBLE_STATUSES
ADMIT_EVENTS: dict[EpisodeStatus, EpisodeEvent] = {
    EpisodeStatus.RENDER_READY: EpisodeEvent.STAGE_ADMITTED,
    EpisodeStatus.NEEDS_WORK: EpisodeEvent.RETRY_ADMITTED,
    EpisodeStatus.BLOCKED: EpisodeEvent.RESUMED,
}
#: 再開は **upload 自身が止めた** Episode だけ（入場トークンの workflow id で判定）。
RESUMABLE_STATUSES = frozenset({EpisodeStatus.NEEDS_WORK, EpisodeStatus.BLOCKED})
WORKFLOW_OWNED_JOB_TYPES = frozenset({JobType.UPLOAD_FINAL_VIDEO})
FINAL_VIDEO_FILENAME = "final.mp4"
#: 送信・status query の一時障害を Activity の中で吸収する回数と初回の待ち（指数 backoff）
DEFAULT_TRANSIENT_RETRIES = 4
DEFAULT_TRANSIENT_BACKOFF_SECONDS = 2.0
#: 予約行を読み直して状態機械をやり直す上限（並行試行との競合）
MAX_LEDGER_STEPS = 8
_RETRY_IN_ACTIVITY = (YouTubeTransientError, YouTubeRateLimitError)

HeartbeatFn = Callable[..., None]
SleepFn = Callable[[float], Awaitable[None]]


def admission_token(workflow_id: str, run_id: str) -> str:
    """入場トークン（ADR-0017 §8 と同形）。"""
    return f"{workflow_id}:{run_id}"


def parse_admission_token(token: str | None) -> tuple[str, str] | None:
    if not token or ":" not in token:
        return None
    workflow_id, run_id = token.rsplit(":", 1)
    if not workflow_id or not run_id:
        return None
    return workflow_id, run_id


def _activity_heartbeat(*details: Any) -> None:
    try:
        activity.info()
    except RuntimeError:
        return
    activity.heartbeat(*details)


def _attempt() -> int:
    try:
        return activity.info().attempt
    except RuntimeError:
        return 1


def reservation_key(upload_key: str, ledger_round: int) -> str:
    """予約の ``idempotency_key``。ラウンド1は upload key そのもの（ADR-0020 §3）。

    運用者の放棄の後のラウンドだけ、UNIQUE 制約を満たすため round を混ぜた鍵にする。
    """
    if ledger_round == 1:
        return upload_key
    return idempotency_key(
        provider=ProviderCall.YOUTUBE_UPLOAD.value, input_hash=upload_key, round=ledger_round
    )


@dataclass(frozen=True)
class _Outcome:
    video_id: str
    reconciled_by: str


@dataclass(frozen=True)
class _Final:
    meta: ArtifactMetadata
    artifact: FinalVideoArtifact


class UploadActivities:
    """外部依存をすべて注入する（INV-18）。"""

    heartbeat_interval_seconds: float = DEFAULT_UPLOAD_HEARTBEAT_INTERVAL_SECONDS

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
        workdir: WorkDirectory,
        uploader: VideoUploader,
        channel_id: str,
        uploads_paused: Callable[[], bool] = lambda: False,
        chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES,
        marker_lookup_attempts: int = DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS,
        marker_lookup_delay_seconds: float = DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS,
        transient_retries: int = DEFAULT_TRANSIENT_RETRIES,
        transient_backoff_seconds: float = DEFAULT_TRANSIENT_BACKOFF_SECONDS,
        run_inspector: WorkflowRunInspector | None = None,
        heartbeat: HeartbeatFn = _activity_heartbeat,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        if chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be positive")
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket
        self._workdir = workdir
        self._uploader = uploader
        self._channel_id = channel_id
        self._uploads_paused = uploads_paused
        self._chunk_bytes = chunk_bytes
        self._lookup_attempts = max(1, marker_lookup_attempts)
        self._lookup_delay = marker_lookup_delay_seconds
        self._transient_retries = max(0, transient_retries)
        self._transient_backoff = transient_backoff_seconds
        self._run_inspector = run_inspector
        self._heartbeat = heartbeat
        self._sleep = sleep

    def state_activities(self) -> Sequence[Callable[..., object]]:
        """``UPLOAD_TASK_QUEUE``（workflow と同じ queue）へ登録する。"""
        return [self.admit, self.mark_uploaded, self.record_failure]

    def media_activities(self) -> Sequence[Callable[..., object]]:
        """``UPLOAD_MEDIA_TASK_QUEUE``（並行数 1）へ登録する。"""
        return [self.upload_final_video]

    # ------------------------------------------------------------------ 入場

    @activity.defn(name=UPLOAD_ADMIT)
    async def admit(self, request: UploadAdmitRequest) -> UploadAdmitResult:
        """render の admit と同じ規則。``uploaded`` は入れない（再投稿しない）。"""
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(request.episode_id)
            if episode is None:
                return UploadAdmitResult(admitted=False, status="")
            owner = await episodes.get_workflow_id(request.episode_id)
            if episode.status is EpisodeStatus.IN_PROGRESS:
                if owner != token:
                    if not await self._stale_run(owner, request.workflow_id):
                        return UploadAdmitResult(admitted=False, status=episode.status.value)
                    logger.warning(
                        "upload admit takes over episode=%s from closed run %s", episode.id, owner
                    )
                    await episodes.set_workflow_id(request.episode_id, token)
                await self._ensure_job(session, request.episode_id)
                await session.commit()
                status = episode.status.value
            else:
                event = ADMIT_EVENTS.get(episode.status)
                if event is None:
                    return UploadAdmitResult(admitted=False, status=episode.status.value)
                if episode.status in RESUMABLE_STATUSES:
                    parsed = parse_admission_token(owner)
                    if parsed is None or parsed[0] != request.workflow_id:
                        return UploadAdmitResult(admitted=False, status=episode.status.value)
                updated = await episodes.apply_event(request.episode_id, event)
                await episodes.set_workflow_id(request.episode_id, token)
                await self._ensure_job(session, request.episode_id)
                await session.commit()
                status = updated.status.value
        return UploadAdmitResult(
            admitted=True,
            status=status,
            upload_timeout_seconds=await self._estimate_timeout(request.episode_id),
        )

    async def _estimate_timeout(self, episode_id: str) -> int:
        """現行 final_video のサイズから start_to_close を見積もる。読めなければ 0（既定値）。"""
        try:
            final = await self._current_final_video(episode_id)
        except (DomainError, KeyError, ValueError):
            return 0
        return upload_timeout_seconds(final.artifact.media.bytes)

    @staticmethod
    async def _ensure_job(session: AsyncSession, episode_id: str) -> str:
        jobs = JobRepository(session)
        job = await jobs.find_open(episode_id, JobType.UPLOAD_FINAL_VIDEO)
        if job is None:
            job = await jobs.create(episode_id=episode_id, type=JobType.UPLOAD_FINAL_VIDEO)
        return job.id

    async def _stale_run(self, owner: str | None, workflow_id: str) -> bool:
        parsed = parse_admission_token(owner)
        if parsed is None or parsed[0] != workflow_id or self._run_inspector is None:
            return False
        return await self._run_inspector.is_closed(parsed[0], parsed[1])

    # ------------------------------------------------------------------ 投稿

    @activity.defn(name=UPLOAD_FINAL_VIDEO)
    async def upload_final_video(self, request: UploadFinalVideoRequest) -> UploadFinalVideoResult:
        """現行 final_video を private で1回だけ投稿し、受領を保存する（docstring 冒頭の順序）。

        失敗は job に記録してから ``ApplicationError(type=<型名>, details=(job_id,))`` で送出する。
        cancel は送信を止め、予約（session）を残して再送出する（再実行で続きから）。
        """
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job_id = await self._ensure_job(session, request.episode_id)
            job = await jobs.get(job_id)
            if job is not None and job.status in {JobStatus.QUEUED, JobStatus.RETRYABLE_FAILED}:
                await jobs.start(job_id)
            await session.commit()
        secrets: list[str] = []
        attempt = _attempt()
        try:
            return await self._upload(request.episode_id, job_id, attempt, secrets)
        except asyncio.CancelledError:
            logger.warning(
                "upload cancelled episode=%s job=%s; the saved session is kept for resume",
                request.episode_id,
                job_id,
            )
            raise
        except Exception as exc:
            mapped = translate_youtube_error(exc, secrets)
            if isinstance(mapped, DomainError):
                mapped = _scrubbed(mapped, secrets)
            await self._mark_job_failed(job_id, translate_error(mapped))
            raise_activity_error(mapped, details=(job_id,))
        finally:
            self._cleanup(request.episode_id, job_id, attempt)

    async def _upload(
        self, episode_id: str, job_id: str, attempt: int, secrets: list[str]
    ) -> UploadFinalVideoResult:
        final = await self._current_final_video(episode_id)
        media = final.artifact.media
        upload_key = compute_upload_key(
            episode_id=episode_id,
            final_video_sha256=media.sha256,
            destination_id=self._channel_id,
        )
        script = await self._source_script(episode_id, final)
        metadata = build_youtube_metadata(
            upload_key=upload_key,
            title=script.title,
            hook=script.hook,
            narration=script.narration,
            language=script.language,
        )
        self._heartbeat("inputs_resolved")

        latest = await self._latest_reservation(episode_id, upload_key)
        if latest is not None and latest.status is ReservationStatus.SPENT:
            # (a) 投稿済み。YouTube を呼ばない
            outcome = _spent_outcome(latest)
            return await self._finish(
                episode_id, job_id, final, metadata, upload_key, latest, outcome, called=False
            )

        if self._uploads_paused():
            raise UploadsPausedError("UPLOADS_PAUSED is set; refusing to start or resume an upload")

        work = self._workdir.create(episode_id, job_id, attempt=attempt)
        path = work.input / FINAL_VIDEO_FILENAME
        await self._download_verified(final, path)

        reservation = await self._reserve(episode_id, upload_key, job_id)
        outcome = await self._drive(
            reservation.id, path, media.bytes, insert_body(metadata), upload_key, secrets
        )
        async with self._session_factory() as session:
            # 受領より先に video id と spent を commit（crash 後も再投稿なしで受領を作れる）
            reservation = await ProviderReservationRepository(session).record_upload_result(
                reservation.id, outcome.video_id, reconciled_by=outcome.reconciled_by
            )
            await session.commit()
        logger.info(
            "uploaded episode=%s video=%s reconciled_by=%s",
            episode_id,
            outcome.video_id,
            outcome.reconciled_by,
        )
        return await self._finish(
            episode_id, job_id, final, metadata, upload_key, reservation, outcome, called=True
        )

    # ------------------------------------------------------------------ 台帳

    async def _latest_reservation(
        self, episode_id: str, upload_key: str
    ) -> ProviderReservation | None:
        async with self._session_factory() as session:
            return await ProviderReservationRepository(session).find_latest_for_input(
                episode_id, ProviderCall.YOUTUBE_UPLOAD, None, upload_key
            )

    async def _reserve(self, episode_id: str, upload_key: str, job_id: str) -> ProviderReservation:
        """``reserved`` / ``spent`` の最新予約を返す。無い・abandoned 後だけ新ラウンド。"""
        for _ in range(3):
            async with self._session_factory() as session:
                repo = ProviderReservationRepository(session)
                latest = await repo.find_latest_for_input(
                    episode_id, ProviderCall.YOUTUBE_UPLOAD, None, upload_key
                )
                if latest is not None and latest.status is not ReservationStatus.ABANDONED:
                    return latest
                ledger_round = 1 if latest is None else latest.round + 1
                try:
                    created = await repo.reserve(
                        episode_id=episode_id,
                        provider=ProviderCall.YOUTUBE_UPLOAD,
                        idempotency_key=reservation_key(upload_key, ledger_round),
                        input_hash=upload_key,
                        round=ledger_round,
                        job_id=job_id,
                    )
                    await session.commit()
                    return created
                except IntegrityError:
                    # 並行する試行が同じラウンドを先に作った。読み直してそれを使う
                    await session.rollback()
        raise TransientError("could not reserve the upload after concurrent inserts")

    async def _reservation(self, reservation_id: str) -> ProviderReservation:
        async with self._session_factory() as session:
            found = await ProviderReservationRepository(session).get(reservation_id)
        if found is None:
            raise UploadOutcomeUnknownError(f"upload reservation {reservation_id} disappeared")
        return found

    async def _save_session(self, reservation_id: str, uri: str, replaces: str | None) -> bool:
        async with self._session_factory() as session:
            saved = await ProviderReservationRepository(session).record_upload_session(
                reservation_id, uri, replaces=replaces
            )
            await session.commit()
        return saved

    async def _dispatch(self, reservation_id: str, uri: str) -> bool:
        async with self._session_factory() as session:
            ok = await ProviderReservationRepository(session).mark_upload_dispatched(
                reservation_id, uri
            )
            await session.commit()
        return ok

    async def _drive(
        self,
        reservation_id: str,
        path: Path,
        total: int,
        body: dict[str, Any],
        upload_key: str,
        secrets: list[str],
    ) -> _Outcome:
        """予約行の状態から、送る・照会する・照合するのどれかを選ぶ（冒頭の 6）。"""
        for _ in range(MAX_LEDGER_STEPS):
            row = await self._reservation(reservation_id)
            if row.status is ReservationStatus.SPENT:
                return _spent_outcome(row)
            if row.status is not ReservationStatus.RESERVED:
                raise UploadOutcomeUnknownError(
                    f"upload reservation {reservation_id} was closed ({row.status.value}) "
                    "while uploading; re-run the upload"
                )
            if row.provider_job_ref is None:
                # (b) session なし。session の作成は動画を作らないので繰り返してよい
                session = await self._start_session(body, total, secrets)
                if not await self._save_session(reservation_id, session.uri, None):
                    continue  # 別の試行が先に session を保存した
                if not await self._dispatch(reservation_id, session.uri):
                    continue
                result = await self._send(session, path, 0)
            else:
                session = UploadSessionRef(
                    uri=row.provider_job_ref, total_bytes=total, content_type=UPLOAD_CONTENT_TYPE
                )
                secrets.append(session.uri)
                progress, _ = await self._query(session)
                if row.dispatched_at is None:
                    # (c) session はあるが bytes を1つも送っていない
                    if isinstance(progress, UploadExpired):
                        fresh = await self._start_session(body, total, secrets)
                        if not await self._save_session(
                            reservation_id, fresh.uri, replaces=session.uri
                        ):
                            continue
                        if not await self._dispatch(reservation_id, fresh.uri):
                            continue
                        result = await self._send(fresh, path, 0)
                    elif isinstance(progress, UploadCompleted):
                        result = _Outcome(progress.video_id, "status_query")
                    else:
                        if not await self._dispatch(reservation_id, session.uri):
                            continue
                        result = await self._send(session, path, progress.next_offset)
                elif isinstance(progress, UploadCompleted):
                    # (d) dispatch 済み。完了していた（応答を失った / 記録前の crash）
                    result = _Outcome(progress.video_id, "status_query")
                elif isinstance(progress, UploadExpired):
                    result = progress
                else:
                    result = await self._send(session, path, progress.next_offset)
            if isinstance(result, _Outcome):
                return result
            # dispatch 済みの session が失効した。bytes が届いていたかもしれない
            return await self._reconcile_by_marker(upload_marker(upload_key))
        raise TransientError("upload reservation kept changing under concurrent attempts")

    # ------------------------------------------------------------------ YouTube

    async def _start_session(
        self, body: dict[str, Any], total: int, secrets: list[str]
    ) -> UploadSessionRef:
        session = await self._beating(
            "start_session", self._uploader.start_session(body, total, UPLOAD_CONTENT_TYPE)
        )
        secrets.append(session.uri)
        return session

    async def _query(self, session: UploadSessionRef) -> tuple[UploadProgress, bool]:
        """status query。一時障害は backoff して繰り返し、尽きたら ``TransientError``。"""
        last: BaseException | None = None
        for attempt in range(self._transient_retries + 1):
            if attempt:
                await self._pause(self._transient_backoff * 2 ** (attempt - 1), "backoff")
            try:
                return await self._beating("status", self._uploader.query_status(session)), True
            except _RETRY_IN_ACTIVITY as exc:
                last = exc
        raise TransientError(
            f"upload status query kept failing ({type(last).__name__}); will retry the activity"
        )

    async def _send(
        self, session: UploadSessionRef, path: Path, offset: int
    ) -> _Outcome | UploadExpired:
        """``offset`` から最後まで送る。一時障害は status query で受理位置を確かめて再開する。"""
        total = session.total_bytes
        stalls = 0
        while True:
            if offset >= total:
                progress, _ = await self._query(session)
                if isinstance(progress, UploadCompleted):
                    return _Outcome(progress.video_id, "status_query")
                if isinstance(progress, UploadExpired):
                    return progress
                if progress.next_offset >= total:
                    raise TransientError("upload accepted every byte but is not complete yet")
                offset = progress.next_offset
            length = min(self._chunk_bytes, total - offset)
            chunk = await asyncio.to_thread(_read_chunk, path, offset, length)
            self._heartbeat("uploading", offset, total)
            via = "upload_response"
            try:
                progress = await self._beating(
                    "uploading", self._uploader.send_chunk(session, offset, chunk, total)
                )
            except _RETRY_IN_ACTIVITY:
                stalls += 1
                if stalls > self._transient_retries:
                    raise TransientError(
                        "upload chunk kept failing; will retry the activity from the saved session"
                    ) from None
                await self._pause(self._transient_backoff * 2 ** (stalls - 1), "backoff")
                progress, _ = await self._query(session)
                via = "status_query"
            if isinstance(progress, UploadCompleted):
                return _Outcome(progress.video_id, via)
            if isinstance(progress, UploadExpired):
                return progress
            if progress.next_offset > offset:
                stalls = 0 if via == "upload_response" else stalls
            elif via == "upload_response":
                stalls += 1
                if stalls > self._transient_retries:
                    raise TransientError("upload made no progress; will retry the activity")
            offset = progress.next_offset

    async def _reconcile_by_marker(self, marker: str) -> _Outcome:
        """失効した dispatch 済み session の結果を uploads playlist のマーカーで確かめる。"""
        for attempt in range(self._lookup_attempts):
            if attempt:
                await self._pause(self._lookup_delay, "marker_lookup_wait")
            try:
                video_id = await self._beating(
                    "marker_lookup", self._uploader.find_video_by_marker(marker)
                )
            except (*_RETRY_IN_ACTIVITY, YouTubeQuotaError):
                logger.warning("upload marker lookup failed (attempt %s)", attempt + 1)
                continue
            if video_id is not None:
                return _Outcome(video_id, "marker_lookup")
        raise UploadOutcomeUnknownError(
            f"upload session expired after bytes were sent and marker {marker} was not found "
            f"after {self._lookup_attempts} lookups; check the channel, then follow "
            "docs/operations/upload-worker.md (no new session is opened automatically)"
        )

    async def _pause(self, seconds: float, label: str) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            step = min(remaining, self.heartbeat_interval_seconds)
            await self._sleep(step)
            remaining -= step
            self._heartbeat(label)

    async def _beating[T](self, label: str, work: Awaitable[T]) -> T:
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.heartbeat_interval_seconds)
                if done:
                    return task.result()
                self._heartbeat(label)
        except asyncio.CancelledError:
            task.cancel()
            raise

    # ------------------------------------------------------------------ 入力

    async def _load_json(self, meta: ArtifactMetadata, label: str) -> dict[str, Any]:
        try:
            payload = await self._store.get_json(meta.object_key)
        except KeyError as exc:
            raise UploadInputMissingError(
                f"{label} artifact {meta.id} object is missing from the store"
            ) from exc
        except ValueError as exc:
            raise UploadIntegrityError(f"{label} artifact {meta.id} is not valid JSON") from exc
        digest = sha256_hex(canonical_json_bytes(payload))
        if digest != meta.sha256:
            raise UploadIntegrityError(
                f"{label} artifact {meta.id} sha256 mismatch: "
                f"stored={digest} metadata={meta.sha256}"
            )
        return payload

    async def _current_final_video(self, episode_id: str) -> _Final:
        from contracts.artifacts import parse_final_video

        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.FINAL_VIDEO
            )
        if meta is None:
            raise UploadInputMissingError(f"no current final_video for episode {episode_id}")
        payload = await self._load_json(meta, "final_video")
        try:
            artifact = parse_final_video(payload)
        except ValueError as exc:
            raise UploadIntegrityError(
                f"final_video artifact {meta.id} invalid: {str(exc)[:300]}"
            ) from exc
        if artifact.episode_id != episode_id:
            raise UploadIntegrityError(f"final_video {meta.id} belongs to another episode")
        return _Final(meta=meta, artifact=artifact)

    async def _source_script(self, episode_id: str, final: _Final) -> ScriptArtifact:
        ref = final.artifact.source_script
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).get(ref.artifact_id)
        if meta is None or meta.artifact_type is not ArtifactType.SCRIPT:
            raise UploadInputMissingError(f"script {ref.artifact_id} of the final_video not found")
        if meta.sha256 != ref.sha256 or str(meta.episode_id) != episode_id:
            raise UploadIntegrityError(
                f"script {ref.artifact_id} sha256 {meta.sha256} != referenced {ref.sha256}"
            )
        payload = await self._load_json(meta, "script")
        try:
            return parse_script_artifact(payload)
        except ValueError as exc:
            raise UploadIntegrityError(f"script {meta.id} invalid: {str(exc)[:300]}") from exc

    async def _download_verified(self, final: _Final, path: Path) -> None:
        media = final.artifact.media
        try:
            actual = await self._beating(
                "download", self._store.download_to(media.object_key, path)
            )
        except KeyError as exc:
            raise UploadInputMissingError(
                f"final video media is missing at {media.object_key}"
            ) from exc
        size = path.stat().st_size
        if actual != media.sha256 or size != media.bytes:
            raise UploadIntegrityError(
                f"final video media mismatch at {media.object_key}: sha256={actual} "
                f"expected={media.sha256} bytes={size} expected={media.bytes}"
            )
        self._heartbeat("media_verified")

    def _cleanup(self, episode_id: str, job_id: str, attempt: int) -> None:
        try:
            self._workdir.cleanup(episode_id, job_id, attempt=attempt)
        except DomainError:
            logger.warning("upload work directory cleanup failed job=%s", job_id, exc_info=True)

    # ------------------------------------------------------------------ 受領

    async def _finish(
        self,
        episode_id: str,
        job_id: str,
        final: _Final,
        metadata: YouTubeVideoMetadata,
        upload_key: str,
        reservation: ProviderReservation,
        outcome: _Outcome,
        *,
        called: bool,
    ) -> UploadFinalVideoResult:
        async with self._session_factory() as session:
            existing = await ArtifactMetadataRepository(session).find_current(
                episode_id, ArtifactType.UPLOAD_RECEIPT, upload_key
            )
        meta = await self._reusable_receipt(existing, outcome.video_id)
        wrote = False
        if meta is None:
            meta = await self._write_receipt(
                episode_id, job_id, final, metadata, upload_key, outcome
            )
            wrote = True
        async with self._session_factory() as session:
            if reservation.outcome_artifact_id != meta.id:
                await ProviderReservationRepository(session).attach_artifact(
                    reservation.id, meta.id
                )
            await _finish_job(session, job_id, skipped=not called and not wrote)
            await session.commit()
        return UploadFinalVideoResult(
            artifact_id=meta.id,
            sha256=meta.sha256,
            version=meta.version,
            video_id=outcome.video_id,
            skipped=not called,
            reconciled_by=outcome.reconciled_by,
            job_id=job_id,
        )

    async def _reusable_receipt(
        self, existing: ArtifactMetadata | None, video_id: str
    ) -> ArtifactMetadata | None:
        if existing is None:
            return None
        try:
            receipt = parse_upload_receipt(await self._load_json(existing, "upload_receipt"))
        except (DomainError, ValueError):
            logger.warning("upload_receipt %s is not readable; rewriting", existing.id)
            return None
        return existing if receipt.video_id == video_id else None

    async def _write_receipt(
        self,
        episode_id: str,
        job_id: str,
        final: _Final,
        metadata: YouTubeVideoMetadata,
        upload_key: str,
        outcome: _Outcome,
    ) -> ArtifactMetadata:
        payload = build_upload_receipt(
            episode_id=episode_id,
            source_final_video={
                "artifact_id": final.meta.id,
                "sha256": final.meta.sha256,
                "schema_version": final.meta.schema_version,
            },
            destination={"platform": "youtube", "channel_id": self._channel_id},
            video_id=outcome.video_id,
            metadata=metadata.model_dump(mode="json"),
            upload_key=upload_key,
            bytes=final.artifact.media.bytes,
            reconciled_by=outcome.reconciled_by,
        )
        digest = sha256_hex(canonical_json_bytes(payload))
        key = artifact_object_key(episode_id, ArtifactType.UPLOAD_RECEIPT.value, digest)
        stored = await self._store.put_json(key, payload)
        readback = sha256_hex(canonical_json_bytes(await self._store.get_json(stored.key)))
        if stored.sha256 != digest or readback != digest:
            raise TransientError(f"upload_receipt readback sha256 mismatch at {stored.key}")
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=episode_id,
                artifact_type=ArtifactType.UPLOAD_RECEIPT,
                schema_version=UPLOAD_ARTIFACT_SCHEMA_VERSION,
                bucket=self._bucket,
                object_key=stored.key,
                sha256=digest,
                size_bytes=stored.size,
                produced_by_job_id=job_id,
                input_hash=upload_key,
            )
            await session.commit()
        return meta

    async def _mark_job_failed(self, job_id: str, exc: BaseException) -> None:
        failure_class = classify_failure(exc)
        try:
            async with self._session_factory() as session:
                jobs = JobRepository(session)
                job = await jobs.get(job_id)
                if job is None or job.status in JOB_TERMINAL_STATUSES:
                    return
                if (
                    job.status is JobStatus.RETRYABLE_FAILED
                    and failure_class in RETRYABLE_FAILURE_CLASSES
                ):
                    return
                await jobs.record_failure(
                    job_id,
                    event=job_event_for_failure(failure_class),
                    failure_class=failure_class,
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
                await session.commit()
        except Exception:
            logger.warning("upload job failure could not be recorded job=%s", job_id, exc_info=True)

    # ------------------------------------------------------------------ 完了 / 失敗

    @activity.defn(name=UPLOAD_MARK_UPLOADED)
    async def mark_uploaded(self, request: UploadMarkUploadedRequest) -> UploadMarkUploadedResult:
        token = admission_token(request.workflow_id, request.run_id)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            current = await episodes.get(request.episode_id)
            if current is None:
                return UploadMarkUploadedResult(status="", owned=False)
            if not await _owns(episodes, request.episode_id, token, "mark_uploaded"):
                return UploadMarkUploadedResult(status=current.status.value, owned=False)
            if current.status is EpisodeStatus.UPLOADED:
                return UploadMarkUploadedResult(status=current.status.value)
            episode = await episodes.apply_event(request.episode_id, EpisodeEvent.UPLOAD_SUCCEEDED)
            await session.commit()
            return UploadMarkUploadedResult(status=episode.status.value)

    @activity.defn(name=UPLOAD_RECORD_FAILURE)
    async def record_failure(self, request: UploadRecordFailureRequest) -> UploadFailureOutcome:
        """render の record_failure と同じ規則（使い切りは blocked）。"""
        failure_class = FailureClass(request.failure_class)
        token = admission_token(request.workflow_id, request.run_id)
        summary = scrub(request.error_summary)
        async with self._session_factory() as session:
            episodes = EpisodeRepository(session)
            if not await _owns(episodes, request.episode_id, token, "record_failure"):
                current = await episodes.get(request.episode_id)
                return UploadFailureOutcome(
                    episode_status=current.status.value if current else "", owned=False
                )
            await _settle_jobs(session, request, failure_class, summary)
            current = await episodes.get(request.episode_id)
            if current is not None and current.status is not EpisodeStatus.IN_PROGRESS:
                await session.commit()
                return UploadFailureOutcome(episode_status=current.status.value)
            episode = await episodes.apply_event(
                request.episode_id,
                episode_event_for_failure(failure_class),
                blocked_reason=f"{failure_class.value}: {summary}"[:1000],
            )
            if request.retry_exhausted and episode.status is EpisodeStatus.NEEDS_WORK:
                episode = await episodes.apply_event(
                    request.episode_id, EpisodeEvent.RETRY_BUDGET_EXHAUSTED
                )
            await session.commit()
            return UploadFailureOutcome(episode_status=episode.status.value)


def _read_chunk(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def _spent_outcome(reservation: ProviderReservation) -> _Outcome:
    if not reservation.provider_result_ref:
        raise UploadOutcomeUnknownError(
            f"upload reservation {reservation.id} is spent without a video id; record the video "
            "id (docs/operations/upload-worker.md)"
        )
    by = reservation.reconciled_by
    reconciled_by = (
        by if by in {"upload_response", "status_query", "marker_lookup"} else "marker_lookup"
    )
    return _Outcome(reservation.provider_result_ref, cast(str, reconciled_by))


def _scrubbed(exc: DomainError, secrets: list[str]) -> DomainError:
    text = str(exc)
    clean = scrub(text, secrets)
    if clean == text:
        return exc
    replacement = type(exc)(clean)
    replacement.__cause__ = None
    return replacement


async def _finish_job(session: AsyncSession, job_id: str, *, skipped: bool) -> None:
    jobs = JobRepository(session)
    job = await jobs.get(job_id)
    if job is None or job.status in JOB_TERMINAL_STATUSES:
        return
    if skipped:
        await jobs.mark_skipped(job_id)
        return
    if job.status is not JobStatus.RUNNING:
        await jobs.start(job_id)
    await jobs.succeed(job_id)


async def _settle_jobs(
    session: AsyncSession,
    request: UploadRecordFailureRequest,
    failure_class: FailureClass,
    summary: str,
) -> None:
    jobs = JobRepository(session)
    for job in await jobs.list_for_episode(request.episode_id):
        if job.type not in WORKFLOW_OWNED_JOB_TYPES or job.status in JOB_TERMINAL_STATUSES:
            continue
        if job.id == request.job_id or failure_class is FailureClass.PERMANENT:
            if job.status is JobStatus.RETRYABLE_FAILED:
                event = (
                    JobEvent.ATTEMPTS_EXHAUSTED
                    if failure_class in RETRYABLE_FAILURE_CLASSES
                    else JobEvent.PERMANENT_FAILURE
                )
            else:
                event = job_event_for_failure(failure_class)
            await jobs.record_failure(
                job.id, event=event, failure_class=failure_class, error_summary=summary
            )
        elif job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            await jobs.record_failure(
                job.id,
                event=JobEvent.RETRYABLE_FAILURE,
                failure_class=FailureClass.RETRYABLE,
                error_summary=f"interrupted: upload stopped ({summary})",
            )


async def _owns(episodes: EpisodeRepository, episode_id: str, token: str, action: str) -> bool:
    owner = await episodes.get_workflow_id(episode_id)
    if owner == token:
        return True
    logger.warning(
        "upload %s refused: admission token mismatch episode=%s recorded=%s caller=%s",
        action,
        episode_id,
        owner,
        token,
    )
    return False


__all__ = [
    "ADMISSIBLE_STATUSES",
    "ADMIT_EVENTS",
    "MAX_LEDGER_STEPS",
    "RESUMABLE_STATUSES",
    "WORKFLOW_OWNED_JOB_TYPES",
    "UploadActivities",
    "admission_token",
    "parse_admission_token",
    "reservation_key",
]
