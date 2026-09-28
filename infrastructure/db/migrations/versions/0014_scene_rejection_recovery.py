"""シーン単位の内容拒否の記録・代替映像案・レシピ版を除いた再利用キー（ADR-0035）

1. 語彙の追加（CHECK の貼り替え。0006 と同じ構造）: ``jobs.type`` に ``plan_scene_alternative``、
   ``artifact_metadata.artifact_type`` に ``scene_visual_override``、provider に
   ``codex_scene_alternative``。3つともシーン単位なので ``*_scene_scope`` も貼り替える。
   upgrade・downgrade とも **literal で凍結**する（照合:
   ``tests/contract/test_migration_frozen_vocabulary.py``）。
2. ``artifact_metadata.content_fingerprint``（NULL 可）: 生成レシピの版を除いた入力指紋。
3. ``provider_rejections``: provider による内容拒否を1件ずつ構造化して保存する。
4. 既存データの一度きりの補完: ``error_summary`` が ``ProviderRejectedError:`` で始まる
   spent 予約に ``input_rejected_by_provider = true`` を立て、``provider_rejections`` を1行ずつ
   作る。拒否の対象・理由は ``error_summary`` の文字列から判定する（構造化される前の行なので、
   ここだけは文字列しか材料が無い）。判定できなければ ``unknown``。

downgrade は新しい語彙を持つ行が残っていると CHECK の作成で失敗する（行は触らない）。
補完した ``input_rejected_by_provider`` は戻さない（true は「同じ入力を再送しない」安全側）。

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-29
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels = None
depends_on = None

#: 8a8c499（0013 時点）の語彙。**編集しない**（履歴の凍結値）。
PRE_0014_JOB_TYPES: tuple[str, ...] = (
    "dummy",
    "write_script",
    "plan_storyboard",
    "produce_scene_image",
    "produce_scene_voice",
    "produce_scene_video",
    "assemble_production",
    "render_final_video",
    "upload_final_video",
)
PRE_0014_ARTIFACT_TYPES: tuple[str, ...] = (
    "dummy",
    "script",
    "storyboard",
    "scene_image",
    "scene_voice",
    "scene_video",
    "production_manifest",
    "final_video",
    "upload_receipt",
)
PRE_0014_PROVIDER_CALLS: tuple[str, ...] = (
    "codex_script",
    "codex_storyboard",
    "fal_image",
    "fal_video",
    "youtube_upload",
)
PRE_0014_SCENE_ARTIFACT_TYPES: tuple[str, ...] = ("scene_image", "scene_video", "scene_voice")
PRE_0014_SCENE_JOB_TYPES: tuple[str, ...] = (
    "produce_scene_image",
    "produce_scene_video",
    "produce_scene_voice",
)
PRE_0014_SCENE_PROVIDER_CALLS: tuple[str, ...] = ("fal_image", "fal_video")

#: 0014 が足す語彙。**編集しない**。
JOB_TYPES: tuple[str, ...] = (*PRE_0014_JOB_TYPES, "plan_scene_alternative")
ARTIFACT_TYPES: tuple[str, ...] = (*PRE_0014_ARTIFACT_TYPES, "scene_visual_override")
PROVIDER_CALLS: tuple[str, ...] = (*PRE_0014_PROVIDER_CALLS, "codex_scene_alternative")
SCENE_ARTIFACT_TYPES: tuple[str, ...] = (*PRE_0014_SCENE_ARTIFACT_TYPES, "scene_visual_override")
SCENE_JOB_TYPES: tuple[str, ...] = (*PRE_0014_SCENE_JOB_TYPES, "plan_scene_alternative")
SCENE_PROVIDER_CALLS: tuple[str, ...] = (
    *PRE_0014_SCENE_PROVIDER_CALLS,
    "codex_scene_alternative",
)
REJECTED_INPUTS: tuple[str, ...] = ("image", "prompt", "unknown")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _vocabulary_checks(
    jobs: tuple[str, ...],
    artifacts: tuple[str, ...],
    providers: tuple[str, ...],
    scene_artifacts: tuple[str, ...],
    scene_jobs: tuple[str, ...],
    scene_providers: tuple[str, ...],
) -> tuple[tuple[str, str, str], ...]:
    """(table, constraint, 条件)。upgrade と downgrade で同じ集合の制約を張り替える。"""
    return (
        ("jobs", "ck_jobs_type", f"type IN ({_in(jobs)})"),
        (
            "jobs",
            "ck_jobs_scene_scope",
            f"(type IN ({_in(scene_jobs)}) AND scene_id IS NOT NULL) "
            f"OR (type NOT IN ({_in(scene_jobs)}) AND scene_id IS NULL)",
        ),
        ("artifact_metadata", "ck_artifact_metadata_type", f"artifact_type IN ({_in(artifacts)})"),
        (
            "artifact_metadata",
            "ck_artifact_metadata_scene_scope",
            f"(artifact_type IN ({_in(scene_artifacts)}) AND scene_id IS NOT NULL) "
            f"OR (artifact_type NOT IN ({_in(scene_artifacts)}) AND scene_id IS NULL)",
        ),
        (
            "provider_reservations",
            "ck_provider_reservations_provider",
            f"provider IN ({_in(providers)})",
        ),
        (
            "provider_reservations",
            "ck_provider_reservations_scene_scope",
            f"provider NOT IN ({_in(scene_providers)}) OR scene_id IS NOT NULL",
        ),
        (
            "provider_auth_incidents",
            "ck_provider_auth_incidents_provider",
            f"provider IN ({_in(providers)})",
        ),
    )


UPGRADED_CHECKS = _vocabulary_checks(
    JOB_TYPES,
    ARTIFACT_TYPES,
    PROVIDER_CALLS,
    SCENE_ARTIFACT_TYPES,
    SCENE_JOB_TYPES,
    SCENE_PROVIDER_CALLS,
)
DOWNGRADED_CHECKS = _vocabulary_checks(
    PRE_0014_JOB_TYPES,
    PRE_0014_ARTIFACT_TYPES,
    PRE_0014_PROVIDER_CALLS,
    PRE_0014_SCENE_ARTIFACT_TYPES,
    PRE_0014_SCENE_JOB_TYPES,
    PRE_0014_SCENE_PROVIDER_CALLS,
)

#: 0004 が張った artifact_metadata の式索引（scene キー込み）。**編集しない**。
_SCENE_KEY = "coalesce(scene_id, '')"
_CURRENT_WHERE = "superseded_at IS NULL"
_ARTIFACT_INDEXES: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("uq_artifact_metadata_content", ("episode_id", "artifact_type", _SCENE_KEY, "sha256"), None),
    ("uq_artifact_metadata_version", ("episode_id", "artifact_type", _SCENE_KEY, "version"), None),
    ("uq_artifact_metadata_current", ("episode_id", "artifact_type", _SCENE_KEY), _CURRENT_WHERE),
)


def _apply(checks: tuple[tuple[str, str, str], ...], *, drop_fingerprint: bool = False) -> None:
    # SQLite の batch（テーブル再作成）は式索引を黙って失うので、張り替えの前に落とし、
    # artifact_metadata を作り直す操作（列の削除を含む）は全部この間で行う。
    for name, _columns, _where in _ARTIFACT_INDEXES:
        op.drop_index(name, table_name="artifact_metadata")
    if drop_fingerprint:
        with op.batch_alter_table("artifact_metadata", schema=None) as batch:
            batch.drop_column("content_fingerprint")
    for table, name, condition in checks:
        with op.batch_alter_table(table, schema=None) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, sa.text(condition))
    for name, columns, where in _ARTIFACT_INDEXES:
        op.create_index(
            name,
            "artifact_metadata",
            [sa.text(c) if c == _SCENE_KEY else c for c in columns],
            unique=True,
            sqlite_where=sa.text(where) if where else None,
            postgresql_where=sa.text(where) if where else None,
        )


def classify_legacy_rejection(error_summary: str) -> dict[str, object]:
    """構造化される前の ``error_summary`` から拒否の対象と理由を判定する（この migration 専用）。

    ``fal_queue._short`` の2つの形（``(at body.image_url)`` と、ADR-0034 以前の pydantic repr
    ``'loc': ['body', 'image_url']``）の両方を読む。判定できなければ ``unknown``。
    """
    text = error_summary or ""
    if "body.image_url" in text or "'body', 'image_url'" in text:
        rejected_input = "image"
    elif "body.prompt" in text or "'body', 'prompt'" in text:
        rejected_input = "prompt"
    else:
        rejected_input = "unknown"
    types = ["content_policy_violation"] if "content_policy_violation" in text else []
    reason = "partner_validation_failed" if "partner_validation_failed" in text else None
    http_status = 422 if "HTTP 422" in text else None
    return {
        "rejected_input": rejected_input,
        "types": json.dumps(types),
        "reason": reason,
        "message": text[:1000] or None,
        "http_status": http_status,
    }


def _backfill_legacy_rejections() -> None:
    bind = op.get_bind()
    reservations = sa.table(
        "provider_reservations",
        sa.column("id", sa.Uuid()),
        sa.column("episode_id", sa.Uuid()),
        sa.column("scene_id", sa.String()),
        sa.column("provider", sa.String()),
        sa.column("input_hash", sa.String()),
        sa.column("status", sa.String()),
        sa.column("error_summary", sa.String()),
        sa.column("reconciled_at", sa.DateTime(timezone=True)),
        sa.column("input_rejected_by_provider", sa.Boolean()),
    )
    rejections = sa.table(
        "provider_rejections",
        sa.column("id", sa.Uuid()),
        sa.column("episode_id", sa.Uuid()),
        sa.column("scene_id", sa.String()),
        sa.column("provider", sa.String()),
        sa.column("reservation_id", sa.Uuid()),
        sa.column("input_hash", sa.String()),
        sa.column("rejected_input", sa.String()),
        sa.column("source_media_sha256", sa.String()),
        sa.column("types", sa.Text()),
        sa.column("reason", sa.String()),
        sa.column("message", sa.String()),
        sa.column("http_status", sa.Integer()),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(
        sa.select(
            reservations.c.id,
            reservations.c.episode_id,
            reservations.c.scene_id,
            reservations.c.provider,
            reservations.c.input_hash,
            reservations.c.error_summary,
            reservations.c.reconciled_at,
        ).where(
            reservations.c.status == "spent",
            reservations.c.error_summary.like("ProviderRejectedError:%"),
        )
    ).all()
    for row in rows:
        bind.execute(
            sa.update(reservations)
            .where(reservations.c.id == row.id)
            .values(input_rejected_by_provider=True)
        )
        values = classify_legacy_rejection(row.error_summary)
        insert_values: dict[str, object] = {
            "id": uuid.uuid4(),
            "episode_id": row.episode_id,
            "scene_id": row.scene_id,
            "provider": row.provider,
            "reservation_id": row.id,
            "input_hash": row.input_hash,
            "source_media_sha256": None,
            **values,
        }
        if row.reconciled_at is not None:
            insert_values["occurred_at"] = row.reconciled_at
        bind.execute(sa.insert(rejections).values(**insert_values))


def upgrade() -> None:
    op.add_column(
        "artifact_metadata", sa.Column("content_fingerprint", sa.String(length=64), nullable=True)
    )
    op.create_table(
        "provider_rejections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scene_id", sa.String(length=16), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "reservation_id",
            sa.Uuid(),
            sa.ForeignKey("provider_reservations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("rejected_input", sa.String(length=16), nullable=False),
        sa.Column("source_media_sha256", sa.String(length=64), nullable=True),
        sa.Column("types", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"provider IN ({_in(PROVIDER_CALLS)})", name="ck_provider_rejections_provider"
        ),
        sa.CheckConstraint(
            f"rejected_input IN ({_in(REJECTED_INPUTS)})",
            name="ck_provider_rejections_rejected_input",
        ),
    )
    op.create_index(
        "ix_provider_rejections_episode_scene", "provider_rejections", ["episode_id", "scene_id"]
    )
    op.create_index(
        "ix_provider_rejections_source_media",
        "provider_rejections",
        ["provider", "source_media_sha256"],
    )
    _apply(UPGRADED_CHECKS)
    _backfill_legacy_rejections()


def downgrade() -> None:
    """新しい語彙の行が残っていれば CHECK の作成で失敗する（行は触らない）。"""
    op.drop_index("ix_provider_rejections_source_media", table_name="provider_rejections")
    op.drop_index("ix_provider_rejections_episode_scene", table_name="provider_rejections")
    op.drop_table("provider_rejections")
    _apply(DOWNGRADED_CHECKS, drop_fingerprint=True)
