"""provider 認可拒否の記録（ADR-0030）

``provider_auth_incidents``: fal 等の provider への準備呼び出しが 401/403 を返すたびに1行。
``operational_anomalies``（1日1行）とは粒度が違う: 数分単位のバースト検出に使うため1件ずつ持つ。
``PaidJobRunner.submit`` は予約 INSERT の前にこのテーブルを読み、同じ provider の直近の未解決
件数が閾値を超えていれば新規 submit を止める（新規課金を抑止する）。

downgrade は表を落とす（監視の記録であり、Episode 等の業務データは残らない）。

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from contracts.states import ProviderCall

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    allowed = ", ".join(f"'{p.value}'" for p in ProviderCall)
    op.create_table(
        "provider_auth_incidents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"provider IN ({allowed})", name="ck_provider_auth_incidents_provider"),
    )
    op.create_index(
        "ix_provider_auth_incidents_provider_occurred",
        "provider_auth_incidents",
        ["provider", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_auth_incidents_provider_occurred", table_name="provider_auth_incidents"
    )
    op.drop_table("provider_auth_incidents")
