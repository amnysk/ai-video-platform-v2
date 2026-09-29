"""Research の永続化（ADR-0037）: 依頼・外部呼び出し台帳・成果物を**新しい表だけ**で持つ

1. ``research_requests``: 調査依頼 1 件（冪等キーで一意、状態は CHECK）。``episode_id`` は参照
   だけで FK を張らない（Episode の寿命を Research が縛らない。INV-37）。
2. ``research_calls``: 外部呼び出しの台帳。``UNIQUE(request_id, provider_call, call_seq)`` と
   ``call_seq >= 1`` によって、1 依頼・1 種別の行数を採番の上限より増やせない（INV-36）。
   ``reserved`` / ``spent`` / ``abandoned`` のすべてが枠を数え、行は消さない。
3. ``research_artifacts``: research 所有の成果物の世代。``(request_id, artifact_type)`` ごとに
   現行（``superseded_at IS NULL``）は1本（部分一意索引）。

本番の表（Episode の job・成果物・予約・拒否の記録）には触れない。語彙は upgrade・downgrade とも
**literal で凍結**する（contracts を import しない。照合:
``tests/contract/test_migration_0015_research.py``）。

downgrade は research の行が1行でも残っていれば**何も変えずに拒否する**（支出の記録を黙って
消さない）。空なら表を落とす。

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None

#: ADR-0037 の語彙。**編集しない**（履歴の凍結値。値を足すときは新しい migration で張り替える）。
RESEARCH_KINDS: tuple[str, ...] = ("trend", "evidence")
RESEARCH_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "completed",
    "partial",
    "blocked",
    "failed",
)
RESEARCH_CALLS: tuple[str, ...] = ("search", "fetch", "assess")
RESEARCH_CALL_STATUSES: tuple[str, ...] = ("reserved", "spent", "abandoned")
RESEARCH_ARTIFACT_TYPES: tuple[str, ...] = (
    "research_trend",
    "research_evidence",
    "research_script_verification",
)

#: 作る順（downgrade は逆順で落とす）。
RESEARCH_TABLES: tuple[str, ...] = ("research_requests", "research_calls", "research_artifacts")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "research_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requester", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("episode_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("provider_config_version", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_reason", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"kind IN ({_in(RESEARCH_KINDS)})", name="ck_research_requests_kind"),
        sa.CheckConstraint(
            f"status IN ({_in(RESEARCH_STATUSES)})", name="ck_research_requests_status"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_research_requests_idempotency_key"),
    )
    op.create_index("ix_research_requests_request_hash", "research_requests", ["request_hash"])
    op.create_index(
        "ix_research_requests_status_created_at", "research_requests", ["status", "created_at"]
    )
    op.create_index(
        "ix_research_requests_channel_kind", "research_requests", ["channel_id", "kind"]
    )

    op.create_table(
        "research_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("research_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_call", sa.String(length=16), nullable=False),
        sa.Column("call_seq", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 4), nullable=True),
        sa.Column("quota_units", sa.Integer(), nullable=True),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            f"provider_call IN ({_in(RESEARCH_CALLS)})", name="ck_research_calls_provider_call"
        ),
        sa.CheckConstraint(
            f"status IN ({_in(RESEARCH_CALL_STATUSES)})", name="ck_research_calls_status"
        ),
        sa.CheckConstraint("call_seq >= 1", name="ck_research_calls_call_seq_positive"),
        sa.CheckConstraint(
            "(status = 'reserved' AND settled_at IS NULL) "
            "OR (status <> 'reserved' AND settled_at IS NOT NULL)",
            name="ck_research_calls_settled",
        ),
        sa.CheckConstraint(
            "status <> 'abandoned' OR dispatched_at IS NULL",
            name="ck_research_calls_abandoned_not_dispatched",
        ),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_research_calls_cost_non_negative",
        ),
        sa.CheckConstraint(
            "quota_units IS NULL OR quota_units >= 0",
            name="ck_research_calls_quota_non_negative",
        ),
        sa.UniqueConstraint(
            "request_id", "provider_call", "call_seq", name="uq_research_calls_call_seq"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_research_calls_idempotency_key"),
    )
    op.create_index(
        "ix_research_calls_request_input",
        "research_calls",
        ["request_id", "provider_call", "input_hash"],
    )

    op.create_table(
        "research_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("research_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("bucket", sa.String(length=63), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"artifact_type IN ({_in(RESEARCH_ARTIFACT_TYPES)})",
            name="ck_research_artifacts_type",
        ),
        sa.CheckConstraint("version >= 1", name="ck_research_artifacts_version_positive"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_research_artifacts_size_non_negative"),
        sa.UniqueConstraint(
            "request_id", "artifact_type", "sha256", name="uq_research_artifacts_content"
        ),
        sa.UniqueConstraint(
            "request_id", "artifact_type", "version", name="uq_research_artifacts_version"
        ),
    )
    op.create_index(
        "uq_research_artifacts_current",
        "research_artifacts",
        ["request_id", "artifact_type"],
        unique=True,
        sqlite_where=sa.text("superseded_at IS NULL"),
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def _refuse_if_research_rows_exist() -> None:
    bind = op.get_bind()
    counts = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in RESEARCH_TABLES
    }
    remaining = {table: count for table, count in counts.items() if count}
    if remaining:
        raise RuntimeError(
            "refusing to downgrade 0015: research rows still exist "
            f"({remaining}); export or delete them deliberately first"
        )


def downgrade() -> None:
    """research の行が残っていれば拒否する（何も変えない）。空なら表を落とす。"""
    _refuse_if_research_rows_exist()
    op.drop_index("uq_research_artifacts_current", table_name="research_artifacts")
    op.drop_table("research_artifacts")
    op.drop_index("ix_research_calls_request_input", table_name="research_calls")
    op.drop_table("research_calls")
    op.drop_index("ix_research_requests_channel_kind", table_name="research_requests")
    op.drop_index("ix_research_requests_status_created_at", table_name="research_requests")
    op.drop_index("ix_research_requests_request_hash", table_name="research_requests")
    op.drop_table("research_requests")
