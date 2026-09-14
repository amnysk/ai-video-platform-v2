"""render 工程の語彙（ADR-0019）

CHECK 語彙の貼り替え（0004 と同じ構造）。upgrade は ``contracts.states`` から導出し、
downgrade は **dad86f5 時点の Phase 4 の値を literal で凍結する**
（照合: ``tests/contract/test_migration_frozen_vocabulary.py``）。

``final_video`` / ``render_final_video`` は Episode 単位なので、0004 の scene_scope CHECK
（シーン単位の型の集合を literal で凍結）はそのまま正しく、張り替えない。
provider 予約の語彙は増えない（ローカル計算で課金が無い。ADR-0019）。

downgrade は Phase 5 の値を持つ行が残っていると CHECK の作成で**失敗する**。
行を消す・書き換えることはしない（データ移行は人間の判断）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-14
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from alembic import op

from contracts.states import ArtifactType, EpisodeStatus, JobType

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None

#: Phase 4（dad86f5）の CHECK 語彙。**編集しない**（履歴の凍結値）。
PHASE4_EPISODE_STATUSES: tuple[str, ...] = (
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
    "assets_ready",
    "failed",
    "cancelled",
)
PHASE4_JOB_TYPES: tuple[str, ...] = (
    "dummy",
    "write_script",
    "plan_storyboard",
    "produce_scene_image",
    "produce_scene_voice",
    "produce_scene_video",
    "assemble_production",
)
PHASE4_ARTIFACT_TYPES: tuple[str, ...] = (
    "dummy",
    "script",
    "storyboard",
    "scene_image",
    "scene_voice",
    "scene_video",
    "production_manifest",
)

#: (table, constraint, column, enum)
_CHECKS: tuple[tuple[str, str, str, type[Enum]], ...] = (
    ("episodes", "ck_episodes_status", "status", EpisodeStatus),
    ("jobs", "ck_jobs_type", "type", JobType),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", ArtifactType),
)

#: downgrade 先（0004）の CHECK。(table, constraint, column, 凍結値)
_PHASE4_CHECKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("episodes", "ck_episodes_status", "status", PHASE4_EPISODE_STATUSES),
    ("jobs", "ck_jobs_type", "type", PHASE4_JOB_TYPES),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", PHASE4_ARTIFACT_TYPES),
)


def _in_check(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _recheck(table: str, name: str, condition: str) -> None:
    """CHECK を DROP して新しい条件で作り直す（SQLite は batch でテーブル再作成）。"""
    with op.batch_alter_table(table, schema=None) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, sa.text(condition))


#: 0004 が張った artifact_metadata の式索引（scene キー込み）。**編集しない**。
_SCENE_KEY = "coalesce(scene_id, '')"
_CURRENT_WHERE = "superseded_at IS NULL"
_ARTIFACT_INDEXES: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("uq_artifact_metadata_content", ("episode_id", "artifact_type", _SCENE_KEY, "sha256"), None),
    ("uq_artifact_metadata_version", ("episode_id", "artifact_type", _SCENE_KEY, "version"), None),
    ("uq_artifact_metadata_current", ("episode_id", "artifact_type", _SCENE_KEY), _CURRENT_WHERE),
)


def _drop_artifact_indexes() -> None:
    """SQLite の batch（テーブル再作成）は式索引を黙って失うので、張り替えの前に落とす。"""
    for name, _columns, _where in _ARTIFACT_INDEXES:
        op.drop_index(name, table_name="artifact_metadata")


def _create_artifact_indexes() -> None:
    for name, columns, where in _ARTIFACT_INDEXES:
        op.create_index(
            name,
            "artifact_metadata",
            [sa.text(c) if c == _SCENE_KEY else c for c in columns],
            unique=True,
            sqlite_where=sa.text(where) if where else None,
            postgresql_where=sa.text(where) if where else None,
        )


def _apply(checks: tuple[tuple[str, str, str, tuple[str, ...]], ...]) -> None:
    _drop_artifact_indexes()
    for table, name, column, values in checks:
        _recheck(table, name, _in_check(column, values))
    _create_artifact_indexes()


def upgrade() -> None:
    _apply(
        tuple(
            (table, name, column, tuple(m.value for m in enum_cls))
            for table, name, column, enum_cls in _CHECKS
        )
    )


def downgrade() -> None:
    """Phase 5 の値を持つ行が残っていれば CHECK の作成で失敗する（行は触らない）。

    PostgreSQL では DDL がトランザクション内なので、失敗すれば索引の削除も巻き戻る。
    """
    _apply(_PHASE4_CHECKS)
