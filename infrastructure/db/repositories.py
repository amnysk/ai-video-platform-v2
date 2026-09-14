"""リポジトリ。状態遷移は必ず domain の表を通す（AGENTS.md §8）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
from domain.artifact.entities import ArtifactMetadata
from domain.episode.entities import Episode
from domain.episode.transitions import EpisodeEvent, Rejected, transition_episode
from domain.errors import InvalidTransitionError, UnreconciledReservationError
from domain.job.entities import Job
from domain.job.transitions import JobEvent, transition_job
from domain.provider.reservations import ReservationEvent, transition_reservation
from infrastructure.db.models import (
    ArtifactMetadataRow,
    EpisodeRow,
    JobRow,
    ProviderReservationRow,
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
