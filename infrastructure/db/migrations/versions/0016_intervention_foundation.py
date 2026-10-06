"""判断記録と介入の土台（JP-MAS-001 v2.0 / ADR-0041）: **新しい表だけ**を足す

1. ``episode_controls``: Episode の制御軸（mode・control_state・制御版・採用 manifest・待機地点）。
   行の無い Episode は legacy / running として読むので、既存 Episode の backfill はしない。
2. ``artifact_manifests``: 採用 manifest（active_revision_set の履歴）。
   ``UNIQUE(episode_id, seq)``。
3. ``revision_links``: revision の依存（上流 → 下流、kind）。``artifact_metadata`` を FK で指す。
4. ``agent_runs``: 役割実行・ContextSnapshot・tool 参照。本文は MinIO、ここは参照と sha256。
5. ``decision_records``: 判断記録（追記型）。
6. ``annotations``: コメント・レビュー所見・ユーザー制約・承認・解決（追記型）。
7. ``change_sets``: 変更案（Variant を含む）。
8. ``intervention_commands``: command（行そのものが outbox）。
9. ``episode_events``: 判断・介入イベントの正本（``UNIQUE(episode_id, seq)``）。

既存の表は**変えない**（列も CHECK も足さない）。語彙は upgrade 時の値を **literal で凍結**する
（contracts を import しない。照合: ``tests/contract/test_migration_0016_intervention.py``）。

downgrade は新しい表に1行でも残っていれば**何も変えずに拒否する**
（判断・介入の記録を黙って消さない）。

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None

#: 作る順（downgrade は逆順で落とす）。
INTERVENTION_TABLES: tuple[str, ...] = (
    "episode_controls",
    "artifact_manifests",
    "revision_links",
    "agent_runs",
    "decision_records",
    "annotations",
    "change_sets",
    "intervention_commands",
    "episode_events",
)


#: 語彙の凍結値。**編集しない**（値を足すときは新しい migration で張り替える）。
EPISODE_CONTROLS_CONTROL_STATE_VALUES: tuple[str, ...] = (
    "running",
    "hold_requested",
    "held",
    "waiting_review",
    "cancel_requested",
)
EPISODE_CONTROLS_MODE_VALUES: tuple[str, ...] = (
    "legacy",
    "auto_intervenable",
    "review_points",
    "co_production",
)
WAITING_POINTS: tuple[str, ...] = (
    "before_storyboard",
    "before_production",
    "before_render",
    "before_upload",
)
ARTIFACT_MANIFESTS_ACTOR_TYPE_VALUES: tuple[str, ...] = (
    "human",
    "ai_assistant",
    "agent",
    "system",
)
REVISION_LINKS_KIND_VALUES: tuple[str, ...] = (
    "parent",
    "derived_from",
    "rebound_to",
    "evidence",
)
AGENT_RUNS_OUTCOME_VALUES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "rejected_output",
)
AGENT_RUNS_ROLE_VALUES: tuple[str, ...] = (
    "producer",
    "writer_director",
    "reviewer",
)
DECISION_RECORDS_ACTOR_TYPE_VALUES: tuple[str, ...] = (
    "human",
    "ai_assistant",
    "agent",
    "system",
)
ANNOTATIONS_ACTOR_TYPE_VALUES: tuple[str, ...] = (
    "human",
    "ai_assistant",
    "agent",
    "system",
)
ANNOTATIONS_KIND_VALUES: tuple[str, ...] = (
    "comment",
    "review_finding",
    "user_constraint",
    "approval",
    "resolution",
)
CHANGE_SETS_ACTOR_TYPE_VALUES: tuple[str, ...] = (
    "human",
    "ai_assistant",
    "agent",
    "system",
)
CHANGE_SETS_KIND_VALUES: tuple[str, ...] = (
    "script.edit_scenes",
    "scene.override_visual",
)
CHANGE_SETS_STATUS_VALUES: tuple[str, ...] = (
    "proposed",
    "applied",
    "rejected",
    "conflicted",
    "not_selected",
)
INTERVENTION_COMMANDS_ACTOR_TYPE_VALUES: tuple[str, ...] = (
    "human",
    "ai_assistant",
    "agent",
    "system",
)
INTERVENTION_COMMANDS_GRANTED_SCOPE_VALUES: tuple[str, ...] = (
    "read",
    "comment",
    "propose",
    "hold",
    "apply",
    "resume",
    "set_mode",
)
REJECT_REASONS: tuple[str, ...] = (
    "unsupported_command",
    "unsupported_change",
    "forbidden",
    "stale_control_revision",
    "stale_target_revision",
    "invalid_state",
    "user_constraint_violation",
    "budget_exceeded",
    "budget_unverifiable",
    "invalid_payload",
    "not_found",
    "internal_error",
)
INTERVENTION_COMMANDS_STATUS_VALUES: tuple[str, ...] = (
    "received",
    "validated",
    "applying",
    "applied",
    "conflict",
    "rejected",
    "failed",
)
INTERVENTION_COMMANDS_TYPE_VALUES: tuple[str, ...] = (
    "comment",
    "request_hold",
    "propose_change",
    "apply_change",
    "reject_change",
    "resume",
    "release_checkpoint",
    "set_user_constraint",
    "set_mode",
    "request_cancel",
)
EPISODE_EVENTS_ACTOR_TYPE_VALUES: tuple[str, ...] = (
    "human",
    "ai_assistant",
    "agent",
    "system",
)
EPISODE_EVENTS_EVENT_NAME_VALUES: tuple[str, ...] = (
    "decision.recorded",
    "agent.run.recorded",
    "intervention.command.received",
    "intervention.command.finished",
    "intervention.comment.added",
    "intervention.hold.requested",
    "intervention.hold.effective",
    "intervention.change.proposed",
    "intervention.change.applied",
    "intervention.change.conflicted",
    "intervention.variant.selected",
    "review.invalidated",
    "review.resolved",
    "checkpoint.waiting",
    "checkpoint.released",
    "intervention.resume.requested",
    "intervention.resume.completed",
    "intervention.mode.changed",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "episode_controls",
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("control_state", sa.String(length=32), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("review_points", sa.JSON(), nullable=False),
        sa.Column("active_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("waiting_point", sa.String(length=32), nullable=True),
        sa.Column("waiting_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("last_event_seq", sa.Integer(), nullable=False),
        sa.Column("hold_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "control_revision >= 0", name="ck_episode_controls_control_revision_non_negative"
        ),
        sa.CheckConstraint(
            f"control_state IN ({_in(EPISODE_CONTROLS_CONTROL_STATE_VALUES)})",
            name="ck_episode_controls_control_state",
        ),
        sa.CheckConstraint("last_event_seq >= 0", name="ck_episode_controls_last_event_seq"),
        sa.CheckConstraint(
            f"mode IN ({_in(EPISODE_CONTROLS_MODE_VALUES)})",
            name="ck_episode_controls_mode",
        ),
        sa.CheckConstraint(
            f"waiting_point IS NULL OR waiting_point IN ({_in(WAITING_POINTS)})",
            name="ck_episode_controls_waiting_point",
        ),
    )
    op.create_table(
        "artifact_manifests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("parent_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("members", sa.JSON(), nullable=False),
        sa.Column("causation_command_id", sa.String(length=64), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"actor_type IN ({_in(ARTIFACT_MANIFESTS_ACTOR_TYPE_VALUES)})",
            name="ck_artifact_manifests_actor_type",
        ),
        sa.CheckConstraint("seq >= 1", name="ck_artifact_manifests_seq_positive"),
        sa.UniqueConstraint("episode_id", "seq", name="uq_artifact_manifests_episode_seq"),
    )
    op.create_table(
        "revision_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "upstream_revision_id",
            sa.Uuid(),
            sa.ForeignKey("artifact_metadata.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "downstream_revision_id",
            sa.Uuid(),
            sa.ForeignKey("artifact_metadata.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("causation_command_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"kind IN ({_in(REVISION_LINKS_KIND_VALUES)})",
            name="ck_revision_links_kind",
        ),
        sa.UniqueConstraint(
            "upstream_revision_id", "downstream_revision_id", "kind", name="uq_revision_links_edge"
        ),
    )
    op.create_index("ix_revision_links_downstream", "revision_links", ["downstream_revision_id"])
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "topic_plan_id",
            sa.Uuid(),
            sa.ForeignKey("topic_plans.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=128), nullable=True),
        sa.Column("snapshot_object_key", sa.String(length=512), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_object_key", sa.String(length=512), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("response_object_key", sa.String(length=512), nullable=True),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("tool_invocations", sa.JSON(), nullable=False),
        sa.Column("token_usage", sa.JSON(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 4), nullable=True),
        sa.Column("output_revision_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("error_summary", sa.String(length=1000), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"outcome IN ({_in(AGENT_RUNS_OUTCOME_VALUES)})",
            name="ck_agent_runs_outcome",
        ),
        sa.CheckConstraint(
            f"role IN ({_in(AGENT_RUNS_ROLE_VALUES)})",
            name="ck_agent_runs_role",
        ),
        sa.CheckConstraint(
            "episode_id IS NOT NULL OR topic_plan_id IS NOT NULL", name="ck_agent_runs_scope"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_runs_idempotency_key"),
    )
    op.create_index("ix_agent_runs_episode", "agent_runs", ["episode_id", "created_at"])
    op.create_table(
        "decision_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "topic_plan_id",
            sa.Uuid(),
            sa.ForeignKey("topic_plans.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "agent_run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result_revision_id", sa.Uuid(), nullable=True),
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"actor_type IN ({_in(DECISION_RECORDS_ACTOR_TYPE_VALUES)})",
            name="ck_decision_records_actor_type",
        ),
        sa.CheckConstraint(
            "episode_id IS NOT NULL OR topic_plan_id IS NOT NULL", name="ck_decision_records_scope"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_decision_records_idempotency_key"),
    )
    op.create_index("ix_decision_records_episode", "decision_records", ["episode_id", "created_at"])
    op.create_table(
        "annotations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("anchor", sa.JSON(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "refers_to_annotation_id",
            sa.Uuid(),
            sa.ForeignKey("annotations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("manifest_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"actor_type IN ({_in(ANNOTATIONS_ACTOR_TYPE_VALUES)})",
            name="ck_annotations_actor_type",
        ),
        sa.CheckConstraint(
            f"kind IN ({_in(ANNOTATIONS_KIND_VALUES)})",
            name="ck_annotations_kind",
        ),
        sa.UniqueConstraint("command_id", name="uq_annotations_command_id"),
    )
    op.create_index("ix_annotations_episode", "annotations", ["episode_id", "created_at"])
    op.create_table(
        "change_sets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("base_revision_id", sa.Uuid(), nullable=False),
        sa.Column("base_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("impact", sa.JSON(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 4), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("variant_group", sa.String(length=64), nullable=True),
        sa.Column("applied_revision_id", sa.Uuid(), nullable=True),
        sa.Column("applied_manifest_id", sa.Uuid(), nullable=True),
        sa.Column("proposed_by_command_id", sa.String(length=64), nullable=False),
        sa.Column("decided_by_command_id", sa.String(length=64), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"actor_type IN ({_in(CHANGE_SETS_ACTOR_TYPE_VALUES)})",
            name="ck_change_sets_actor_type",
        ),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_change_sets_cost_non_negative",
        ),
        sa.CheckConstraint(
            f"kind IN ({_in(CHANGE_SETS_KIND_VALUES)})",
            name="ck_change_sets_kind",
        ),
        sa.CheckConstraint(
            f"status IN ({_in(CHANGE_SETS_STATUS_VALUES)})",
            name="ck_change_sets_status",
        ),
        sa.UniqueConstraint("proposed_by_command_id", name="uq_change_sets_command_id"),
    )
    op.create_index("ix_change_sets_episode", "change_sets", ["episode_id", "created_at"])
    op.create_table(
        "intervention_commands",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("granted_scope", sa.String(length=32), nullable=False),
        sa.Column("executor", sa.String(length=64), nullable=True),
        sa.Column("expected_control_revision", sa.Integer(), nullable=True),
        sa.Column("expected_target_revision", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("reject_reason", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"actor_type IN ({_in(INTERVENTION_COMMANDS_ACTOR_TYPE_VALUES)})",
            name="ck_intervention_commands_actor_type",
        ),
        sa.CheckConstraint(
            "dispatch_attempts >= 0", name="ck_intervention_commands_dispatch_attempts"
        ),
        sa.CheckConstraint(
            f"granted_scope IN ({_in(INTERVENTION_COMMANDS_GRANTED_SCOPE_VALUES)})",
            name="ck_intervention_commands_granted_scope",
        ),
        sa.CheckConstraint(
            f"reject_reason IS NULL OR reject_reason IN ({_in(REJECT_REASONS)})",
            name="ck_intervention_commands_reject_reason",
        ),
        sa.CheckConstraint(
            f"status IN ({_in(INTERVENTION_COMMANDS_STATUS_VALUES)})",
            name="ck_intervention_commands_status",
        ),
        sa.CheckConstraint(
            f"type IN ({_in(INTERVENTION_COMMANDS_TYPE_VALUES)})",
            name="ck_intervention_commands_type",
        ),
    )
    op.create_index(
        "ix_intervention_commands_episode", "intervention_commands", ["episode_id", "received_at"]
    )
    op.create_index(
        "ix_intervention_commands_status", "intervention_commands", ["status", "received_at"]
    )
    op.create_table(
        "episode_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_name", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"actor_type IN ({_in(EPISODE_EVENTS_ACTOR_TYPE_VALUES)})",
            name="ck_episode_events_actor_type",
        ),
        sa.CheckConstraint(
            f"event_name IN ({_in(EPISODE_EVENTS_EVENT_NAME_VALUES)})",
            name="ck_episode_events_event_name",
        ),
        sa.CheckConstraint("seq >= 1", name="ck_episode_events_seq_positive"),
        sa.UniqueConstraint("episode_id", "seq", name="uq_episode_events_episode_seq"),
    )
    op.create_index("ix_episode_events_unprojected", "episode_events", ["projected_at"])


def _refuse_if_rows_exist() -> None:
    bind = op.get_bind()
    counts = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in INTERVENTION_TABLES
    }
    remaining = {table: count for table, count in counts.items() if count}
    if remaining:
        raise RuntimeError(
            "refusing to downgrade 0016: intervention rows still exist "
            f"({remaining}); export them deliberately first"
        )


def downgrade() -> None:
    """記録が残っていれば拒否する（何も変えない）。空なら表を落とす。"""
    _refuse_if_rows_exist()
    for table in reversed(INTERVENTION_TABLES):
        op.drop_table(table)
