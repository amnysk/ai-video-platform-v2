"""SQLAlchemyモデル。PostgreSQLがsource of truth（INV-7）。

型は方言中立なものだけを使う（``Uuid`` / ``DateTime(timezone=True)`` / ``String``）。
これにより同じモデル・同じマイグレーションを PostgreSQL と SQLite の両方へ適用でき、
リポジトリのテストを実DBなしでも同じコードパスで走らせられる。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
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


#: シーン単位の型（ADR-0018）。これらは scene_id 必須、他の型は scene_id NULL。
SCENE_ARTIFACT_TYPES: tuple[ArtifactType, ...] = (
    ArtifactType.SCENE_IMAGE,
    ArtifactType.SCENE_VIDEO,
    ArtifactType.SCENE_VOICE,
)
SCENE_JOB_TYPES: tuple[JobType, ...] = (
    JobType.PRODUCE_SCENE_IMAGE,
    JobType.PRODUCE_SCENE_VIDEO,
    JobType.PRODUCE_SCENE_VOICE,
)
#: シーン単位でしか呼ばない provider（scene_id 必須。他は任意）。
SCENE_PROVIDER_CALLS: tuple[ProviderCall, ...] = (ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO)


def _in_list(values: tuple[Enum, ...]) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


def _scene_scope_check(column: str, values: tuple[Enum, ...], name: str) -> CheckConstraint:
    """シーン単位の型 ⇔ scene_id あり（ADR-0018）。"""
    allowed = _in_list(values)
    return CheckConstraint(
        f"({column} IN ({allowed}) AND scene_id IS NOT NULL) "
        f"OR ({column} NOT IN ({allowed}) AND scene_id IS NULL)",
        name=name,
    )


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
        _scene_scope_check("type", SCENE_JOB_TYPES, "ck_jobs_scene_scope"),
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
    #: シーン単位の job（ADR-0018）。NULL は Episode 単位の job。
    scene_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
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
        _scene_scope_check(
            "artifact_type", SCENE_ARTIFACT_TYPES, "ck_artifact_metadata_scene_scope"
        ),
        # 一意性の索引（content / version / current）は scene キーを含む式索引なので
        # クラス定義の後で張る（ADR-0018）。
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
    #: シーン単位の Artifact（ADR-0018）。NULL は Episode 単位（script / storyboard 等）。
    scene_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
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
        CheckConstraint(
            f"provider NOT IN ({_in_list(SCENE_PROVIDER_CALLS)}) OR scene_id IS NOT NULL",
            name="ck_provider_reservations_scene_scope",
        ),
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
    #: シーン単位の呼び出し（ADR-0018）。未照合予約の検査をシーンごとに独立させる。
    scene_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 非同期ジョブ型 provider のジョブ参照（不透明。ADR-0017）。submit 直後に1度だけ書く。
    provider_job_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 外部呼び出しの結果参照（不透明。YouTube video id、ADR-0020）。受領 Artifact より先に書く。
    provider_result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 予約時点の見積もり（USD、ADR-0013 Alternatives (e) を ADR-0017 で解決）。確定額ではない。
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)


#: scene キー。NULL を '' に畳んで一意性の比較に使う（ADR-0018）。
_SCENE_KEY = func.coalesce(ArtifactMetadataRow.scene_id, "")

# INV-17: 同じ内容の再記録が重複行を作らない。ADR-0012: content-addressed な同一性（INV-11）。
Index(
    "uq_artifact_metadata_content",
    ArtifactMetadataRow.episode_id,
    ArtifactMetadataRow.artifact_type,
    _SCENE_KEY,
    ArtifactMetadataRow.sha256,
    unique=True,
)
Index(
    "uq_artifact_metadata_version",
    ArtifactMetadataRow.episode_id,
    ArtifactMetadataRow.artifact_type,
    _SCENE_KEY,
    ArtifactMetadataRow.version,
    unique=True,
)
# ADR-0012 / ADR-0018: 現行世代（superseded_at IS NULL）は scene キーごとに常に1本。
Index(
    "uq_artifact_metadata_current",
    ArtifactMetadataRow.episode_id,
    ArtifactMetadataRow.artifact_type,
    _SCENE_KEY,
    unique=True,
    sqlite_where=text("superseded_at IS NULL"),
    postgresql_where=text("superseded_at IS NULL"),
)


__all__ = [
    "ArtifactMetadataRow",
    "Base",
    "EpisodeRow",
    "FailureClass",
    "JobRow",
    "ProviderReservationRow",
]
