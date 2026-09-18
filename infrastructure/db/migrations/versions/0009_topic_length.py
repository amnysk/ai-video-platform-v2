"""episodes.topic の上限を契約（``contracts.topic.TOPIC_MAX_CHARS`` = 200）に揃える

0001 以来 ``String(500)`` だったが、ScriptArtifact / TopicCandidate は 200 字までしか許さない。
本番の最大長は 24 字（2026-09-19 確認）なので縮めても既存行は切れない。200 を超える行が
あれば PostgreSQL は ALTER を拒否し、migration は失敗する（黙って切り詰めない）。

長さはこの migration に凍結する（契約の定数が後で変わっても履歴は変わらない）。
SQLite は ALTER COLUMN できないので ``batch_alter_table`` を使う。

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None

_OLD_LENGTH = 500
_NEW_LENGTH = 200


def upgrade() -> None:
    with op.batch_alter_table("episodes", schema=None) as batch:
        batch.alter_column(
            "topic",
            existing_type=sa.String(_OLD_LENGTH),
            type_=sa.String(_NEW_LENGTH),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("episodes", schema=None) as batch:
        batch.alter_column(
            "topic",
            existing_type=sa.String(_NEW_LENGTH),
            type_=sa.String(_OLD_LENGTH),
            existing_nullable=True,
        )
