"""リポジトリ。状態遷移は必ず domain の表を通す（AGENTS.md §8）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.operations import (
    ClaimOutcome,
    DailyEpisodeSlot,
    DailySlotClaim,
    OperationalSwitch,
)
from contracts.states import (
    DEFAULT_MAX_ATTEMPTS,
    JOB_TERMINAL_STATUSES,
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
    ProviderCall,
    ReservationStatus,
)
from contracts.topic_planning import (
    AnalyticsMode,
    DuplicateLevel,
    MemoryItem,
    TopicPlanStatus,
)
from domain.artifact.entities import ArtifactMetadata
from domain.episode.entities import Episode
from domain.episode.transitions import EpisodeEvent, Rejected, transition_episode
from domain.errors import InvalidTransitionError, UnreconciledReservationError
from domain.job.entities import Job
from domain.job.transitions import JobEvent, transition_job
from domain.provider.reservations import ReservationEvent, transition_reservation
from infrastructure.db.models import (
    AnalyticsSnapshotRow,
    ArtifactMetadataRow,
    DailyEpisodeSlotRow,
    EpisodeRow,
    JobRow,
    OperationalSwitchRow,
    ProviderReservationRow,
    TopicCandidateRow,
    TopicPlanRow,
)


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
        topic_plan_id=str(row.topic_plan_id) if row.topic_plan_id else None,
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
        scene_id=row.scene_id,
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
        scene_id=row.scene_id,
        version=row.version,
    )


def _artifact_scene_filter(scene_id: str | None):
    """scene キーの一致条件（ADR-0018）。None は「Episode 単位の行」だけを指す。"""
    if scene_id is None:
        return ArtifactMetadataRow.scene_id.is_(None)
    return ArtifactMetadataRow.scene_id == scene_id


class EpisodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, topic: str | None = None, topic_plan_id: uuid.UUID | str | None = None
    ) -> Episode:
        row = EpisodeRow(
            id=uuid.uuid4(),
            status=EpisodeStatus.PLANNED.value,
            topic=topic,
            topic_plan_id=_as_uuid(topic_plan_id) if topic_plan_id is not None else None,
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

        current = EpisodeStatus(row.status)
        result = transition_episode(current, event)
        if isinstance(result, Rejected):
            raise InvalidTransitionError(result.reason)

        # compare-and-set: 読んだ状態のままの行だけを進める。並行する2つの入場が同じ
        # ``render_ready`` を読んでも、進められるのは片方だけ（もう片方は InvalidTransitionError）。
        now = _now()
        values: dict[str, object] = {
            "status": result.value,
            "status_changed_at": now,
            "updated_at": now,
        }
        if blocked_reason is not None:
            values["blocked_reason"] = blocked_reason
        outcome = await self._session.execute(
            update(EpisodeRow)
            .where(EpisodeRow.id == row.id, EpisodeRow.status == current.value)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        fresh = await self._session.get(EpisodeRow, row.id, populate_existing=True)
        if getattr(outcome, "rowcount", 0) != 1 or fresh is None:
            actual = fresh.status if fresh is not None else "missing"
            raise InvalidTransitionError(
                f"episode transition rejected: concurrent change "
                f"(expected {current.value}, found {actual}) + {event.value}"
            )
        return _to_episode(fresh)

    async def get_workflow_id(self, episode_id: uuid.UUID | str) -> str | None:
        """相関用に記録した workflow id（無ければ ``None``）。"""
        row = await self._row(episode_id)
        return row.workflow_id if row is not None else None

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
        scene_id: str | None = None,
    ) -> Job:
        row = JobRow(
            scene_id=scene_id,
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

    async def find_open(
        self, episode_id: uuid.UUID | str, type: JobType, scene_id: str | None = None
    ) -> Job | None:
        """非終端の job を ``(type, scene_id)`` で引く（再実行で重複生成しない / ADR-0018）。"""
        scene_filter = (
            JobRow.scene_id.is_(None) if scene_id is None else JobRow.scene_id == scene_id
        )
        stmt = (
            select(JobRow)
            .where(
                JobRow.episode_id == _as_uuid(episode_id),
                JobRow.type == type.value,
                scene_filter,
                JobRow.status.not_in([s.value for s in JOB_TERMINAL_STATUSES]),
            )
            .order_by(JobRow.created_at, JobRow.id)
        )
        row = (await self._session.scalars(stmt)).first()
        return _to_job(row) if row else None

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

    async def mark_skipped(self, job_id: uuid.UUID | str) -> Job:
        """実処理をせずに既存Artifactを返した（docs/domain/job.md の `skipped`）。

        **再開が効いた証拠**であり、これが記録されないなら冪等性が壊れている。
        `attempts` は加算しない ── 課金を伴う呼び出しは起きていないため。
        """
        row = await self._apply(job_id, JobEvent.SKIPPED)
        row.failure_class = None
        row.error_summary = None
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
        input_hash: str | None = None,
        size_bytes: int | None = None,
        produced_by_job_id: uuid.UUID | str | None = None,
        scene_id: str | None = None,
    ) -> ArtifactMetadata:
        """同じ内容の再記録は既存行を返す（INV-17）。UNIQUE制約と同じ鍵で引く。

        ``scene_id``（ADR-0018）を渡すと、同一性・世代・現行の判定はすべて
        ``(episode_id, artifact_type, scene_id)`` の中で閉じる。省略時は Episode 単位の行
        （``scene_id IS NULL``）だけを対象にし、Phase 3 までと同じ挙動になる。

        新しい内容は**新しい世代**として記録する（ADR-0012）:
        ``version`` を同 ``(episode_id, artifact_type)`` の最大+1で採番し、
        現行行（``superseded_at IS NULL``）を降ろしてから新行を現行にする。
        「現行は常に1本」は partial unique index ``uq_artifact_metadata_current``
        が DB 側でも保証する。

        ``input_hash`` を省略した場合は ``sha256`` を流用する。
        これは Phase 1 の dummy 生成器のような**決定論的な**呼び出し元のための既定で、
        同じ入力→同じ内容→同じ sha256 が成り立つ限り input_hash と等価に働く。
        非決定的な生成器（Codex 等）は必ず ``domain/script/identity.py`` の
        ``script_input_hash`` を明示的に渡すこと。
        """
        episode_uuid = _as_uuid(episode_id)
        stmt = select(ArtifactMetadataRow).where(
            ArtifactMetadataRow.episode_id == episode_uuid,
            ArtifactMetadataRow.artifact_type == artifact_type.value,
            ArtifactMetadataRow.sha256 == sha256,
            _artifact_scene_filter(scene_id),
        )
        existing = (await self._session.scalars(stmt)).first()
        now = _now()
        if existing is not None:
            if existing.superseded_at is None:
                if input_hash is not None and existing.input_hash != input_hash:
                    # 同じ内容が別の入力から得られた（例: 生成器の仕様だけ変わった）。
                    # 次回の skip 判定が最新の入力で当たるよう、入力の記録を更新する。
                    existing.input_hash = input_hash
                    await self._session.flush()
                return _to_artifact(existing)
            # A→B→A: 同じ内容が過去世代に居る。降ろされた行を「現行」として返すと
            # find_current_by_type と食い違うので、現行を降ろして過去行を復帰させる。
            await self._supersede_current(episode_uuid, artifact_type, now, scene_id)
            existing.superseded_at = None
            if input_hash is not None:
                existing.input_hash = input_hash
            await self._session.flush()
            return _to_artifact(existing)

        max_version = await self._session.scalar(
            select(func.max(ArtifactMetadataRow.version)).where(
                ArtifactMetadataRow.episode_id == episode_uuid,
                ArtifactMetadataRow.artifact_type == artifact_type.value,
                _artifact_scene_filter(scene_id),
            )
        )
        await self._supersede_current(episode_uuid, artifact_type, now, scene_id)

        row = ArtifactMetadataRow(
            id=uuid.uuid4(),
            episode_id=episode_uuid,
            artifact_type=artifact_type.value,
            schema_version=schema_version,
            bucket=bucket,
            object_key=object_key,
            sha256=sha256,
            input_hash=input_hash if input_hash is not None else sha256,
            version=(max_version or 0) + 1,
            superseded_at=None,
            size_bytes=size_bytes,
            scene_id=scene_id,
            produced_by_job_id=_as_uuid(produced_by_job_id) if produced_by_job_id else None,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_artifact(row)

    async def _supersede_current(
        self,
        episode_uuid: uuid.UUID,
        artifact_type: ArtifactType,
        now: datetime,
        scene_id: str | None = None,
    ) -> None:
        """現行世代を降ろす唯一の場所。partial unique index より先に flush する。

        同じ scene キーの行だけを降ろす（別シーンの現行を巻き込まない / ADR-0018）。
        """
        current_stmt = select(ArtifactMetadataRow).where(
            ArtifactMetadataRow.episode_id == episode_uuid,
            ArtifactMetadataRow.artifact_type == artifact_type.value,
            _artifact_scene_filter(scene_id),
            ArtifactMetadataRow.superseded_at.is_(None),
        )
        for current in (await self._session.scalars(current_stmt)).all():
            current.superseded_at = now
        await self._session.flush()

    async def find_current_by_type(
        self,
        episode_id: uuid.UUID | str,
        artifact_type: ArtifactType,
        scene_id: str | None = None,
    ) -> ArtifactMetadata | None:
        """``(episode_id, artifact_type, scene_id)`` の現行世代を引く（ADR-0015 / ADR-0018）。"""
        stmt = select(ArtifactMetadataRow).where(
            ArtifactMetadataRow.episode_id == _as_uuid(episode_id),
            ArtifactMetadataRow.artifact_type == artifact_type.value,
            _artifact_scene_filter(scene_id),
            ArtifactMetadataRow.superseded_at.is_(None),
        )
        row = (await self._session.scalars(stmt)).first()
        return _to_artifact(row) if row else None

    async def find_current(
        self,
        episode_id: uuid.UUID | str,
        artifact_type: ArtifactType,
        input_hash: str,
        scene_id: str | None = None,
    ) -> ArtifactMetadata | None:
        """現行世代かつ同じ入力から作られた成果物を引く（ADR-0012 の skip 判定）。

        これが非 None なら有料呼び出しをせずに既存を返す（INV-17）。
        """
        stmt = select(ArtifactMetadataRow).where(
            ArtifactMetadataRow.episode_id == _as_uuid(episode_id),
            ArtifactMetadataRow.artifact_type == artifact_type.value,
            _artifact_scene_filter(scene_id),
            ArtifactMetadataRow.input_hash == input_hash,
            ArtifactMetadataRow.superseded_at.is_(None),
        )
        row = (await self._session.scalars(stmt)).first()
        return _to_artifact(row) if row else None

    async def list_current_by_type(
        self, episode_id: uuid.UUID | str, artifact_type: ArtifactType
    ) -> list[ArtifactMetadata]:
        """全 scene キーの現行世代（マニフェストの組み立てに使う / ADR-0018）。scene_id 順。"""
        stmt = (
            select(ArtifactMetadataRow)
            .where(
                ArtifactMetadataRow.episode_id == _as_uuid(episode_id),
                ArtifactMetadataRow.artifact_type == artifact_type.value,
                ArtifactMetadataRow.superseded_at.is_(None),
            )
            .order_by(ArtifactMetadataRow.scene_id, ArtifactMetadataRow.id)
        )
        return [_to_artifact(row) for row in (await self._session.scalars(stmt)).all()]

    async def get(self, artifact_id: uuid.UUID | str) -> ArtifactMetadata | None:
        row = await self._session.get(ArtifactMetadataRow, _as_uuid(artifact_id))
        return _to_artifact(row) if row else None

    async def list_for_episode(self, episode_id: uuid.UUID | str) -> list[ArtifactMetadata]:
        stmt = (
            select(ArtifactMetadataRow)
            .where(ArtifactMetadataRow.episode_id == _as_uuid(episode_id))
            .order_by(ArtifactMetadataRow.created_at, ArtifactMetadataRow.id)
        )
        return [_to_artifact(row) for row in (await self._session.scalars(stmt)).all()]


@dataclass(frozen=True, slots=True)
class ProviderReservation:
    """予約台帳の1行（ADR-0013）。"""

    id: str
    episode_id: str
    job_id: str | None
    provider: ProviderCall
    idempotency_key: str
    input_hash: str
    round: int
    status: ReservationStatus
    raw_output_key: str | None
    outcome_artifact_id: str | None
    failure_class: FailureClass | None
    error_summary: str | None
    reserved_at: datetime
    dispatched_at: datetime | None
    reconciled_at: datetime | None
    reconciled_by: str | None
    scene_id: str | None = None
    provider_job_ref: str | None = None
    estimated_cost_usd: Decimal | None = None
    #: 外部呼び出しの結果参照（YouTube video id、ADR-0020）
    provider_result_ref: str | None = None


def _to_reservation(row: ProviderReservationRow) -> ProviderReservation:
    return ProviderReservation(
        id=str(row.id),
        episode_id=str(row.episode_id),
        job_id=str(row.job_id) if row.job_id else None,
        provider=ProviderCall(row.provider),
        idempotency_key=row.idempotency_key,
        input_hash=row.input_hash,
        round=row.round,
        status=ReservationStatus(row.status),
        raw_output_key=row.raw_output_key,
        outcome_artifact_id=str(row.outcome_artifact_id) if row.outcome_artifact_id else None,
        failure_class=FailureClass(row.failure_class) if row.failure_class else None,
        error_summary=row.error_summary,
        reserved_at=row.reserved_at,
        dispatched_at=row.dispatched_at,
        reconciled_at=row.reconciled_at,
        reconciled_by=row.reconciled_by,
        scene_id=row.scene_id,
        provider_job_ref=row.provider_job_ref,
        estimated_cost_usd=row.estimated_cost_usd,
        provider_result_ref=row.provider_result_ref,
    )


class ProviderReservationRepository:
    """予約台帳（ADR-0013）。commit は呼び出し側の責務。

    書き込み順序（この順序が INV-15 の実体）:
    ``reserve`` → commit → ``mark_dispatched`` → commit → 外部呼び出し →
    ``mark_spent`` → commit → パース/検証 → ``attach_artifact``。

    非同期ジョブ型（ADR-0017）は submit と結果取得の間に
    ``record_provider_job_ref`` → commit が入る。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, reservation_id: uuid.UUID | str) -> ProviderReservationRow:
        row = await self._session.get(ProviderReservationRow, _as_uuid(reservation_id))
        if row is None:
            raise InvalidTransitionError(f"reservation not found: {reservation_id}")
        return row

    async def find_by_key(self, idempotency_key: str) -> ProviderReservation | None:
        """再開判定はここだけで行う（ADR-0013 の再開分岐表）。"""
        stmt = select(ProviderReservationRow).where(
            ProviderReservationRow.idempotency_key == idempotency_key
        )
        row = (await self._session.scalars(stmt)).first()
        return _to_reservation(row) if row else None

    async def find_latest_for_input(
        self,
        episode_id: uuid.UUID | str,
        provider: ProviderCall,
        scene_id: str | None,
        input_hash: str,
    ) -> ProviderReservation | None:
        """同じ入力（Episode + provider + scene + input_hash）で最も大きいラウンドの予約。

        台帳のラウンドは workflow の run ごとの試行番号ではなく**ここから導く**（ADR-0017 §3）。
        同じラウンドの並行 INSERT は ``idempotency_key`` の一意制約が止める。
        """
        scene_filter = (
            ProviderReservationRow.scene_id.is_(None)
            if scene_id is None
            else ProviderReservationRow.scene_id == scene_id
        )
        stmt = (
            select(ProviderReservationRow)
            .where(
                ProviderReservationRow.episode_id == _as_uuid(episode_id),
                ProviderReservationRow.provider == provider.value,
                scene_filter,
                ProviderReservationRow.input_hash == input_hash,
            )
            .order_by(
                ProviderReservationRow.round.desc(),
                ProviderReservationRow.reserved_at.desc(),
                ProviderReservationRow.id,
            )
            .limit(1)
        )
        row = (await self._session.scalars(stmt)).first()
        return _to_reservation(row) if row else None

    async def list_for_episode_provider(
        self, episode_id: uuid.UUID | str, provider: ProviderCall
    ) -> list[ProviderReservation]:
        """Episode × provider の全予約（ラウンド順）。Episode 単位の二重投稿検査（ADR-0020）。"""
        stmt = (
            select(ProviderReservationRow)
            .where(
                ProviderReservationRow.episode_id == _as_uuid(episode_id),
                ProviderReservationRow.provider == provider.value,
            )
            .order_by(ProviderReservationRow.reserved_at, ProviderReservationRow.round)
        )
        return [_to_reservation(r) for r in (await self._session.scalars(stmt)).all()]

    async def find_unreconciled(
        self,
        episode_id: uuid.UUID | str,
        provider: ProviderCall,
        scene_id: str | None = None,
    ) -> list[ProviderReservation]:
        """evidence（生出力）の無い ``reserved`` を探す。

        新しいラウンドを開始する前に必ず呼ぶ。1件でも残っていれば
        ``UnreconciledReservationError`` を投げて Episode を blocked にする。
        これがラウンドを跨いだ二重呼び出しの最後の砦である。

        ``scene_id``（ADR-0018）で範囲を閉じる。並行するシーンが互いを止めない。
        省略時は Episode 単位の予約（``scene_id IS NULL``）だけを見る。
        provider job 参照を持つ ``reserved`` も含む（進行中の課金ジョブなので新ラウンドを止める）。
        """
        scene_filter = (
            ProviderReservationRow.scene_id.is_(None)
            if scene_id is None
            else ProviderReservationRow.scene_id == scene_id
        )
        stmt = (
            select(ProviderReservationRow)
            .where(
                ProviderReservationRow.episode_id == _as_uuid(episode_id),
                ProviderReservationRow.provider == provider.value,
                scene_filter,
                ProviderReservationRow.status == ReservationStatus.RESERVED.value,
                ProviderReservationRow.raw_output_key.is_(None),
            )
            .order_by(ProviderReservationRow.reserved_at, ProviderReservationRow.id)
        )
        return [_to_reservation(row) for row in (await self._session.scalars(stmt)).all()]

    async def reserve(
        self,
        *,
        episode_id: uuid.UUID | str,
        provider: ProviderCall,
        idempotency_key: str,
        input_hash: str,
        round: int,
        job_id: uuid.UUID | str | None = None,
        scene_id: str | None = None,
        estimated_cost_usd: Decimal | None = None,
    ) -> ProviderReservation:
        """予約を INSERT する。呼び出し側がこの直後に commit すること。"""
        row = ProviderReservationRow(
            scene_id=scene_id,
            estimated_cost_usd=estimated_cost_usd,
            id=uuid.uuid4(),
            episode_id=_as_uuid(episode_id),
            job_id=_as_uuid(job_id) if job_id else None,
            provider=provider.value,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            round=round,
            status=ReservationStatus.RESERVED.value,
            reserved_at=_now(),
        )
        self._session.add(row)
        await self._session.flush()
        return _to_reservation(row)

    async def mark_dispatched(self, reservation_id: uuid.UUID | str) -> ProviderReservation:
        """subprocess を起動する**直前**に呼び、commit してから起動する。

        状態は ``reserved`` のまま。``dispatched_at`` が「呼んだ可能性」の境界であり、
        NULL なら起動前 crash（呼んでいない証拠）と判定できる。

        検査と書き込みを1文の条件付き UPDATE にする（``reserved`` かつ未 dispatch の行だけ）。
        並行する2つの submit が同じ予約を読んでも、dispatch できるのは片方だけ。
        更新0行は「既に呼んだかもしれない行」なので ``UnreconciledReservationError``（人手照合）。
        """
        table = ProviderReservationRow
        await self._row(reservation_id)  # 行が無ければ InvalidTransitionError
        result = await self._session.execute(
            update(table)
            .where(
                table.id == _as_uuid(reservation_id),
                table.status == ReservationStatus.RESERVED.value,
                table.dispatched_at.is_(None),
            )
            .values(dispatched_at=_now())
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise UnreconciledReservationError(
                f"reservation {reservation_id} is already dispatched or closed; "
                "refusing to dispatch it again"
            )
        fresh = await self._session.get(table, _as_uuid(reservation_id), populate_existing=True)
        if fresh is None:
            raise InvalidTransitionError(f"reservation not found: {reservation_id}")
        return _to_reservation(fresh)

    async def record_provider_job_ref(
        self, reservation_id: uuid.UUID | str, provider_job_ref: str
    ) -> ProviderReservation:
        """submit が返した provider job 参照を**1度だけ**書く（ADR-0017）。commit は直後に。

        - 同じ参照の再記録は no-op（Activity 再実行）
        - 異なる参照で上書きしようとしたら ``InvalidTransitionError``（二重 submit の兆候）
        - ``reserved`` かつ ``dispatched_at`` ありの行にだけ新規に書ける
        """
        if not provider_job_ref:
            raise ValueError("provider_job_ref must be non-empty")
        row = await self._row(reservation_id)
        if row.provider_job_ref == provider_job_ref:
            return _to_reservation(row)
        # 検査と書き込みを1文の条件付き UPDATE にする。読み取り後に別セッションが
        # 参照を書いても、古い読み取りからは上書きできない（write-once を DB で守る）。
        table = ProviderReservationRow
        result = await self._session.execute(
            update(table)
            .where(
                table.id == _as_uuid(reservation_id),
                table.status == ReservationStatus.RESERVED.value,
                table.dispatched_at.is_not(None),
                (table.provider_job_ref.is_(None)) | (table.provider_job_ref == provider_job_ref),
            )
            .values(provider_job_ref=provider_job_ref)
            .execution_options(synchronize_session=False)
        )
        fresh = await self._session.get(table, row.id, populate_existing=True)
        if fresh is None:
            raise InvalidTransitionError(f"reservation not found: {reservation_id}")
        if getattr(result, "rowcount", 0) == 1:
            return _to_reservation(fresh)
        if fresh.provider_job_ref == provider_job_ref:
            return _to_reservation(fresh)
        if fresh.provider_job_ref is not None:
            raise InvalidTransitionError(
                f"reservation {reservation_id} already has a different provider job ref"
            )
        if ReservationStatus(fresh.status) is not ReservationStatus.RESERVED:
            raise InvalidTransitionError(
                f"reservation {reservation_id} is {fresh.status}; job ref needs reserved"
            )
        raise InvalidTransitionError(
            f"reservation {reservation_id} is not dispatched; job ref needs dispatched_at"
        )

    # ------------------------------------------------------------ resumable upload（ADR-0020）
    #
    # upload は「session 開始 → session を保存 → dispatched → bytes」の順で、ADR-0017 の
    # submit（dispatched → submit → 参照）と順序が逆になる。session の作成は動画を作らないので、
    # 保存前の crash は無害（孤児 session は bytes を受け取らない）。
    #
    # 不変条件（すべて1文の条件付き UPDATE で DB が守る。行ロックを長時間持たない）:
    # - ``dispatched_at`` が入った後、``provider_job_ref``（session）は二度と変わらない
    # - bytes を送ってよいのは、``dispatched_at`` を**その session に対して**立てた後だけ
    # → bytes を受け取る session は予約あたり高々1つ → 動画は高々1本（INV-14）

    async def record_upload_session(
        self, reservation_id: uuid.UUID | str, session_ref: str, *, replaces: str | None = None
    ) -> bool:
        """未 dispatch の予約に session を書く。``replaces`` を渡すと、その session からの差し替え。

        書けたら True。別の試行が先に書いた・dispatch 済み・閉じた予約なら False（読み直すこと）。
        """
        if not session_ref:
            raise ValueError("session_ref must be non-empty")
        table = ProviderReservationRow
        ref_filter = (
            table.provider_job_ref.is_(None)
            if replaces is None
            else table.provider_job_ref == replaces
        )
        result = await self._session.execute(
            update(table)
            .where(
                table.id == _as_uuid(reservation_id),
                table.status == ReservationStatus.RESERVED.value,
                table.dispatched_at.is_(None),
                ref_filter,
            )
            .values(provider_job_ref=session_ref)
            .execution_options(synchronize_session=False)
        )
        return getattr(result, "rowcount", 0) == 1

    async def mark_upload_dispatched(
        self, reservation_id: uuid.UUID | str, session_ref: str
    ) -> tuple[bool, bool]:
        """``session_ref`` へ最初の bytes を送る直前に呼び、commit してから送る。

        戻り値 ``(ok, changed)``。``ok``: 予約の session が ``session_ref`` で dispatch 済み
        （送ってよい）。``changed``: この呼び出しが ``dispatched_at`` を立てた。
        session が差し替わっていた・閉じた予約なら ``ok`` は False（送ってはならない）。
        """
        table = ProviderReservationRow
        changed = await self._session.execute(
            update(table)
            .where(
                table.id == _as_uuid(reservation_id),
                table.status == ReservationStatus.RESERVED.value,
                table.provider_job_ref == session_ref,
                table.dispatched_at.is_(None),
            )
            .values(dispatched_at=_now())
            .execution_options(synchronize_session=False)
        )
        fresh = await self._session.get(table, _as_uuid(reservation_id), populate_existing=True)
        ok = (
            fresh is not None
            and ReservationStatus(fresh.status) is ReservationStatus.RESERVED
            and fresh.provider_job_ref == session_ref
            and fresh.dispatched_at is not None
        )
        # (送ってよいか, この呼び出しが dispatched_at を立てたか)。立てた呼び出しだけが
        # offset 0 から送ってよい。他は status query で受理位置を確かめる
        return ok, ok and getattr(changed, "rowcount", 0) == 1

    async def record_upload_result(
        self, reservation_id: uuid.UUID | str, result_ref: str, *, reconciled_by: str
    ) -> ProviderReservation:
        """結果（video id）を書き、同時に ``spent`` にする。受領 Artifact より先に commit。

        同じ結果での再記録は no-op。異なる結果・abandoned への記録は ``InvalidTransitionError``。
        """
        if not result_ref:
            raise ValueError("result_ref must be non-empty")
        target = transition_reservation(
            ReservationStatus.RESERVED, ReservationEvent.EVIDENCE_RECONCILED
        )
        if isinstance(target, Rejected):  # pragma: no cover - 表の定義で起きない
            raise InvalidTransitionError(target.reason)
        table = ProviderReservationRow
        await self._session.execute(
            update(table)
            .where(
                table.id == _as_uuid(reservation_id),
                table.status == ReservationStatus.RESERVED.value,
                table.provider_result_ref.is_(None),
            )
            .values(
                status=target.value,
                provider_result_ref=result_ref,
                reconciled_by=reconciled_by,
                reconciled_at=_now(),
            )
            .execution_options(synchronize_session=False)
        )
        fresh = await self._session.get(table, _as_uuid(reservation_id), populate_existing=True)
        if fresh is None:
            raise InvalidTransitionError(f"reservation not found: {reservation_id}")
        if (
            ReservationStatus(fresh.status) is ReservationStatus.SPENT
            and fresh.provider_result_ref == result_ref
        ):
            return _to_reservation(fresh)
        raise InvalidTransitionError(
            f"reservation {reservation_id} is {fresh.status} with a different or missing "
            "result ref; refusing to record another result"
        )

    async def get(self, reservation_id: uuid.UUID | str) -> ProviderReservation | None:
        row = await self._session.get(ProviderReservationRow, _as_uuid(reservation_id))
        return _to_reservation(row) if row else None

    async def _apply(
        self, reservation_id: uuid.UUID | str, event: ReservationEvent
    ) -> ProviderReservationRow:
        row = await self._row(reservation_id)
        result = transition_reservation(ReservationStatus(row.status), event)
        if isinstance(result, Rejected):
            raise InvalidTransitionError(result.reason)
        row.status = result.value
        await self._session.flush()
        return row

    async def mark_spent(
        self,
        reservation_id: uuid.UUID | str,
        *,
        raw_output_key: str | None,
        reconciled_by: str = "evidence",
        failure_class: FailureClass | None = None,
        error_summary: str | None = None,
    ) -> ProviderReservation:
        """「呼んだ」事実を確定する。**パース・検証より前**に commit すること。

        検証で落ちても課金の事実は残る。``reconciled_by`` の意味:

        - ``evidence``: 生出力（``raw_output_key``）が残っている。最も強い証拠
        - ``conservative``: 呼び出しが**戻ってきた上で**失敗した。課金されたかは
          不明だが、課金された前提で確定する（安全側）。未照合のまま残すと
          次ラウンドが永久にブロックされ、retryable な失敗を retry できない
        - ``operator:<id>``: 人手照合

        Worker が落ちて**何も記録できなかった**場合はここへ到達しないので、
        予約は ``reserved`` のまま残り、次ラウンドは正しくブロックされる。
        """
        event = (
            ReservationEvent.EVIDENCE_RECONCILED
            if reconciled_by in {"evidence", "conservative"}
            else ReservationEvent.OPERATOR_CONFIRMED_SPENT
        )
        row = await self._apply(reservation_id, event)
        row.raw_output_key = raw_output_key
        row.reconciled_by = reconciled_by
        row.reconciled_at = _now()
        if failure_class is not None:
            row.failure_class = failure_class.value
        if error_summary is not None:
            row.error_summary = error_summary[:2000]
        await self._session.flush()
        return _to_reservation(row)

    async def abandon(
        self, reservation_id: uuid.UUID | str, *, reconciled_by: str
    ) -> ProviderReservation:
        """人手照合の結果「呼んでいなかった」と確定した場合のみ。自動で呼ばない。"""
        row = await self._apply(reservation_id, ReservationEvent.OPERATOR_ABANDONED)
        row.reconciled_at = _now()
        row.reconciled_by = reconciled_by
        await self._session.flush()
        return _to_reservation(row)

    async def attach_artifact(
        self, reservation_id: uuid.UUID | str, artifact_id: uuid.UUID | str
    ) -> ProviderReservation:
        """検証を通った成果物を予約に紐づける。状態は変えない。"""
        row = await self._row(reservation_id)
        row.outcome_artifact_id = _as_uuid(artifact_id)
        await self._session.flush()
        return _to_reservation(row)

    async def record_failure(
        self,
        reservation_id: uuid.UUID | str,
        *,
        failure_class: FailureClass,
        error_summary: str | None = None,
    ) -> ProviderReservation:
        """失敗を記録する。**状態は進めない**（reserved のまま）。

        失敗したからといって予約を自動で解放しないのが INV-15 である。
        ``error_summary`` は人間向けの説明で、機械判定に使わない。
        """
        row = await self._row(reservation_id)
        row.failure_class = failure_class.value
        row.error_summary = (error_summary or "")[:2000] or None
        await self._session.flush()
        return _to_reservation(row)


class OperationalSwitchRepository:
    """DB の停止スイッチ（ADR-0021）。行が無ければ off。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_on(self, switch: OperationalSwitch) -> bool:
        row = await self._session.get(OperationalSwitchRow, switch.value, populate_existing=True)
        return bool(row is not None and row.is_on)

    async def set(self, switch: OperationalSwitch, on: bool, *, reason: str | None = None) -> None:
        row = await self._session.get(OperationalSwitchRow, switch.value)
        if row is None:
            self._session.add(
                OperationalSwitchRow(name=switch.value, is_on=on, reason=reason, updated_at=_now())
            )
        else:
            row.is_on = on
            row.reason = reason
            row.updated_at = _now()
        await self._session.flush()


class DailyEpisodeSlotRepository:
    """日次 Episode 枠（ADR-0021）。上限は DB の主キーで守り、アプリの読みに頼らない。"""

    #: 並行 claim の衝突で読み直す回数の上限（衝突ごとに枠が1つ埋まるので有限で足りる）
    MAX_ATTEMPTS = 16

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _by_trigger(self, trigger_id: str) -> DailyEpisodeSlotRow | None:
        result = await self._session.execute(
            select(DailyEpisodeSlotRow).where(DailyEpisodeSlotRow.trigger_id == trigger_id)
        )
        return result.scalar_one_or_none()

    async def _rows_for_date(self, slot_date: date) -> list[DailyEpisodeSlotRow]:
        result = await self._session.execute(
            select(DailyEpisodeSlotRow)
            .where(DailyEpisodeSlotRow.slot_date == slot_date)
            .order_by(DailyEpisodeSlotRow.slot_index)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars())

    async def claim(
        self,
        *,
        slot_date: date,
        trigger_id: str,
        daily_limit: int,
        topic: str | None,
        topic_plan_id: uuid.UUID | str | None = None,
    ) -> DailySlotClaim:
        """1回の claim。commit は呼び出し側。衝突は savepoint を戻して読み直す。

        ``topic_plan_id``（ADR-0025）:

        - CREATED: 新しい Episode に結び付け、plan を ``assigned`` にする
        - EXISTING: 何も書かない（Episode に結び付いている plan は ``Episode.topic_plan_id``）
        - RESUME: plan の無い planned Episode なら、この plan を結び付けてから返す
        - plan が既に別の Episode に結び付いている（``uq_episodes_topic_plan_id``）なら、
          その Episode が同じ日の planned 枠なら RESUME、そうでなければ LIMIT_REACHED。
          plan は「1日・1 profile の組に1つ」なので、同じ plan で2本目の Episode を作ることは
          その日の同じ題材を2度作ることであり、枠が残っていても作らない
        """
        plan_id = _as_uuid(topic_plan_id) if topic_plan_id is not None else None
        for _ in range(self.MAX_ATTEMPTS):
            existing = await self._by_trigger(trigger_id)
            if existing is not None:
                return DailySlotClaim(
                    outcome=ClaimOutcome.EXISTING,
                    slot_date=existing.slot_date,
                    episode_id=str(existing.episode_id),
                    slot_index=existing.slot_index,
                )
            rows = await self._rows_for_date(slot_date)
            if plan_id is not None:
                holder = await self._episode_holding_plan(plan_id)
                if holder is not None:
                    return self._resume_holder(slot_date, rows, holder)
            if len(rows) >= daily_limit:
                resumed = await self._resume_or_limit(slot_date, rows, plan_id, topic)
                if resumed is None:
                    continue  # 結び付けの競合。読み直す
                return resumed
            index = max((r.slot_index for r in rows), default=-1) + 1
            try:
                async with self._session.begin_nested():
                    episode = await EpisodeRepository(self._session).create(
                        topic=topic, topic_plan_id=plan_id
                    )
                    self._session.add(
                        DailyEpisodeSlotRow(
                            slot_date=slot_date,
                            slot_index=index,
                            trigger_id=trigger_id,
                            episode_id=_as_uuid(episode.id),
                            created_at=_now(),
                        )
                    )
                    await self._session.flush()
                    if plan_id is not None:
                        await TopicPlanRepository(self._session).mark_assigned(plan_id)
            except IntegrityError:
                continue  # 別の claim が同じ番号・同じ trigger・同じ plan を先に取った。読み直す
            return DailySlotClaim(
                outcome=ClaimOutcome.CREATED,
                slot_date=slot_date,
                episode_id=episode.id,
                slot_index=index,
            )
        raise RuntimeError(f"daily slot claim did not converge: {slot_date} {trigger_id}")

    async def _episode_holding_plan(self, plan_id: uuid.UUID) -> EpisodeRow | None:
        result = await self._session.execute(
            select(EpisodeRow)
            .where(EpisodeRow.topic_plan_id == plan_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _resume_holder(
        slot_date: date, rows: list[DailyEpisodeSlotRow], holder: EpisodeRow
    ) -> DailySlotClaim:
        if holder.status == EpisodeStatus.PLANNED.value:
            for row in rows:
                if row.episode_id == holder.id:
                    return DailySlotClaim(
                        outcome=ClaimOutcome.RESUME,
                        slot_date=slot_date,
                        episode_id=str(row.episode_id),
                        slot_index=row.slot_index,
                    )
        return DailySlotClaim(outcome=ClaimOutcome.LIMIT_REACHED, slot_date=slot_date)

    async def _resume_or_limit(
        self,
        slot_date: date,
        rows: list[DailyEpisodeSlotRow],
        plan_id: uuid.UUID | None,
        topic: str | None,
    ) -> DailySlotClaim | None:
        """上限到達時。planned の Episode を再開する。結び付けが競合に負けたら None。"""
        for row in rows:
            episode = await self._session.get(EpisodeRow, row.episode_id, populate_existing=True)
            if episode is None or episode.status != EpisodeStatus.PLANNED.value:
                continue
            if (
                plan_id is not None
                and episode.topic_plan_id is None
                and not await self._attach_plan(episode.id, plan_id, topic)
            ):
                return None
            return DailySlotClaim(
                outcome=ClaimOutcome.RESUME,
                slot_date=slot_date,
                episode_id=str(row.episode_id),
                slot_index=row.slot_index,
            )
        return DailySlotClaim(outcome=ClaimOutcome.LIMIT_REACHED, slot_date=slot_date)

    async def _attach_plan(
        self, episode_id: uuid.UUID, plan_id: uuid.UUID, topic: str | None
    ) -> bool:
        """plan の無い planned Episode へ plan を結び付ける（compare-and-set）。"""
        try:
            async with self._session.begin_nested():
                values: dict[str, object] = {"topic_plan_id": plan_id, "updated_at": _now()}
                if topic is not None:
                    values["topic"] = topic
                changed = await self._session.execute(
                    update(EpisodeRow)
                    .where(
                        EpisodeRow.id == episode_id,
                        EpisodeRow.topic_plan_id.is_(None),
                        EpisodeRow.status == EpisodeStatus.PLANNED.value,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                if getattr(changed, "rowcount", 0) != 1:
                    return False
                await TopicPlanRepository(self._session).mark_assigned(plan_id)
        except IntegrityError:
            return False  # 同じ plan を別の Episode が先に取った。読み直すと holder が見える
        return True

    async def list_for_date(self, slot_date: date) -> list[DailyEpisodeSlot]:
        return [
            DailyEpisodeSlot(
                slot_date=r.slot_date,
                slot_index=r.slot_index,
                trigger_id=r.trigger_id,
                episode_id=str(r.episode_id),
            )
            for r in await self._rows_for_date(slot_date)
        ]


# ------------------------------------------------------------------ Topic Planner（ADR-0025）


@dataclass(frozen=True, slots=True)
class NewTopicCandidate:
    """保存する候補1件。``payload`` は validation 済み ``TopicCandidate`` の JSON（INV-23）。"""

    ordinal: int
    round: int
    payload: dict[str, Any]
    subject: str
    angle: str
    duplicate_level: DuplicateLevel
    duplicate_score: float
    rejected: bool
    duplicate_of: str | None = None
    score: float | None = None
    score_breakdown: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class NewTopicPlan:
    """確定する plan。題材の列は採用候補（``selected_ordinal``）の値。"""

    plan_date: date
    strategy_profile_id: str
    strategy_version: str
    content_profile_id: str
    content_profile_version: str
    topic: str
    subject: str
    angle: str
    era: str
    theme: str
    hook: str
    entities: list[str]
    score: float
    score_breakdown: dict[str, float]
    duplicate_score: float
    duplicate_level: DuplicateLevel
    analytics_mode: AnalyticsMode
    analytics_confidence: float
    planner_version: str
    prompt_version: str
    analytics_snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class TopicPlan:
    id: str
    plan_date: date
    strategy_profile_id: str
    strategy_version: str
    content_profile_id: str
    content_profile_version: str
    selected_candidate_id: str | None
    topic: str
    subject: str
    angle: str
    era: str
    theme: str
    hook: str
    entities: list[str]
    score: float
    score_breakdown: dict[str, float]
    duplicate_score: float
    duplicate_level: DuplicateLevel
    analytics_mode: AnalyticsMode
    analytics_snapshot_id: str | None
    analytics_confidence: float
    planner_version: str
    prompt_version: str
    status: TopicPlanStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TopicCandidateRecord:
    id: str
    topic_plan_id: str
    ordinal: int
    round: int
    payload: dict[str, Any]
    subject: str
    angle: str
    duplicate_level: DuplicateLevel
    duplicate_score: float
    duplicate_of: str | None
    score: float | None
    score_breakdown: dict[str, float] | None
    rejected: bool


@dataclass(frozen=True, slots=True)
class SavedTopicPlan:
    plan: TopicPlan
    #: True なら同じ (日, strategy, content) の plan が既にあり、それを返した（上書きしていない）
    reused: bool


def _to_topic_plan(row: TopicPlanRow) -> TopicPlan:
    return TopicPlan(
        id=str(row.id),
        plan_date=row.plan_date,
        strategy_profile_id=row.strategy_profile_id,
        strategy_version=row.strategy_version,
        content_profile_id=row.content_profile_id,
        content_profile_version=row.content_profile_version,
        selected_candidate_id=str(row.selected_candidate_id) if row.selected_candidate_id else None,
        topic=row.topic,
        subject=row.subject,
        angle=row.angle,
        era=row.era,
        theme=row.theme,
        hook=row.hook,
        entities=list(row.entities),
        score=row.score,
        score_breakdown=dict(row.score_breakdown),
        duplicate_score=row.duplicate_score,
        duplicate_level=DuplicateLevel(row.duplicate_level),
        analytics_mode=AnalyticsMode(row.analytics_mode),
        analytics_snapshot_id=str(row.analytics_snapshot_id) if row.analytics_snapshot_id else None,
        analytics_confidence=row.analytics_confidence,
        planner_version=row.planner_version,
        prompt_version=row.prompt_version,
        status=TopicPlanStatus(row.status),
        created_at=row.created_at,
    )


def _to_candidate(row: TopicCandidateRow) -> TopicCandidateRecord:
    return TopicCandidateRecord(
        id=str(row.id),
        topic_plan_id=str(row.topic_plan_id),
        ordinal=row.ordinal,
        round=row.round,
        payload=dict(row.payload),
        subject=row.subject,
        angle=row.angle,
        duplicate_level=DuplicateLevel(row.duplicate_level),
        duplicate_score=row.duplicate_score,
        duplicate_of=row.duplicate_of,
        score=row.score,
        score_breakdown=dict(row.score_breakdown) if row.score_breakdown is not None else None,
        rejected=row.rejected,
    )


class TopicPlanRepository:
    """TopicPlan と候補、Content Memory（ADR-0025）。flush まで。commit は呼び出し側。

    Content Memory は表を持たない。``topic_plans`` と ``episodes`` から導出する
    （PostgreSQL が唯一の source of truth。2つ目の写しを作らない）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(
        self, plan_date: date, strategy_profile_id: str, content_profile_id: str
    ) -> TopicPlan | None:
        result = await self._session.execute(
            select(TopicPlanRow)
            .where(
                TopicPlanRow.plan_date == plan_date,
                TopicPlanRow.strategy_profile_id == strategy_profile_id,
                TopicPlanRow.content_profile_id == content_profile_id,
            )
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        return _to_topic_plan(row) if row else None

    async def get(self, plan_id: uuid.UUID | str) -> TopicPlan | None:
        row = await self._session.get(TopicPlanRow, _as_uuid(plan_id), populate_existing=True)
        return _to_topic_plan(row) if row else None

    async def save_plan(
        self,
        plan: NewTopicPlan,
        candidates: Sequence[NewTopicCandidate],
        *,
        selected_ordinal: int,
    ) -> SavedTopicPlan:
        """plan と全候補を1つの savepoint で入れる。

        同じ (日, strategy, content) の plan が既にあれば（先に見つかっても、一意制約の衝突で
        分かっても）既存を ``reused=True`` で返す。既存の plan は決して上書きしない。
        """
        existing = await self.find(
            plan.plan_date, plan.strategy_profile_id, plan.content_profile_id
        )
        if existing is not None:
            return SavedTopicPlan(plan=existing, reused=True)
        ordinals = [c.ordinal for c in candidates]
        if selected_ordinal not in ordinals:
            raise ValueError(f"selected ordinal {selected_ordinal} is not among the candidates")
        plan_id = uuid.uuid4()
        candidate_ids = {c.ordinal: uuid.uuid4() for c in candidates}
        now = _now()
        try:
            async with self._session.begin_nested():
                row = TopicPlanRow(
                    id=plan_id,
                    plan_date=plan.plan_date,
                    strategy_profile_id=plan.strategy_profile_id,
                    strategy_version=plan.strategy_version,
                    content_profile_id=plan.content_profile_id,
                    content_profile_version=plan.content_profile_version,
                    selected_candidate_id=None,
                    topic=plan.topic,
                    subject=plan.subject,
                    angle=plan.angle,
                    era=plan.era,
                    theme=plan.theme,
                    hook=plan.hook,
                    entities=list(plan.entities),
                    score=plan.score,
                    score_breakdown=dict(plan.score_breakdown),
                    duplicate_score=plan.duplicate_score,
                    duplicate_level=plan.duplicate_level.value,
                    analytics_mode=plan.analytics_mode.value,
                    analytics_snapshot_id=(
                        _as_uuid(plan.analytics_snapshot_id) if plan.analytics_snapshot_id else None
                    ),
                    analytics_confidence=plan.analytics_confidence,
                    planner_version=plan.planner_version,
                    prompt_version=plan.prompt_version,
                    status=TopicPlanStatus.PLANNED.value,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(row)
                await self._session.flush()  # 一意制約の衝突はここで分かる
                self._session.add_all(
                    TopicCandidateRow(
                        id=candidate_ids[c.ordinal],
                        topic_plan_id=plan_id,
                        ordinal=c.ordinal,
                        round=c.round,
                        payload=dict(c.payload),
                        subject=c.subject,
                        angle=c.angle,
                        duplicate_level=c.duplicate_level.value,
                        duplicate_score=c.duplicate_score,
                        duplicate_of=c.duplicate_of,
                        score=c.score,
                        score_breakdown=(
                            dict(c.score_breakdown) if c.score_breakdown is not None else None
                        ),
                        rejected=c.rejected,
                        created_at=now,
                    )
                    for c in candidates
                )
                await self._session.flush()
                row.selected_candidate_id = candidate_ids[selected_ordinal]
                await self._session.flush()
        except IntegrityError:
            winner = await self.find(
                plan.plan_date, plan.strategy_profile_id, plan.content_profile_id
            )
            if winner is None:
                raise  # 一意制約以外の違反。握りつぶさない
            return SavedTopicPlan(plan=winner, reused=True)
        saved = await self.get(plan_id)
        assert saved is not None
        return SavedTopicPlan(plan=saved, reused=False)

    async def list_candidates(self, plan_id: uuid.UUID | str) -> list[TopicCandidateRecord]:
        result = await self._session.scalars(
            select(TopicCandidateRow)
            .where(TopicCandidateRow.topic_plan_id == _as_uuid(plan_id))
            .order_by(TopicCandidateRow.ordinal)
        )
        return [_to_candidate(r) for r in result.all()]

    async def mark_assigned(self, plan_id: uuid.UUID | str) -> None:
        await self._session.execute(
            update(TopicPlanRow)
            .where(TopicPlanRow.id == _as_uuid(plan_id))
            .values(status=TopicPlanStatus.ASSIGNED.value, updated_at=_now())
            .execution_options(synchronize_session=False)
        )

    async def list_memory(self) -> list[MemoryItem]:
        """Content Memory: 全 plan（状態を問わない）+ plan の無い Episode（topic あり・未 cancel）。

        plan に結び付いた Episode は plan として1回だけ数える（正規化した列を持つのは plan）。
        その status は Episode の状態（制作中の題材が ``in_progress`` 等で見える）。
        plan の無い Episode（Planner 導入前）は題名だけの項目になる。古い順。
        """
        items: list[tuple[datetime, MemoryItem]] = []
        plans = await self._session.execute(
            select(TopicPlanRow, EpisodeRow.status)
            .outerjoin(EpisodeRow, EpisodeRow.topic_plan_id == TopicPlanRow.id)
            .execution_options(populate_existing=True)
        )
        for plan, episode_status in plans.all():
            items.append(
                (
                    datetime.combine(plan.plan_date, datetime.min.time(), UTC),
                    MemoryItem(
                        topic=plan.topic,
                        subject=plan.subject,
                        entities=list(plan.entities),
                        era=plan.era,
                        theme=plan.theme,
                        angle=plan.angle,
                        day=plan.plan_date.isoformat(),
                        status=episode_status or plan.status,
                    ),
                )
            )
        legacy = await self._session.scalars(
            select(EpisodeRow).where(
                EpisodeRow.topic_plan_id.is_(None),
                EpisodeRow.topic.is_not(None),
                EpisodeRow.status != EpisodeStatus.CANCELLED.value,
            )
        )
        for episode in legacy.all():
            created = episode.created_at
            if created.tzinfo is None:  # SQLite は tz を落とす
                created = created.replace(tzinfo=UTC)
            items.append(
                (
                    created,
                    MemoryItem(
                        topic=episode.topic or "",
                        subject=None,
                        entities=[],
                        era=None,
                        theme=None,
                        angle=None,
                        day=created.date().isoformat(),
                        status=episode.status,
                    ),
                )
            )
        items.sort(key=lambda pair: (pair[0], pair[1].topic))
        return [item for _, item in items]

    async def plans_by_video_id(self, video_ids: Iterable[str]) -> dict[str, TopicPlan]:
        """YouTube video id → その動画の TopicPlan（Analytics の特徴量の結合用）。

        video id は upload 予約（``youtube_upload`` の spent 行）の ``provider_result_ref``
        （ADR-0020）。plan の無い Episode の動画は結果に含まれない。
        """
        wanted = sorted(set(video_ids))
        if not wanted:
            return {}
        result = await self._session.execute(
            select(ProviderReservationRow.provider_result_ref, TopicPlanRow)
            .join(EpisodeRow, EpisodeRow.id == ProviderReservationRow.episode_id)
            .join(TopicPlanRow, TopicPlanRow.id == EpisodeRow.topic_plan_id)
            .where(
                ProviderReservationRow.provider == ProviderCall.YOUTUBE_UPLOAD.value,
                ProviderReservationRow.status == ReservationStatus.SPENT.value,
                ProviderReservationRow.provider_result_ref.in_(wanted),
            )
        )
        return {str(video_id): _to_topic_plan(plan) for video_id, plan in result.all()}


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    id: str
    snapshot_date: date
    provider: str
    payload: dict[str, Any]
    fetched_at: datetime


def _to_snapshot(row: AnalyticsSnapshotRow) -> AnalyticsSnapshot:
    return AnalyticsSnapshot(
        id=str(row.id),
        snapshot_date=row.snapshot_date,
        provider=row.provider,
        payload=dict(row.payload),
        fetched_at=row.fetched_at,
    )


class AnalyticsSnapshotRepository:
    """Analytics の取得結果（ADR-0025）。(日, provider) に1行。flush まで。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _find(self, snapshot_date: date, provider: str) -> AnalyticsSnapshotRow | None:
        result = await self._session.execute(
            select(AnalyticsSnapshotRow)
            .where(
                AnalyticsSnapshotRow.snapshot_date == snapshot_date,
                AnalyticsSnapshotRow.provider == provider,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def save(
        self, snapshot_date: date, provider: str, payload: dict[str, Any]
    ) -> AnalyticsSnapshot:
        """冪等。同じ (日, provider) が既にあれば既存を返す（先に取った方が勝つ・上書きしない）。"""
        existing = await self._find(snapshot_date, provider)
        if existing is not None:
            return _to_snapshot(existing)
        row = AnalyticsSnapshotRow(
            id=uuid.uuid4(),
            snapshot_date=snapshot_date,
            provider=provider,
            payload=dict(payload),
            fetched_at=_now(),
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            winner = await self._find(snapshot_date, provider)
            if winner is None:
                raise
            return _to_snapshot(winner)
        return _to_snapshot(row)

    async def latest(self, provider: str) -> AnalyticsSnapshot | None:
        result = await self._session.scalars(
            select(AnalyticsSnapshotRow)
            .where(AnalyticsSnapshotRow.provider == provider)
            .order_by(
                AnalyticsSnapshotRow.snapshot_date.desc(), AnalyticsSnapshotRow.fetched_at.desc()
            )
            .limit(1)
        )
        row = result.first()
        return _to_snapshot(row) if row else None
