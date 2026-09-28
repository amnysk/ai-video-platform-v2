"""provider_reservations に input_rejected_by_provider を追加する（ADR-0034）

2026-09-26/27 の fal 422 content_policy_violation 事故: 拒否された入力のまま
``spent`` した予約は、次の submit で無条件に「最新ラウンド + 1」として新しい予約・
新しい provider 呼び出しへ進んでいた（ADR-0017 §3 の元の表）。同じ入力（プロンプト・
画像）を直さない限り同じ拒否を繰り返すだけで、無駄な再送・再課金リスクになる。

この列は「provider が入力そのものを拒否したために spent した」ことを機械的に示す。
``infrastructure.production.paid_job._plan_round`` はこの列が true の予約を見つけたら
自動でラウンドを進めず ``ProviderRejectedRetryBlockedError`` を送出する。

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_reservations", schema=None) as batch:
        batch.add_column(
            sa.Column(
                "input_rejected_by_provider",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("provider_reservations", schema=None) as batch:
        batch.drop_column("input_rejected_by_provider")
