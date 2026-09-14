"""upload 工程の語彙と投稿結果の参照列（ADR-0020）

CHECK 語彙の貼り替え（0005 と同じ構造）。upgrade は ``contracts.states`` から導出し、
downgrade は **a51bc13 時点の Phase 5 の値を literal で凍結する**
（照合: ``tests/contract/test_migration_frozen_vocabulary.py``）。

- ``jobs.type`` に ``upload_final_video``、``artifact_metadata.artifact_type`` に
  ``upload_receipt``、``provider_reservations.provider`` に ``youtube_upload`` を足す
- Episode 状態は増えない（既存の ``uploaded`` を使う）ので ``ck_episodes_status`` は触らない
- 投稿は Episode 単位なので scene_scope CHECK（0004 の凍結集合）はそのまま正しい
- ``provider_reservations.provider_result_ref``: 外部呼び出しの結果参照（YouTube video id）。
  受領 Artifact を書く前に予約へ永続化し、crash 後の再実行が YouTube を再度呼ばずに受領を作れる
  ようにする（session URI は既存の ``provider_job_ref`` に入る）

downgrade は Phase 6 の値を持つ行が残っていると CHECK の作成で**失敗する**。
行を消す・書き換えることはしない（データ移行は人間の判断）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-15
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from alembic import op

from contracts.states import ArtifactType, JobType, ProviderCall

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None

#: Phase 5（a51bc13）の CHECK 語彙。**編集しない**（履歴の凍結値）。
PHASE5_JOB_TYPES: tuple[str, ...] = (
    "dummy",
    "write_script",
    "plan_storyboard",
    "produce_scene_image",
    "produce_scene_voice",
    "produce_scene_video",
    "assemble_production",
    "render_final_video",
)
PHASE5_ARTIFACT_TYPES: tuple[str, ...] = (
    "dummy",
    "script",
    "storyboard",
    "scene_image",
    "scene_voice",
    "scene_video",
    "production_manifest",
    "final_video",
)
PHASE5_PROVIDER_CALLS: tuple[str, ...] = (
    "codex_script",
    "codex_storyboard",
    "fal_image",
    "fal_video",
)

#: (table, constraint, column, enum)
_CHECKS: tuple[tuple[str, str, str, type[Enum]], ...] = (
    ("jobs", "ck_jobs_type", "type", JobType),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", ArtifactType),
    ("provider_reservations", "ck_provider_reservations_provider", "provider", ProviderCall),
)

#: downgrade 先（0005）の CHECK。(table, constraint, column, 凍結値)
_PHASE5_CHECKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("jobs", "ck_jobs_type", "type", PHASE5_JOB_TYPES),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", PHASE5_ARTIFACT_TYPES),
    (
        "provider_reservations",
        "ck_provider_reservations_provider",
        "provider",
        PHASE5_PROVIDER_CALLS,
    ),
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


def _apply(checks: tuple[tuple[str, str, str, tuple[str, ...]], ...]) -> None:
    # SQLite の batch（テーブル再作成）は式索引を黙って失うので、張り替えの前に落とす。
    for name, _columns, _where in _ARTIFACT_INDEXES:
        op.drop_index(name, table_name="artifact_metadata")
    for table, name, column, values in checks:
        _recheck(table, name, _in_check(column, values))
    for name, columns, where in _ARTIFACT_INDEXES:
        op.create_index(
            name,
            "artifact_metadata",
            [sa.text(c) if c == _SCENE_KEY else c for c in columns],
            unique=True,
            sqlite_where=sa.text(where) if where else None,
            postgresql_where=sa.text(where) if where else None,
        )


def upgrade() -> None:
    op.add_column(
        "provider_reservations", sa.Column("provider_result_ref", sa.Text(), nullable=True)
    )
    _apply(
        tuple(
            (table, name, column, tuple(m.value for m in enum_cls))
            for table, name, column, enum_cls in _CHECKS
        )
    )


def downgrade() -> None:
    """Phase 6 の値を持つ行が残っていれば CHECK の作成で失敗する（行は触らない）。"""
    _apply(_PHASE5_CHECKS)
    with op.batch_alter_table("provider_reservations", schema=None) as batch:
        batch.drop_column("provider_result_ref")
