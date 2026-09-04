"""骨組みworkflowのActivity群。

Activityは「入力から出力Artifactを作って結果を返す」だけ。
**次に何をするかは決めない**（INV-4）。他のworkerをimportしない（INV-3）。
すべて冪等（INV-17）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.artifacts import DUMMY_ARTIFACT_SCHEMA_VERSION, build_dummy_artifact
from contracts.states import ArtifactType, EpisodeStatus, FailureClass, JobStatus, JobType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from domain.job.transitions import JobEvent, episode_event_for_failure, job_event_for_failure
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.storage.artifact_store import ArtifactStore


@dataclass
class EpisodeRef:
    episode_id: str


@dataclass
class CreateJobRequest:
    episode_id: str
    max_attempts: int


@dataclass
class ProduceArtifactRequest:
    episode_id: str
    job_id: str


@dataclass
class ArtifactRef:
    artifact_id: str
    bucket: str
    object_key: str
    sha256: str
    schema_version: str


@dataclass
class RecordFailureRequest:
    episode_id: str
    job_id: str
    failure_class: str
    error_summary: str
    retry_exhausted: bool


@dataclass
class FailureOutcome:
    episode_status: str


class DummyActivities:
    """Activityの実装をまとめた依存注入用のクラス。

    session factory と ArtifactStore を注入するので、テストは実サービスなしで
    同じコードパスを走らせられる（INV-18）。
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket

    def all_activities(self) -> Sequence[Callable[..., object]]:
        """Workerへ登録するActivity。ここが登録漏れの唯一の防波堤。"""
        return [
            self.mark_episode_in_progress,
            self.create_dummy_job,
            self.produce_dummy_artifact,
            self.complete_episode,
            self.record_failure,
        ]

    @activity.defn(name="mark_episode_in_progress")
    async def mark_episode_in_progress(self, request: EpisodeRef) -> None:
        async with self._session_factory() as session:
            await EpisodeRepository(session).apply_event(
                request.episode_id, EpisodeEvent.WORKFLOW_STARTED
            )
            await session.commit()

    @activity.defn(name="create_dummy_job")
    async def create_dummy_job(self, request: CreateJobRequest) -> str:
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            existing = [
                job
                for job in await jobs.list_for_episode(request.episode_id)
                if job.type is JobType.DUMMY
            ]
            if existing:
                # Activityの再実行でJobを重複生成しない（INV-17）。
                return existing[0].id

            job = await jobs.create(
                episode_id=request.episode_id,
                type=JobType.DUMMY,
                max_attempts=request.max_attempts,
            )
            await session.commit()
            return job.id

    @activity.defn(name="produce_dummy_artifact")
    async def produce_dummy_artifact(self, request: ProduceArtifactRequest) -> ArtifactRef:
        """Job=running → MinIO保存 → メタデータ記録 → Job=succeeded。

        失敗した場合はJobを retryable_failed / terminal_failed に落としてから
        例外を再送出する。Episodeの状態は**ここでは触らない**（それはworkflowの判断）。
        """
        async with self._session_factory() as session:
            await JobRepository(session).start(request.job_id)
            await session.commit()

        try:
            payload = build_dummy_artifact(episode_id=request.episode_id)
            body = canonical_json_bytes(payload)
            digest = sha256_hex(body)
            key = artifact_object_key(request.episode_id, ArtifactType.DUMMY.value, digest)

            put = await self._store.put_json(key, payload)

            async with self._session_factory() as session:
                meta = await ArtifactMetadataRepository(session).record(
                    episode_id=request.episode_id,
                    artifact_type=ArtifactType.DUMMY,
                    schema_version=DUMMY_ARTIFACT_SCHEMA_VERSION,
                    bucket=self._bucket,
                    object_key=put.key,
                    sha256=put.sha256,
                    size_bytes=put.size,
                    produced_by_job_id=request.job_id,
                )
                await JobRepository(session).succeed(request.job_id)
                await session.commit()
        except Exception as exc:
            await self._mark_job_failed(request.job_id, exc)
            raise

        return ArtifactRef(
            artifact_id=meta.id,
            bucket=meta.bucket,
            object_key=meta.object_key,
            sha256=meta.sha256,
            schema_version=meta.schema_version,
        )

    @activity.defn(name="complete_episode")
    async def complete_episode(self, request: EpisodeRef) -> str:
        async with self._session_factory() as session:
            episode = await EpisodeRepository(session).apply_event(
                request.episode_id, EpisodeEvent.SKELETON_COMPLETED
            )
            await session.commit()
            return episode.status.value

    @activity.defn(name="record_failure")
    async def record_failure(self, request: RecordFailureRequest) -> FailureOutcome:
        """失敗の確定。Episodeの遷移先は失敗クラスからのみ決まる（INV-12）。"""
        failure_class = FailureClass(request.failure_class)
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(request.job_id)
            if job is not None and job.status not in {
                JobStatus.TERMINAL_FAILED,
                JobStatus.SUCCEEDED,
            }:
                await jobs.record_failure(
                    request.job_id,
                    event=JobEvent.ATTEMPTS_EXHAUSTED
                    if job.status is JobStatus.RETRYABLE_FAILED
                    else job_event_for_failure(failure_class),
                    failure_class=failure_class,
                    error_summary=request.error_summary,
                )

            episodes = EpisodeRepository(session)
            episode = await episodes.apply_event(
                request.episode_id,
                episode_event_for_failure(failure_class),
                blocked_reason=f"{failure_class.value}: {request.error_summary}"[:1000],
            )
            if request.retry_exhausted and episode.status is EpisodeStatus.NEEDS_WORK:
                # 枠を使い切った retryable 失敗は blocked（人間へ）。failed にはしない。
                episode = await episodes.apply_event(
                    request.episode_id, EpisodeEvent.RETRY_BUDGET_EXHAUSTED
                )
            await session.commit()
            return FailureOutcome(episode_status=episode.status.value)

    async def _mark_job_failed(self, job_id: str, exc: BaseException) -> None:
        from domain.errors import classify_failure

        failure_class = classify_failure(exc)
        async with self._session_factory() as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id)
            if job is None or job.status in {JobStatus.TERMINAL_FAILED, JobStatus.SUCCEEDED}:
                return
            await jobs.record_failure(
                job_id,
                event=job_event_for_failure(failure_class),
                failure_class=failure_class,
                error_summary=f"{type(exc).__name__}: {exc}",
            )
            await session.commit()
