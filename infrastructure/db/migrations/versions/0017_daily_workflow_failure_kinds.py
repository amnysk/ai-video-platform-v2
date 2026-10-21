"""operational_anomalies.kind に日次 workflow 失敗の2種を許す（ADR-0054）

expand-only: 列・表は増えず、``kind`` の CHECK 制約に ``DAILY_WORKFLOW_FAILED`` と
``DAILY_WORKFLOW_UNOBSERVABLE`` を足すだけ。語彙は凍結値（0012 の ``for k in AnomalyKind`` は
将来の kind 追加で履歴が変わるため、この migration は一覧を直書きする）。

SQLite は制約の ALTER ができないので ``batch_alter_table`` を使う（0012 と同じ理由）。

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None

_PRIOR_KINDS = (
    "DAILY_AUTOMATION_NOT_STARTED",
    "SCHEDULE_PAUSED_UNEXPECTEDLY",
    "SCHEDULE_MAINTENANCE_OVERRUN",
    "SCHEDULE_MISSING",
    "SCHEDULE_NEXT_RUN_INVALID",
    "EPISODE_STAGE_STALLED",
    "EPISODE_NOT_COMPLETED_BY_DEADLINE",
    "EPISODE_NOT_UPLOADED_BY_DEADLINE",
    "PIPELINE_OUTCOME_MISMATCH",
)
_NEW_KINDS = ("DAILY_WORKFLOW_FAILED", "DAILY_WORKFLOW_UNOBSERVABLE")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _set_check(allowed: tuple[str, ...]) -> None:
    with op.batch_alter_table("operational_anomalies", schema=None) as batch:
        batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
        batch.create_check_constraint("ck_operational_anomalies_kind", f"kind IN ({_in(allowed)})")


def upgrade() -> None:
    _set_check(_PRIOR_KINDS + _NEW_KINDS)


def downgrade() -> None:
    # 旧 CHECK は新しい kind を表現できない。先に消さないと downgrade 自体が CHECK 違反で失敗する
    op.execute(sa.text(f"DELETE FROM operational_anomalies WHERE kind IN ({_in(_NEW_KINDS)})"))
    _set_check(_PRIOR_KINDS)
