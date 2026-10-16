"""自律改善型制作の語彙を CHECK に足す（ADR-0043 §4、INV-54）。

**制約の張り替えだけ**。新しい表・列は作らない（内容と版は既存の ``artifact_metadata`` + MinIO）。

1. ``artifact_metadata.artifact_type``: ``user_brief`` / ``strategy_brief`` / ``creative_brief`` /
   ``quality_review`` / ``lesson`` / ``workflow_plan``（すべて Episode 単位。scene_id は NULL なので
   ``ck_artifact_metadata_scene_scope`` のシーン単位の集合は 0014 から変わらない。
   同じ式で張り直す）。
2. ``agent_runs.role``: ``strategy`` / ``creative_director`` / ``quality_reviewer`` / ``learning``。
3. ``provider_reservations.provider`` / ``provider_auth_incidents.provider`` /
   ``provider_rejections.provider``: ``agent_llm``（Episode 単位）。
   ``provider_rejections`` は ADR-0043 の
   一覧に無いが、models が ``ProviderCall`` から CHECK を導くので同じ語彙に揃える（実装時の訂正）。

語彙は literal で凍結する（contracts を import しない。照合:
``tests/contract/test_migration_0019_agentic_vocabulary.py``）。

downgrade は新しい語彙の行が1行でもあれば**何も変えずに拒否する**（0016 と同じ作法）。

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None

#: 0018 時点（9440302）の語彙。**編集しない**（履歴の凍結値）。
PRE_0019_ARTIFACT_TYPES: tuple[str, ...] = (
    "dummy",
    "script",
    "storyboard",
    "scene_image",
    "scene_voice",
    "scene_video",
    "production_manifest",
    "final_video",
    "upload_receipt",
    "scene_visual_override",
)
PRE_0019_PROVIDER_CALLS: tuple[str, ...] = (
    "codex_script",
    "codex_storyboard",
    "fal_image",
    "fal_video",
    "youtube_upload",
    "codex_scene_alternative",
)
PRE_0019_AGENT_ROLES: tuple[str, ...] = ("producer", "writer_director", "reviewer")
#: シーン単位の型・provider（0014 から変わらない）
SCENE_ARTIFACT_TYPES: tuple[str, ...] = (
    "scene_image",
    "scene_video",
    "scene_voice",
    "scene_visual_override",
)

#: 0019 が足す語彙。**編集しない**。
NEW_ARTIFACT_TYPES: tuple[str, ...] = (
    "user_brief",
    "strategy_brief",
    "creative_brief",
    "quality_review",
    "lesson",
    "workflow_plan",
)
NEW_PROVIDER_CALLS: tuple[str, ...] = ("agent_llm",)
NEW_AGENT_ROLES: tuple[str, ...] = (
    "strategy",
    "creative_director",
    "quality_reviewer",
    "learning",
)
ARTIFACT_TYPES: tuple[str, ...] = (*PRE_0019_ARTIFACT_TYPES, *NEW_ARTIFACT_TYPES)
PROVIDER_CALLS: tuple[str, ...] = (*PRE_0019_PROVIDER_CALLS, *NEW_PROVIDER_CALLS)
AGENT_ROLES: tuple[str, ...] = (*PRE_0019_AGENT_ROLES, *NEW_AGENT_ROLES)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _vocabulary_checks(
    artifacts: tuple[str, ...], providers: tuple[str, ...], roles: tuple[str, ...]
) -> tuple[tuple[str, str, str], ...]:
    """(table, constraint, 条件)。upgrade と downgrade で同じ集合の制約を張り替える。"""
    return (
        ("artifact_metadata", "ck_artifact_metadata_type", f"artifact_type IN ({_in(artifacts)})"),
        (
            "artifact_metadata",
            "ck_artifact_metadata_scene_scope",
            f"(artifact_type IN ({_in(SCENE_ARTIFACT_TYPES)}) AND scene_id IS NOT NULL) "
            f"OR (artifact_type NOT IN ({_in(SCENE_ARTIFACT_TYPES)}) AND scene_id IS NULL)",
        ),
        ("agent_runs", "ck_agent_runs_role", f"role IN ({_in(roles)})"),
        (
            "provider_reservations",
            "ck_provider_reservations_provider",
            f"provider IN ({_in(providers)})",
        ),
        (
            "provider_auth_incidents",
            "ck_provider_auth_incidents_provider",
            f"provider IN ({_in(providers)})",
        ),
        (
            "provider_rejections",
            "ck_provider_rejections_provider",
            f"provider IN ({_in(providers)})",
        ),
    )


UPGRADED_CHECKS = _vocabulary_checks(ARTIFACT_TYPES, PROVIDER_CALLS, AGENT_ROLES)
DOWNGRADED_CHECKS = _vocabulary_checks(
    PRE_0019_ARTIFACT_TYPES, PRE_0019_PROVIDER_CALLS, PRE_0019_AGENT_ROLES
)

#: 0004 が張った artifact_metadata の式索引（scene キー込み）。**編集しない**。
_SCENE_KEY = "coalesce(scene_id, '')"
_CURRENT_WHERE = "superseded_at IS NULL"
_ARTIFACT_INDEXES: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("uq_artifact_metadata_content", ("episode_id", "artifact_type", _SCENE_KEY, "sha256"), None),
    ("uq_artifact_metadata_version", ("episode_id", "artifact_type", _SCENE_KEY, "version"), None),
    ("uq_artifact_metadata_current", ("episode_id", "artifact_type", _SCENE_KEY), _CURRENT_WHERE),
)


def _apply(checks: tuple[tuple[str, str, str], ...]) -> None:
    # SQLite の batch（テーブル再作成）は式索引を黙って失うので、張り替えの前に落として作り直す
    for name, _columns, _where in _ARTIFACT_INDEXES:
        op.drop_index(name, table_name="artifact_metadata")
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


def _refuse_if_new_vocabulary_is_used() -> None:
    bind = op.get_bind()
    probes = (
        ("artifact_metadata", "artifact_type", NEW_ARTIFACT_TYPES),
        ("agent_runs", "role", NEW_AGENT_ROLES),
        ("provider_reservations", "provider", NEW_PROVIDER_CALLS),
        ("provider_auth_incidents", "provider", NEW_PROVIDER_CALLS),
        ("provider_rejections", "provider", NEW_PROVIDER_CALLS),
    )
    remaining: dict[str, int] = {}
    for table, column, values in probes:
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({_in(values)})")
        ).scalar_one()
        if count:
            remaining[f"{table}.{column}"] = count
    if remaining:
        raise RuntimeError(
            f"refusing to downgrade 0019: rows still use the agentic vocabulary ({remaining}); "
            "export them deliberately first"
        )


def upgrade() -> None:
    _apply(UPGRADED_CHECKS)


def downgrade() -> None:
    """新しい語彙の行があれば何も変えずに拒否する。無ければ 0018 の CHECK へ戻す。"""
    _refuse_if_new_vocabulary_is_used()
    _apply(DOWNGRADED_CHECKS)
