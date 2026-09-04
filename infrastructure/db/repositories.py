"""リポジトリ。状態遷移は必ず domain の表を通す（AGENTS.md §8）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.states import (
    DEFAULT_MAX_ATTEMPTS,
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
)
from domain.artifact.entities import ArtifactMetadata
from domain.episode.entities import Episode
from domain.episode.transitions import EpisodeEvent, Rejected, transition_episode
from domain.errors import InvalidTransitionError
from domain.job.entities import Job
from domain.job.transitions import JobEvent, transition_job
from infrastructure.db.models import ArtifactMetadataRow, EpisodeRow, JobRow


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _now() -> datetime:
    return datetime.now(UTC)


def _to_episode(row: EpisodeRow) -> Episode:
    return Episode(
        id=str(row.id),
        status=EpisodeStatus(row.status),
        topic=row.topic,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_job(row: JobRow) -> Job:
    return Job(
        id=str(row.id),
        episode_id=str(row.episode_id),
        type=JobType(row.type),
        status=JobStatus(row.status),
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        failure_class=FailureClass(row.failure_class) if row.failure_class else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_artifact(row: ArtifactMetadataRow) -> ArtifactMetadata:
    return ArtifactMetadata(
        id=str(row.id),
        episode_id=str(row.episode_id),
        artifact_type=ArtifactType(row.artifact_type),
        schema_version=row.schema_version,
        bucket=row.bucket,
        object_key=row.object_key,
        sha256=row.sha256,
        created_at=row.created_at,
    )


class EpisodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, topic: str | None = None) -> Episode:
        row = EpisodeRow(
            id=uuid.uuid4(),
            status=EpisodeStatus.PLANNED.value,
            topic=topic,
            created_at=_now(),
            updated_at=_now(),
            status_changed_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_episode(row)

    async def _row(self, episode_id: uuid.UUID | str) -> EpisodeRow | None:
        return await self._session.get(EpisodeRow, _as_uuid(episode_id))

    async def get(self, episode_id: uuid.UUID | str) -> Episode | None:
        row = await self._row(episode_id)
        return _to_episode(row) if row else None

    async def apply_event(
        self,
        episode_id: uuid.UUID | str,
        event: EpisodeEvent,
        *,
        blocked_reason: str | None = None,
    ) -> Episode:
        """遷移表を通して状態を進める。表に無ければ書き込まずに例外。"""
        row = await self._row(episode_id)
        if row is None:
            raise InvalidTransitionError(f"episode not found: {episode_id}")

        result = transition_episode(EpisodeStatus(row.status), event)
        if isinstance(result, Rejected):
            raise InvalidTransitionError(result.reason)

        row.status = result.value
        row.status_changed_at = _now()
        row.updated_at = _now()
        if blocked_reason is not None:
            row.blocked_reason = blocked_reason
        await self._session.flush()
        return _to_episode(row)

    async def set_workflow_id(self, episode_id: uuid.UUID | str, workflow_id: str) -> None:
        """Temporal参照は相関のためだけに持つ。状態の権威ではない（INV-8）。"""
        row = await self._row(episode_id)
        if row is not None:
            row.workflow_id = workflow_id
            await self._session.flush()


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        episode_id: uuid.UUID | str,
        type: JobType,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Job:
        row = JobRow(
            id=uuid.uuid4(),
            episode_id=_as_uuid(episode_id),
            type=type.value,
            status=JobStatus.QUEUED.value,
            attempts=0,
            max_attempts=max_attempts,
            created_at=_now(),
            updated_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_job(row)

    async def _row(self, job_id: uuid.UUID | str) -> JobRow | None:
        return await self._session.get(JobRow, _as_uuid(job_id))

    async def get(self, job_id: uuid.UUID | str) -> Job | None:
        row = await self._row(job_id)
        return _to_job(row) if row else None

    async def list_for_episode(self, episode_id: uuid.UUID | str) -> list[Job]:
        stmt = (
            select(JobRow)
            .where(JobRow.episode_id == _as_uuid(episode_id))
            .order_by(JobRow.created_at, JobRow.id)
        )
        return [_to_job(row) for row in (await self._session.scalars(stmt)).all()]

    async def _apply(self, job_id: uuid.UUID | str, event: JobEvent) -> JobRow:
        row = await self._row(job_id)
        if row is None:
            raise InvalidTransitionError(f"job not found: {job_id}")
        result = transition_job(JobStatus(row.status), event)
        if isinstance(result, Rejected):
            raise InvalidTransitionError(result.reason)
        row.status = result.value
        row.updated_at = _now()
        await self._session.flush()
        return row

    async def start(self, job_id: uuid.UUID | str) -> Job:
        """Activity開始時に呼ぶ。初回は QUEUED から、retryは RETRYABLE_FAILED から。"""
        row = await self._row(job_id)
        if row is None:
            raise InvalidTransitionError(f"job not found: {job_id}")
        event = (
            JobEvent.RETRY_ADMITTED
            if JobStatus(row.status) is JobStatus.RETRYABLE_FAILED
            else JobEvent.STARTED
        )
        row = await self._apply(job_id, event)
        row.attempts += 1
        row.updated_at = _now()
        await self._session.flush()
        return _to_job(row)

    async def succeed(self, job_id: uuid.UUID | str) -> Job:
        row = await self._apply(job_id, JobEvent.SUCCEEDED)
        row.failure_class = None
        row.error_summary = None
        await self._session.flush()
        return _to_job(row)

    async def record_failure(
        self,
        job_id: uuid.UUID | str,
        *,
        event: JobEvent,
        failure_class: FailureClass,
        error_summary: str | None = None,
    ) -> Job:
        row = await self._apply(job_id, event)
        row.failure_class = failure_class.value
        # 人間向けの説明。機械判定にこの文字列を使わない（docs/domain/job.md）。
        row.error_summary = (error_summary or "")[:2000] or None
        await self._session.flush()
        return _to_job(row)


class ArtifactMetadataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        episode_id: uuid.UUID | str,
        artifact_type: ArtifactType,
        schema_version: str,
        bucket: str,
        object_key: str,
        sha256: str,
        size_bytes: int | None = None,
        produced_by_job_id: uuid.UUID | str | None = None,
    ) -> ArtifactMetadata:
        """同じ内容の再記録は既存行を返す（INV-17）。UNIQUE制約と同じ鍵で引く。"""
        episode_uuid = _as_uuid(episode_id)
        stmt = select(ArtifactMetadataRow).where(
            ArtifactMetadataRow.episode_id == episode_uuid,
            ArtifactMetadataRow.artifact_type == artifact_type.value,
            ArtifactMetadataRow.sha256 == sha256,
        )
        existing = (await self._session.scalars(stmt)).first()
        if existing is not None:
            return _to_artifact(existing)

        row = ArtifactMetadataRow(
            id=uuid.uuid4(),
            episode_id=episode_uuid,
            artifact_type=artifact_type.value,
            schema_version=schema_version,
            bucket=bucket,
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            produced_by_job_id=_as_uuid(produced_by_job_id) if produced_by_job_id else None,
            created_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_artifact(row)

    async def list_for_episode(self, episode_id: uuid.UUID | str) -> list[ArtifactMetadata]:
        stmt = (
            select(ArtifactMetadataRow)
            .where(ArtifactMetadataRow.episode_id == _as_uuid(episode_id))
            .order_by(ArtifactMetadataRow.created_at, ArtifactMetadataRow.id)
        )
        return [_to_artifact(row) for row in (await self._session.scalars(stmt)).all()]
