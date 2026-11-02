"""operational_anomalies.kind に品質ゲートの保留率の警報を許す（ADR-0087 §11）

**CHECK 語彙の拡張**（旧集合 ⊂ 新集合。0017 と同じ方式）。列・表は増えず、旧コードが書く値は全て
通り続ける。語彙は凍結値（この migration は一覧を直書きする）。``CHECK_WIDENING`` は release
preflight への宣言で、宣言と ``upgrade()`` の 2 文が一致するときだけ contract と数えない
（ADR-0060 / INV-82）。

番号について: ADR-0087 §4 は 0020（agent-tools）と 0021（BGM カタログ）を他の枝に予約している。
この migration は **0022** を名乗り、この枝では 0019 の次に置く（``down_revision = "0019"``）。
他の枝が先に入ったら、後から入る側が ``down_revision`` を付け替える。

Revision ID: 0022
Revises: 0019
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0019"
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
        "DAILY_WORKFLOW_FAILED",
        "DAILY_WORKFLOW_UNOBSERVABLE",
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
        "QUALITY_GATE_HOLD_RATE_HIGH",
    ),
}
_ADDED_KINDS = ("QUALITY_GATE_HOLD_RATE_HIGH",)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table("operational_anomalies") as batch:
        batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
        batch.create_check_constraint(
            "ck_operational_anomalies_kind",
            "kind IN ('DAILY_AUTOMATION_NOT_STARTED', "
            "'SCHEDULE_PAUSED_UNEXPECTEDLY', 'SCHEDULE_MAINTENANCE_OVERRUN', "
            "'SCHEDULE_MISSING', 'SCHEDULE_NEXT_RUN_INVALID', "
            "'EPISODE_STAGE_STALLED', 'EPISODE_NOT_COMPLETED_BY_DEADLINE', "
            "'EPISODE_NOT_UPLOADED_BY_DEADLINE', 'PIPELINE_OUTCOME_MISMATCH', "
            "'DAILY_WORKFLOW_FAILED', 'DAILY_WORKFLOW_UNOBSERVABLE', "
            "'QUALITY_GATE_HOLD_RATE_HIGH')",
        )


def downgrade() -> None:
    # 旧 CHECK は新しい kind を表現できない。監査行を黙って消さない（0017 と同じ方針）:
    # 新 kind の行が 1 つでもあれば何も変えずに拒否する。消してよいと判断した所有者が
    # 行を退避・削除してから再実行する
    bind = op.get_bind()
    remaining = bind.execute(
        sa.text(f"SELECT count(*) FROM operational_anomalies WHERE kind IN ({_in(_ADDED_KINDS)})")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"refusing to downgrade 0022: {remaining} operational_anomalies row(s) of kind "
            f"{', '.join(_ADDED_KINDS)} would be lost (the old CHECK cannot hold them). "
            "Export or delete them deliberately, then run the downgrade again (ADR-0060)"
        )
    old: tuple[str, ...] = CHECK_WIDENING["old"]  # type: ignore[assignment]
    with op.batch_alter_table("operational_anomalies") as batch:
        batch.drop_constraint("ck_operational_anomalies_kind", type_="check")
        batch.create_check_constraint("ck_operational_anomalies_kind", f"kind IN ({_in(old)})")
