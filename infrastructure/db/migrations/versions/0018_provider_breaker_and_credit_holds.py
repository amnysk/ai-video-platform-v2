"""fal 残高不足の breaker と保留（ADR-0057）: 新しい表を2つ足すだけ（expand-only）

- ``provider_breakers``: provider アカウント単位の circuit breaker（1アカウント1行）
- ``provider_credit_holds``: 保留した Episode / シーンの理由と、その時点の写し

既存の表・制約には触れない（``provider_reservations`` の CHECK も広げない。release gate は
制約の drop を止めるため。0017 の未解決問題を増やさない）。語彙は literal で凍結する。

downgrade は行が残っていれば何も変えずに拒否する（保留・breaker の監査記録を黙って消さない）。

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None

BREAKERS = "provider_breakers"
HOLDS = "provider_credit_holds"
#: ADR-0057 の語彙。**編集しない**（履歴の凍結値）。
BREAKER_STATES: tuple[str, ...] = ("closed", "open", "half_open")
REFUSAL_KINDS: tuple[str, ...] = (
    "balance_exhausted",
    "auth_invalid",
    "permission_denied",
    "unknown",
)
HOLD_REASONS: tuple[str, ...] = ("waiting_for_credits", "provider_refusal_unknown")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        BREAKERS,
        sa.Column("account_key", sa.String(length=64), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.String(length=400), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "opened_by_episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("refusal_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_refusal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("approval_fingerprint", sa.String(length=16), nullable=True),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approval_evidence", sa.Text(), nullable=True),
        sa.CheckConstraint(f"state IN ({_in(BREAKER_STATES)})", name="ck_provider_breakers_state"),
        sa.CheckConstraint(f"reason IN ({_in(REFUSAL_KINDS)})", name="ck_provider_breakers_reason"),
        sa.CheckConstraint(
            "state = 'closed' OR reason IS NOT NULL", name="ck_provider_breakers_reason_required"
        ),
    )
    op.create_table(
        HOLDS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "episode_id",
            sa.Uuid(),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scene_id", sa.String(length=16), nullable=True),
        sa.Column("scene_key", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("account_key", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("resumable_stage", sa.String(length=32), nullable=False),
        sa.Column("generation_profile_id", sa.String(length=128), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "held_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_approval_fingerprint", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            f"reason IN ({_in(HOLD_REASONS)})", name="ck_provider_credit_holds_reason"
        ),
    )
    op.create_index("ix_provider_credit_holds_episode_id", HOLDS, ["episode_id"])
    op.create_index(
        "uq_provider_credit_holds_open",
        HOLDS,
        ["episode_id", "scene_key", "account_key"],
        unique=True,
        sqlite_where=sa.text("released_at IS NULL"),
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (HOLDS, BREAKERS):
        remaining = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if remaining:
            raise RuntimeError(
                f"refusing to drop {table}: {remaining} audit row(s) would be lost (ADR-0057)"
            )
    op.drop_index("uq_provider_credit_holds_open", table_name=HOLDS)
    op.drop_index("ix_provider_credit_holds_episode_id", table_name=HOLDS)
    op.drop_table(HOLDS)
    op.drop_table(BREAKERS)
