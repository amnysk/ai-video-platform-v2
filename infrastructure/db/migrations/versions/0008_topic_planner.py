"""Topic Planner（ADR-0025）

- ``analytics_snapshots``: Analytics の取得結果。``(snapshot_date, provider)`` で一意
- ``topic_plans``: 1日・1 profile の組に1つの確定 Topic（``uq_topic_plans_day_profile``）
- ``topic_candidates``: 検討した候補（plan ごとに ``ordinal`` 一意、plan 削除で消える）
- ``episodes.topic_plan_id``: Episode の題材を決めた plan（NULL 可・一意）

``topic_plans.selected_candidate_id`` と ``topic_candidates.topic_plan_id`` は循環参照なので、
前者の外部キーは両テーブルを作った後に張る。SQLite は ALTER で制約を足せないので
``batch_alter_table`` を使う（downgrade も同じ）。

語彙（CHECK）は ``contracts.topic_planning`` から導出する（このテーブルは 0008 で生まれ、
downgrade は表ごと落とすので凍結した語彙を持つ必要が無い）。

downgrade は Planner の記録を消す（Episode 本体は残り、``topic`` 列の値も残る）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-18
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from alembic import op

from contracts.topic_planning import AnalyticsMode, DuplicateLevel, TopicPlanStatus

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None


def _check(column: str, enum_cls: type[Enum], name: str) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{m.value}'" for m in enum_cls)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=name)


def _timestamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "analytics_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        _timestamp("fetched_at"),
        sa.UniqueConstraint(
            "snapshot_date", "provider", name="uq_analytics_snapshots_day_provider"
        ),
    )
    op.create_table(
        "topic_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_date", sa.Date(), nullable=False),
        sa.Column("strategy_profile_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=32), nullable=False),
        sa.Column("content_profile_id", sa.String(length=64), nullable=False),
        sa.Column("content_profile_version", sa.String(length=32), nullable=False),
        sa.Column("selected_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("topic", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=80), nullable=False),
        sa.Column("angle", sa.String(length=32), nullable=False),
        sa.Column("era", sa.String(length=40), nullable=False),
        sa.Column("theme", sa.String(length=60), nullable=False),
        sa.Column("hook", sa.String(length=300), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_breakdown", sa.JSON(), nullable=False),
        sa.Column("duplicate_score", sa.Float(), nullable=False),
        sa.Column("duplicate_level", sa.String(length=16), nullable=False),
        sa.Column("analytics_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "analytics_snapshot_id",
            sa.Uuid(),
            sa.ForeignKey("analytics_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("analytics_confidence", sa.Float(), nullable=False),
        sa.Column("planner_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _check("status", TopicPlanStatus, "ck_topic_plans_status"),
        _check("analytics_mode", AnalyticsMode, "ck_topic_plans_analytics_mode"),
        _check("duplicate_level", DuplicateLevel, "ck_topic_plans_duplicate_level"),
        sa.UniqueConstraint(
            "plan_date",
            "strategy_profile_id",
            "content_profile_id",
            name="uq_topic_plans_day_profile",
        ),
    )
    op.create_table(
        "topic_candidates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "topic_plan_id",
            sa.Uuid(),
            sa.ForeignKey("topic_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(length=80), nullable=False),
        sa.Column("angle", sa.String(length=32), nullable=False),
        sa.Column("duplicate_level", sa.String(length=16), nullable=False),
        sa.Column("duplicate_score", sa.Float(), nullable=False),
        sa.Column("duplicate_of", sa.String(length=200), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("score_breakdown", sa.JSON(), nullable=True),
        sa.Column("rejected", sa.Boolean(), nullable=False),
        _timestamp("created_at"),
        _check("duplicate_level", DuplicateLevel, "ck_topic_candidates_duplicate_level"),
        sa.UniqueConstraint("topic_plan_id", "ordinal", name="uq_topic_candidates_plan_ordinal"),
    )
    with op.batch_alter_table("topic_plans", schema=None) as batch:
        batch.create_foreign_key(
            "fk_topic_plans_selected_candidate_id",
            "topic_candidates",
            ["selected_candidate_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("episodes", schema=None) as batch:
        batch.add_column(sa.Column("topic_plan_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_episodes_topic_plan_id", "topic_plans", ["topic_plan_id"], ["id"]
        )
        batch.create_unique_constraint("uq_episodes_topic_plan_id", ["topic_plan_id"])


def downgrade() -> None:
    with op.batch_alter_table("episodes", schema=None) as batch:
        batch.drop_constraint("uq_episodes_topic_plan_id", type_="unique")
        batch.drop_constraint("fk_episodes_topic_plan_id", type_="foreignkey")
        batch.drop_column("topic_plan_id")
    with op.batch_alter_table("topic_plans", schema=None) as batch:
        batch.drop_constraint("fk_topic_plans_selected_candidate_id", type_="foreignkey")
    op.drop_table("topic_candidates")
    op.drop_table("topic_plans")
    op.drop_table("analytics_snapshots")
