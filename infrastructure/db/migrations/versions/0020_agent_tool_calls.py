"""Agent Tool の呼び出し表と語彙（ADR-0047 §6、INV-59）。

1. 新表 ``agent_tool_calls``（tool 呼び出し 1 回 = 1 行。``UNIQUE(agent_run_key, seq)``、
   PK ``tool_call_id``）。実行前に ``started`` で commit し、完了で結果を保存する。
2. ``artifact_metadata.artifact_type``: ``fact_check_report``（Episode 単位。scene_id は NULL なので
   ``ck_artifact_metadata_scene_scope`` のシーン単位の集合は変わらない。同じ式で張り直す）。
3. ``agent_runs.role``: ``historian``。

語彙は literal で凍結する（contracts を import しない。照合:
``tests/contract/test_migration_0020_agent_tool_calls.py``）。

downgrade は新しい語彙の行・``agent_tool_calls`` の行が 1 行でもあれば**何も変えずに拒否する**
（0016 / 0019 と同じ作法）。

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None

#: 0019 時点の語彙。**編集しない**（履歴の凍結値）。
PRE_0020_ARTIFACT_TYPES: tuple[str, ...] = (
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
    "user_brief",
    "strategy_brief",
    "creative_brief",
    "quality_review",
    "lesson",
    "workflow_plan",
)
PRE_0020_AGENT_ROLES: tuple[str, ...] = (
    "producer",
    "writer_director",
    "reviewer",
    "strategy",
    "creative_director",
    "quality_reviewer",
    "learning",
)
SCENE_ARTIFACT_TYPES: tuple[str, ...] = (
    "scene_image",
    "scene_video",
    "scene_voice",
    "scene_visual_override",
)

#: 0020 が足す語彙。**編集しない**。
NEW_ARTIFACT_TYPES: tuple[str, ...] = ("fact_check_report",)
NEW_AGENT_ROLES: tuple[str, ...] = ("historian",)
ARTIFACT_TYPES: tuple[str, ...] = (*PRE_0020_ARTIFACT_TYPES, *NEW_ARTIFACT_TYPES)
AGENT_ROLES: tuple[str, ...] = (*PRE_0020_AGENT_ROLES, *NEW_AGENT_ROLES)

#: ``agent_tool_calls.status`` の語彙（実行前の ``started`` + ToolStatus）。**編集しない**。
TOOL_CALL_STATUSES: tuple[str, ...] = (
    "started",
    "ok",
    "no_data",
    "insufficient_data",
    "withheld_by_threshold",
    "reporting_delay",
    "not_available_via_api",
    "permission_denied",
    "quota_exceeded",
    "rate_limited",
    "timeout",
    "pending",
    "provider_not_configured",
    "not_supported",
    "failed",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _vocabulary_checks(
    artifacts: tuple[str, ...], roles: tuple[str, ...]
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
    )


UPGRADED_CHECKS = _vocabulary_checks(ARTIFACT_TYPES, AGENT_ROLES)
DOWNGRADED_CHECKS = _vocabulary_checks(PRE_0020_ARTIFACT_TYPES, PRE_0020_AGENT_ROLES)

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
    # （0019 と同じ）
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


def _refuse_if_new_data_exists() -> None:
    bind = op.get_bind()
    probes = (
        ("artifact_metadata", "artifact_type", NEW_ARTIFACT_TYPES),
        ("agent_runs", "role", NEW_AGENT_ROLES),
    )
    remaining: dict[str, int] = {}
    for table, column, values in probes:
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({_in(values)})")
        ).scalar_one()
        if count:
            remaining[f"{table}.{column}"] = count
    calls = bind.execute(sa.text("SELECT COUNT(*) FROM agent_tool_calls")).scalar_one()
    if calls:
        remaining["agent_tool_calls"] = calls
    if remaining:
        raise RuntimeError(
            f"refusing to downgrade 0020: rows still use the agent tool vocabulary ({remaining}); "
            "export them deliberately first"
        )


def upgrade() -> None:
    op.create_table(
        "agent_tool_calls",
        sa.Column("tool_call_id", sa.String(length=36), primary_key=True),
        sa.Column("agent_run_key", sa.String(length=255), nullable=False),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("agent_role", sa.String(length=32), nullable=False),
        sa.Column("agent_spec_version", sa.String(length=16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_object_key", sa.String(length=512), nullable=True),
        sa.Column("result_sha256", sa.String(length=64), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("research_request_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            f"status IN ({_in(TOOL_CALL_STATUSES)})", name="ck_agent_tool_calls_status"
        ),
        sa.UniqueConstraint("agent_run_key", "seq", name="uq_agent_tool_calls_run_seq"),
    )
    op.create_index("ix_agent_tool_calls_episode", "agent_tool_calls", ["episode_id", "started_at"])
    _apply(UPGRADED_CHECKS)


def downgrade() -> None:
    """新しい語彙・tool 呼び出しの行があれば何も変えずに拒否する。無ければ 0019 の状態へ戻す。"""
    _refuse_if_new_data_exists()
    _apply(DOWNGRADED_CHECKS)
    op.drop_index("ix_agent_tool_calls_episode", table_name="agent_tool_calls")
    op.drop_table("agent_tool_calls")
