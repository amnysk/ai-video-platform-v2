"""運用スイッチと日次 Episode 枠（ADR-0021）

- ``operational_switches``: DB の停止スイッチ。語彙（CHECK）は ``contracts.operations`` から導出
- ``daily_episode_slots``: 1日の Episode 本数上限。PK ``(slot_date, slot_index)`` と
  一意な ``trigger_id`` / ``episode_id``

既存テーブルの語彙は触らない。downgrade は2テーブルを落とす（行も消える。自動生成の枠と
スイッチは再作成できる運用状態であり、Episode 本体は残る）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from contracts.operations import OperationalSwitch

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    allowed = ", ".join(f"'{s.value}'" for s in OperationalSwitch)
    op.create_table(
        "operational_switches",
        sa.Column("name", sa.String(length=64), primary_key=True),
        sa.Column("is_on", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(f"name IN ({allowed})", name="ck_operational_switches_name"),
    )
    op.create_table(
        "daily_episode_slots",
        sa.Column("slot_date", sa.Date(), nullable=False),
        sa.Column("slot_index", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("trigger_id", sa.String(length=255), nullable=False),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("slot_date", "slot_index"),
        sa.CheckConstraint("slot_index >= 0", name="ck_daily_episode_slots_index_nonnegative"),
        sa.UniqueConstraint("trigger_id", name="uq_daily_episode_slots_trigger_id"),
        sa.UniqueConstraint("episode_id", name="uq_daily_episode_slots_episode_id"),
    )


def downgrade() -> None:
    op.drop_table("daily_episode_slots")
    op.drop_table("operational_switches")
