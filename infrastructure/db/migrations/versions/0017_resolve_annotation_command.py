"""resolve_annotation を command 型の CHECK に足す（ADR-0041 addendum）。

``intervention_commands.type`` の CHECK は 0016 が語彙を literal で凍結している。0016 は編集せず、
ここで ``resolve_annotation`` を足した全語彙に張り替える。表・列は変えない。

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_intervention_commands_type"
#: 0016 時点の語彙。**編集しない**（履歴の凍結値）。
PRE_0017_COMMAND_TYPES: tuple[str, ...] = (
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
COMMAND_TYPES: tuple[str, ...] = (*PRE_0017_COMMAND_TYPES, "resolve_annotation")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _replace(values: tuple[str, ...]) -> None:
    with op.batch_alter_table("intervention_commands", schema=None) as batch:
        batch.drop_constraint(CONSTRAINT, type_="check")
        batch.create_check_constraint(CONSTRAINT, sa.text(f"type IN ({_in(values)})"))


def upgrade() -> None:
    _replace(COMMAND_TYPES)


def downgrade() -> None:
    """新しい型の行が残っていれば CHECK の作成で失敗する（行は触らない）。"""
    _replace(PRE_0017_COMMAND_TYPES)
