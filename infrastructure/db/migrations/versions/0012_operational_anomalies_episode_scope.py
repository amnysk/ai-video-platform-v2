"""operational_anomalies に Episode 単位の粒度を持たせる（ADR-0031）

``episode_id`` を追加し、``(kind, anomaly_date)`` 単一の一意制約を2本の部分インデックスに
分ける: ``episode_id IS NULL`` の Schedule 系は今まで通り1日1行、``episode_id IS NOT NULL`` の
Episode 系は Episode ごとに1日1行。CHECK 制約も新しい4種の AnomalyKind を含めて更新する。

SQLite は制約の ALTER ができないので ``batch_alter_table`` を使う（0009 と同じ理由）。

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from contracts.schedule_guard import AnomalyKind

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None

_PRIOR_KINDS = (
    "DAILY_AUTOMATION_NOT_STARTED",
    "SCHEDULE_PAUSED_UNEXPECTEDLY",
    "SCHEDULE_MAINTENANCE_OVERRUN",
    "SCHEDULE_MISSING",
    "SCHEDULE_NEXT_RUN_INVALID",
)


def upgrade() -> None:
    allowed = ", ".join(f"'{k.value}'" for k in AnomalyKind)
    with op.batch_alter_table("operational_anomalies", schema=None) as batch:
        batch.add_column(
            sa.Column(
                "episode_id",
                sa.Uuid(),
                sa.ForeignKey(
                    "episodes.id",
                    ondelete="SET NULL",
                    name="fk_operational_anomalies_episode_id",
                ),
                nullable=True,
            )
        )
        batch.drop_constraint("uq_operational_anomalies_kind_date", type_="unique")
        batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
        batch.create_check_constraint("ck_operational_anomalies_kind", f"kind IN ({allowed})")

    op.create_index(
        "uq_operational_anomalies_kind_date_schedule",
        "operational_anomalies",
        ["kind", "anomaly_date"],
        unique=True,
        postgresql_where=sa.text("episode_id IS NULL"),
        sqlite_where=sa.text("episode_id IS NULL"),
    )
    op.create_index(
        "uq_operational_anomalies_kind_date_episode",
        "operational_anomalies",
        ["kind", "anomaly_date", "episode_id"],
        unique=True,
        postgresql_where=sa.text("episode_id IS NOT NULL"),
        sqlite_where=sa.text("episode_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_operational_anomalies_kind_date_episode", table_name="operational_anomalies")
    op.drop_index("uq_operational_anomalies_kind_date_schedule", table_name="operational_anomalies")

    # 旧 CHECK/UNIQUE はこの migration が追加した4種の episode 単位の kind を表現できない。
    # 制約を戻す前に、それらの行を消しておかないと CHECK 違反で downgrade 自体が失敗する
    # （episode 単位の異常履歴は Phase 1 の Artifact と違い監視の記録であり、失っても業務データは
    # 残る。ADR-0030 の provider_auth_incidents と同じ「監視テーブルはdowngradeで表ごと消える」
    # 扱いと整合する）。
    op.execute(
        sa.text(
            "DELETE FROM operational_anomalies WHERE episode_id IS NOT NULL "
            "OR kind NOT IN (" + ", ".join(f"'{k}'" for k in _PRIOR_KINDS) + ")"
        )
    )

    # downgrade はこの migration 適用前に存在した5種だけを許す
    prior_allowed = ", ".join(f"'{k}'" for k in _PRIOR_KINDS)
    with op.batch_alter_table("operational_anomalies", schema=None) as batch:
        batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
        batch.create_check_constraint("ck_operational_anomalies_kind", f"kind IN ({prior_allowed})")
        batch.create_unique_constraint(
            "uq_operational_anomalies_kind_date", ["kind", "anomaly_date"]
        )
        batch.drop_column("episode_id")
