"""production 工程の語彙とシーン単位の Artifact（ADR-0017 / ADR-0018）

1. CHECK 語彙の貼り替え（0003 と同じ構造）。upgrade は ``contracts.states`` から導出し、
   downgrade は **c80a987 時点の Phase 3 の値を literal で凍結する**
   （照合: ``tests/contract/test_migration_frozen_vocabulary.py``）
2. ``scene_id``（jobs / artifact_metadata / provider_reservations）、
   ``provider_job_ref`` / ``estimated_cost_usd``（provider_reservations）を追加
3. artifact_metadata の一意性（content / version / current）を scene キー
   ``coalesce(scene_id, '')`` 込みの索引へ張り替える

downgrade は Phase 4 の値を持つ行が残っていると CHECK の作成で**失敗する**。
行を消す・書き換えることはしない（データ移行は人間の判断）。語彙の検査を先に行うので、
シーン単位の行（必ず Phase 4 の型を持つ）が残っていれば一意性の復元より前に止まる。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from alembic import op

from contracts.states import ArtifactType, EpisodeStatus, JobType, ProviderCall

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None

#: Phase 3（c80a987）の CHECK 語彙。**編集しない**（履歴の凍結値）。
PHASE3_EPISODE_STATUSES: tuple[str, ...] = (
    "planned",
    "in_progress",
    "needs_work",
    "blocked",
    "ready_for_review",
    "approved",
    "uploaded",
    "analyzed",
    "completed",
    "script_ready",
    "storyboard_ready",
    "failed",
    "cancelled",
)
PHASE3_JOB_TYPES: tuple[str, ...] = ("dummy", "write_script", "plan_storyboard")
PHASE3_ARTIFACT_TYPES: tuple[str, ...] = ("dummy", "script", "storyboard")
PHASE3_PROVIDER_CALLS: tuple[str, ...] = ("codex_script", "codex_storyboard")

#: (table, constraint, column, enum)
_CHECKS: tuple[tuple[str, str, str, type[Enum]], ...] = (
    ("episodes", "ck_episodes_status", "status", EpisodeStatus),
    ("jobs", "ck_jobs_type", "type", JobType),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", ArtifactType),
    ("provider_reservations", "ck_provider_reservations_provider", "provider", ProviderCall),
)

#: downgrade 先（0003）の CHECK。(table, constraint, column, 凍結値)
_PHASE3_CHECKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("episodes", "ck_episodes_status", "status", PHASE3_EPISODE_STATUSES),
    ("jobs", "ck_jobs_type", "type", PHASE3_JOB_TYPES),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", PHASE3_ARTIFACT_TYPES),
    (
        "provider_reservations",
        "ck_provider_reservations_provider",
        "provider",
        PHASE3_PROVIDER_CALLS,
    ),
)

_SCENE_KEY = "coalesce(scene_id, '')"
_CURRENT_WHERE = "superseded_at IS NULL"


def _in_check(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _recheck(table: str, name: str, condition: str) -> None:
    """CHECK を DROP して新しい条件で作り直す（SQLite は batch でテーブル再作成）。"""
    with op.batch_alter_table(table, schema=None) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, sa.text(condition))


def upgrade() -> None:
    for table, name, column, enum_cls in _CHECKS:
        _recheck(table, name, _in_check(column, tuple(m.value for m in enum_cls)))

    with op.batch_alter_table("jobs", schema=None) as batch:
        batch.add_column(sa.Column("scene_id", sa.String(length=16), nullable=True))

    with op.batch_alter_table("provider_reservations", schema=None) as batch:
        batch.add_column(sa.Column("scene_id", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("provider_job_ref", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("estimated_cost_usd", sa.Numeric(precision=10, scale=4), nullable=True)
        )

    op.drop_index("uq_artifact_metadata_current", table_name="artifact_metadata")
    with op.batch_alter_table("artifact_metadata", schema=None) as batch:
        batch.add_column(sa.Column("scene_id", sa.String(length=16), nullable=True))
        batch.drop_constraint("uq_artifact_metadata_content", type_="unique")
        batch.drop_constraint("uq_artifact_metadata_version", type_="unique")

    op.create_index(
        "uq_artifact_metadata_content",
        "artifact_metadata",
        ["episode_id", "artifact_type", sa.text(_SCENE_KEY), "sha256"],
        unique=True,
    )
    op.create_index(
        "uq_artifact_metadata_version",
        "artifact_metadata",
        ["episode_id", "artifact_type", sa.text(_SCENE_KEY), "version"],
        unique=True,
    )
    op.create_index(
        "uq_artifact_metadata_current",
        "artifact_metadata",
        ["episode_id", "artifact_type", sa.text(_SCENE_KEY)],
        unique=True,
        sqlite_where=sa.text(_CURRENT_WHERE),
        postgresql_where=sa.text(_CURRENT_WHERE),
    )


def downgrade() -> None:
    """Phase 4 の値を持つ行が残っていれば CHECK の作成で失敗する（行は触らない）。

    式索引は語彙の貼り替えより**先に**落とす。SQLite の batch（テーブル再作成）は式索引を
    反映できず黙って失うため。PostgreSQL では DDL がトランザクション内なので、語彙の検査で
    失敗すれば索引の削除も巻き戻る。
    """
    op.drop_index("uq_artifact_metadata_current", table_name="artifact_metadata")
    op.drop_index("uq_artifact_metadata_version", table_name="artifact_metadata")
    op.drop_index("uq_artifact_metadata_content", table_name="artifact_metadata")

    for table, name, column, values in _PHASE3_CHECKS:
        _recheck(table, name, _in_check(column, values))
    with op.batch_alter_table("artifact_metadata", schema=None) as batch:
        batch.drop_column("scene_id")
        batch.create_unique_constraint(
            "uq_artifact_metadata_content", ["episode_id", "artifact_type", "sha256"]
        )
        batch.create_unique_constraint(
            "uq_artifact_metadata_version", ["episode_id", "artifact_type", "version"]
        )
    op.create_index(
        "uq_artifact_metadata_current",
        "artifact_metadata",
        ["episode_id", "artifact_type"],
        unique=True,
        sqlite_where=sa.text(_CURRENT_WHERE),
        postgresql_where=sa.text(_CURRENT_WHERE),
    )

    with op.batch_alter_table("provider_reservations", schema=None) as batch:
        batch.drop_column("estimated_cost_usd")
        batch.drop_column("provider_job_ref")
        batch.drop_column("scene_id")

    with op.batch_alter_table("jobs", schema=None) as batch:
        batch.drop_column("scene_id")
