"""判断記録・実行記録に Scene と Temporal の識別子を足す（全て NULL 可）。

「判断が Episode・Scene・Workflow のどれに属するか」を行から辿れるようにする。

- ``agent_runs``: ``workflow_id`` / ``workflow_run_id`` / ``activity_id``（その実行を作った
  Activity の Temporal 識別子。Activity の外（test・CLI・Topic の staging）で作った行は NULL）
- ``decision_records``: ``scene_id``（判断が単一 Scene を対象にすると payload が明示するときだけ）、
  ``workflow_id`` / ``workflow_run_id``

既存行は書き換えない（NULL のまま。補完しない）。

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None

AGENT_RUN_COLUMNS: tuple[str, ...] = ("workflow_id", "workflow_run_id", "activity_id")
DECISION_COLUMNS: tuple[str, ...] = ("scene_id", "workflow_id", "workflow_run_id")
IX_DECISION_SCENE = "ix_decision_records_episode_scene"
IX_AGENT_RUN_WORKFLOW = "ix_agent_runs_workflow"


def upgrade() -> None:
    with op.batch_alter_table("agent_runs", schema=None) as batch:
        for name in AGENT_RUN_COLUMNS:
            batch.add_column(sa.Column(name, sa.String(length=255), nullable=True))
        batch.create_index(IX_AGENT_RUN_WORKFLOW, ["workflow_id"])
    with op.batch_alter_table("decision_records", schema=None) as batch:
        for name in DECISION_COLUMNS:
            batch.add_column(sa.Column(name, sa.String(length=255), nullable=True))
        batch.create_index(IX_DECISION_SCENE, ["episode_id", "scene_id"])


def downgrade() -> None:
    """列を落とす（記録した識別子は失われる。判断の本体は payload に残る）。"""
    with op.batch_alter_table("decision_records", schema=None) as batch:
        batch.drop_index(IX_DECISION_SCENE)
        for name in reversed(DECISION_COLUMNS):
            batch.drop_column(name)
    with op.batch_alter_table("agent_runs", schema=None) as batch:
        batch.drop_index(IX_AGENT_RUN_WORKFLOW)
        for name in reversed(AGENT_RUN_COLUMNS):
            batch.drop_column(name)
