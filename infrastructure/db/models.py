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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
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
        UniqueConstraint(
            "episode_id", "artifact_type", "sha256", name="uq_artifact_metadata_content"
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
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    produced_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["ArtifactMetadataRow", "Base", "EpisodeRow", "JobRow", "FailureClass"]
