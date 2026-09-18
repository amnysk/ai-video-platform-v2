"""SQLAlchemyモデル。PostgreSQLがsource of truth（INV-7）。

型は方言中立なものだけを使う（``Uuid`` / ``DateTime(timezone=True)`` / ``String``）。
これにより同じモデル・同じマイグレーションを PostgreSQL と SQLite の両方へ適用でき、
リポジトリのテストを実DBなしでも同じコードパスで走らせられる。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
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

from contracts.operations import OperationalSwitch
from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
    ProviderCall,
    ReservationStatus,
)
from contracts.topic import TOPIC_MAX_CHARS
from contracts.topic_planning import AnalyticsMode, DuplicateLevel, TopicPlanStatus


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
    __table_args__ = (
        _check("status", EpisodeStatus, "ck_episodes_status"),
        # 1つの TopicPlan は1つの Episode にだけ結び付く（ADR-0025）
        UniqueConstraint("topic_plan_id", name="uq_episodes_topic_plan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(TOPIC_MAX_CHARS), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: この Episode の題材を決めた TopicPlan（ADR-0025）。Planner 導入前の Episode は NULL
    topic_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("topic_plans.id", name="fk_episodes_topic_plan_id"), nullable=True
    )
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


class OperationalSwitchRow(Base):
    """DB の停止スイッチ（ADR-0021）。行が無ければ off。"""

    __tablename__ = "operational_switches"
    __table_args__ = (_check("name", OperationalSwitch, "ck_operational_switches_name"),)

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    is_on: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DailyEpisodeSlotRow(Base):
    """日次 Episode 枠（ADR-0021）。1日の本数上限を DB の主キーで守る。

    ``(slot_date, slot_index)`` の主キーで同じ番号の枠を2本作れず、``trigger_id`` の一意性で
    同じ起動（Temporal workflow id）の再試行が枠を増やさない。
    """

    __tablename__ = "daily_episode_slots"
    __table_args__ = (
        CheckConstraint("slot_index >= 0", name="ck_daily_episode_slots_index_nonnegative"),
        UniqueConstraint("trigger_id", name="uq_daily_episode_slots_trigger_id"),
        UniqueConstraint("episode_id", name="uq_daily_episode_slots_episode_id"),
    )

    slot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    slot_index: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    trigger_id: Mapped[str] = mapped_column(String(255), nullable=False)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnalyticsSnapshotRow(Base):
    """Analytics の取得結果（ADR-0025）。live 取得に失敗したときの stale fallback 元。"""

    __tablename__ = "analytics_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "provider", name="uq_analytics_snapshots_day_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TopicPlanRow(Base):
    """1日・1 profile の組につき1つの確定 Topic（ADR-0025）。一度確定したら上書きしない。"""

    __tablename__ = "topic_plans"
    __table_args__ = (
        _check("status", TopicPlanStatus, "ck_topic_plans_status"),
        _check("analytics_mode", AnalyticsMode, "ck_topic_plans_analytics_mode"),
        _check("duplicate_level", DuplicateLevel, "ck_topic_plans_duplicate_level"),
        UniqueConstraint(
            "plan_date",
            "strategy_profile_id",
            "content_profile_id",
            name="uq_topic_plans_day_profile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    plan_date: Mapped[date] = mapped_column(Date, nullable=False)
    strategy_profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content_profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 採用した候補。候補と plan は循環参照なので ALTER で張る（use_alter）
    selected_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey(
            "topic_candidates.id",
            name="fk_topic_plans_selected_candidate_id",
            use_alter=True,
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(80), nullable=False)
    angle: Mapped[str] = mapped_column(String(32), nullable=False)
    era: Mapped[str] = mapped_column(String(40), nullable=False)
    theme: Mapped[str] = mapped_column(String(60), nullable=False)
    hook: Mapped[str] = mapped_column(String(300), nullable=False)
    entities: Mapped[list] = mapped_column(JSON, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    score_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False)
    duplicate_score: Mapped[float] = mapped_column(Float, nullable=False)
    duplicate_level: Mapped[str] = mapped_column(String(16), nullable=False)
    analytics_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    analytics_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("analytics_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    analytics_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    planner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TopicCandidateRow(Base):
    """Planner が検討した候補（採否と理由を含む監査記録。ADR-0025）。"""

    __tablename__ = "topic_candidates"
    __table_args__ = (
        _check("duplicate_level", DuplicateLevel, "ck_topic_candidates_duplicate_level"),
        UniqueConstraint("topic_plan_id", "ordinal", name="uq_topic_candidates_plan_ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    topic_plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("topic_plans.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    #: validation 済み ``TopicCandidate`` の JSON（INV-23）
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    subject: Mapped[str] = mapped_column(String(80), nullable=False)
    angle: Mapped[str] = mapped_column(String(32), nullable=False)
    duplicate_level: Mapped[str] = mapped_column(String(16), nullable=False)
    duplicate_score: Mapped[float] = mapped_column(Float, nullable=False)
    duplicate_of: Mapped[str | None] = mapped_column(String(200), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_breakdown: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
    "AnalyticsSnapshotRow",
    "ArtifactMetadataRow",
    "Base",
    "EpisodeRow",
    "FailureClass",
    "JobRow",
    "ProviderReservationRow",
    "TopicCandidateRow",
    "TopicPlanRow",
]
