"""operational_anomalies.kind に日次 workflow 失敗の2種を許す（ADR-0054 / ADR-0060）

**CHECK 語彙の拡張**（旧集合 ⊂ 新集合）。列・表は増えず、旧コードが書く値は全て通り続ける。
PostgreSQL では drop と create が同じトランザクションに入るので、
制約が無い瞬間は外から見えない。語彙は凍結値（0012 の ``for k in AnomalyKind`` は将来の kind 追加で
履歴が変わるため、この migration は一覧を直書きする）。

``CHECK_WIDENING`` は release preflight（``migration_compat``）への宣言。走査は、この宣言と
``upgrade()`` の 2 文が一致し、旧集合が新集合の真の部分集合のときに限り、この drop を contract と
数えない（ADR-0060 / INV-82）。値を足すときは宣言と create の文字列の **両方** を直す
（食い違えば BLOCKED）。

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

CHECK_WIDENING = {
    "table": "operational_anomalies",
    "constraint": "ck_operational_anomalies_kind",
    "column": "kind",
    "old": (
        "DAILY_AUTOMATION_NOT_STARTED",
        "SCHEDULE_PAUSED_UNEXPECTEDLY",
        "SCHEDULE_MAINTENANCE_OVERRUN",
        "SCHEDULE_MISSING",
        "SCHEDULE_NEXT_RUN_INVALID",
        "EPISODE_STAGE_STALLED",
        "EPISODE_NOT_COMPLETED_BY_DEADLINE",
        "EPISODE_NOT_UPLOADED_BY_DEADLINE",
        "PIPELINE_OUTCOME_MISMATCH",
    ),
    "new": (
        "DAILY_AUTOMATION_NOT_STARTED",
        "SCHEDULE_PAUSED_UNEXPECTEDLY",
        "SCHEDULE_MAINTENANCE_OVERRUN",
        "SCHEDULE_MISSING",
        "SCHEDULE_NEXT_RUN_INVALID",
        "EPISODE_STAGE_STALLED",
        "EPISODE_NOT_COMPLETED_BY_DEADLINE",
        "EPISODE_NOT_UPLOADED_BY_DEADLINE",
        "PIPELINE_OUTCOME_MISMATCH",
        "DAILY_WORKFLOW_FAILED",
        "DAILY_WORKFLOW_UNOBSERVABLE",
    ),
}
_ADDED_KINDS = ("DAILY_WORKFLOW_FAILED", "DAILY_WORKFLOW_UNOBSERVABLE")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("operational_anomalies") as batch:
        batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
        batch.create_check_constraint(
            "ck_operational_anomalies_kind",
            "kind IN ('DAILY_AUTOMATION_NOT_STARTED', 'SCHEDULE_PAUSED_UNEXPECTEDLY', "
            "'SCHEDULE_MAINTENANCE_OVERRUN', 'SCHEDULE_MISSING', 'SCHEDULE_NEXT_RUN_INVALID', "
            "'EPISODE_STAGE_STALLED', 'EPISODE_NOT_COMPLETED_BY_DEADLINE', "
            "'EPISODE_NOT_UPLOADED_BY_DEADLINE', 'PIPELINE_OUTCOME_MISMATCH', "
            "'DAILY_WORKFLOW_FAILED', 'DAILY_WORKFLOW_UNOBSERVABLE')",
        )


def downgrade() -> None:
    # 旧 CHECK は新しい kind を表現できない。かといって監査行を黙って消さない（0018 と同じ方針）:
    # 新 kind の行が 1 つでもあれば何も変えずに拒否する。
    # 消してよいと判断した所有者が、行を退避・削除してから再実行する
    bind = op.get_bind()
    remaining = bind.execute(
        sa.text(f"SELECT count(*) FROM operational_anomalies WHERE kind IN ({_in(_ADDED_KINDS)})")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"refusing to downgrade 0017: {remaining} operational_anomalies row(s) of kind "
            f"{', '.join(_ADDED_KINDS)} would be lost (the old CHECK cannot hold them). "
            "Export or delete them deliberately, then run the downgrade again (ADR-0060)"
        )
    old: tuple[str, ...] = CHECK_WIDENING["old"]  # type: ignore[assignment]
    with op.batch_alter_table("operational_anomalies") as batch:
        batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
        batch.create_check_constraint("ck_operational_anomalies_kind", f"kind IN ({_in(old)})")
