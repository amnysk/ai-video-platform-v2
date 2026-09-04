"""SQLAlchemyモデル。PostgreSQLがsource of truth（INV-7）。

型は方言中立なものだけを使う（``Uuid`` / ``DateTime(timezone=True)`` / ``String``）。
これにより同じモデル・同じマイグレーションを PostgreSQL と SQLite の両方へ適用でき、
リポジトリのテストを実DBなしでも同じコードパスで走らせられる。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
    ProviderCall,
    ReservationStatus,
)


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _check(column: str, enum_cls: type[Enum], name: str) -> CheckConstraint:
    """状態値の語彙をDB制約としても表現する。アプリのバグより先に止める。"""
    allowed = ", ".join(f"'{value}'" for value in _enum_values(enum_cls))
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class Base(DeclarativeBase):
    pass


class EpisodeRow(Base):
    __tablename__ = "episodes"
    __table_args__ = (_check("status", EpisodeStatus, "ck_episodes_status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(500), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # onupdate=func.now() は使わない。サーバ側関数だと flush 後に列が expire し、
    # 非同期セッションの外で遅延ロードが走って MissingGreenlet になる。
    # updated_at はリポジトリが必ず明示的に設定する。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    jobs: Mapped[list[JobRow]] = relationship(back_populates="episode")


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        _check("status", JobStatus, "ck_jobs_status"),
        _check("type", JobType, "ck_jobs_type"),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
        Index("ix_jobs_episode_id", "episode_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # onupdate=func.now() は使わない。サーバ側関数だと flush 後に列が expire し、
    # 非同期セッションの外で遅延ロードが走って MissingGreenlet になる。
    # updated_at はリポジトリが必ず明示的に設定する。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    episode: Mapped[EpisodeRow] = relationship(back_populates="jobs")


class ArtifactMetadataRow(Base):
    __tablename__ = "artifact_metadata"
    __table_args__ = (
        _check("artifact_type", ArtifactType, "ck_artifact_metadata_type"),
        # INV-17: 同じ内容の再記録が重複行を作らない。制約で保証する。
        # ADR-0012: content-addressed な同一性は残す（INV-11 immutable の担保）。
        UniqueConstraint(
            "episode_id", "artifact_type", "sha256", name="uq_artifact_metadata_content"
        ),
        UniqueConstraint(
            "episode_id", "artifact_type", "version", name="uq_artifact_metadata_version"
        ),
        # ADR-0012: 現行世代（superseded_at IS NULL）は常に1本。partial unique index
        # なので SQLite / PostgreSQL の両方に方言別の WHERE を渡す。
        Index(
            "uq_artifact_metadata_current",
            "episode_id",
            "artifact_type",
            unique=True,
            sqlite_where=text("superseded_at IS NULL"),
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_artifact_metadata_episode_id", "episode_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: この成果物を作った**入力**の指紋（domain/script/identity.py）。
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 同じ (episode_id, artifact_type) 内で単調増加する世代番号。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: NULL なら現行世代。非NULLなら後続世代に降ろされた。
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    produced_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProviderReservationRow(Base):
    """外部AI呼び出しの予約台帳（ADR-0013）。

    ``jobs`` への列追加ではなく独立テーブルにするのは、1つの Job が複数ラウンド＝
    複数回の外部呼び出しを持ちうるためで、列追加だと上書きになり
    「未照合の予約が消える」形をこちらが作ってしまう。

    コスト列（``estimated_cost_jpy`` 等）は**入れない**。Codex はサブスクリプション
    実行で per-call 課金が無く、今は読み手がゼロだからである（ADR-0013 Alternatives (e)）。
    """

    __tablename__ = "provider_reservations"
    __table_args__ = (
        _check("provider", ProviderCall, "ck_provider_reservations_provider"),
        _check("status", ReservationStatus, "ck_provider_reservations_status"),
        _check("failure_class", FailureClass, "ck_provider_reservations_failure_class"),
        CheckConstraint("round >= 1", name="ck_provider_reservations_round_positive"),
        # 二重呼び出しの防止をアプリのバグで破れないよう DB 制約にする。
        UniqueConstraint("idempotency_key", name="uq_provider_reservations_idempotency_key"),
        Index("ix_provider_reservations_episode_id", "episode_id"),
        Index(
            "ix_provider_reservations_lookup",
            "episode_id",
            "provider",
            "input_hash",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 生出力（provider-raw/）のキー。Artifact ではない（ADR-0013）。照合の evidence。
    raw_output_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    outcome_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("artifact_metadata.id", ondelete="SET NULL"), nullable=True
    )
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: subprocess を起動する**直前**に commit する。呼んだ可能性の境界。
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = [
    "ArtifactMetadataRow",
    "Base",
    "EpisodeRow",
    "FailureClass",
    "JobRow",
    "ProviderReservationRow",
]
