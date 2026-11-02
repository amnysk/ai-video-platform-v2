"""BGM カタログ・選曲・収集の監査/需要: 新しい表を 4 つ足すだけ（ADR-0087 §4）

- ``bgm_tracks``: ``BgmTrack`` の全列 + 承認の主体・規則の版・Content ID リスク・持ち上げ幅・
  使ってよいブランド・取得の来歴・実測（``analysis``）・推定（``inferred_tags``）。``file_sha256`` は UNIQUE
- ``bgm_selections``: Episode ごとの選曲の決定（``episode_id`` UNIQUE。再実行は既存の行を使う）
- ``bgm_acquisition_runs``: 収集の実行ごとの監査（追記のみ）
- ``bgm_acquisition_demands``: 収集の需要（適切な曲が無かった要件の hash が主キー = 冪等）。
  需要を ``runs`` の ``kind`` 列で持つ案もあったが、同じ hash を 1 行に保つ一意制約を素直に張れる
  別表にした（``runs`` は同じ需要で日に複数行になりうる）

どの表も他の表への外部キーを持たない（ADR-0077 の計画どおり、単独で DROP して戻せる）。既存の表・制約・
索引・列には触れない（旧コードはこれらの表を知らないまま動く）。JSON は PostgreSQL では JSONB。

**採番**: ``0020`` は他の枝（``claude/agent-tools-api`` の ``agent_tool_calls``、ADR-0081 追補の TRUNCATE 拒否）が
予約している。本 migration は ``down_revision = "0019"`` で、後から 0020 が入ったら**後に入る側**が
``down_revision`` を付け替える（単一 head は ``test_migration_single_head`` が固定する）。

downgrade は行が残っていれば何も変えずに拒否する（カタログ・選曲の監査を黙って消さない / ADR-0057 と同じ）。

Revision ID: 0021
Revises: 0019
Create Date: 2026-11-02
"""  # noqa: E501

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None

TRACKS = "bgm_tracks"
SELECTIONS = "bgm_selections"
RUNS = "bgm_acquisition_runs"
DEMANDS = "bgm_acquisition_demands"
TABLES = (DEMANDS, RUNS, SELECTIONS, TRACKS)


def _json() -> sa.types.TypeEngine:
    from sqlalchemy.dialects.postgresql import JSONB

    return sa.JSON().with_variant(JSONB(), "postgresql")


def _tz() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def _created_at() -> sa.Column:
    return sa.Column("created_at", _tz(), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        TRACKS,
        sa.Column("track_id", sa.String(length=128), primary_key=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("license_type", sa.String(length=48), nullable=False),
        sa.Column("license_status", sa.String(length=16), nullable=False),
        sa.Column("license_evidence_sha256", sa.String(length=64), nullable=True),
        sa.Column("license_checked_at", _tz(), nullable=True),
        sa.Column("allowed_platforms", _json(), nullable=False),
        sa.Column("allowed_formats", _json(), nullable=False),
        sa.Column("commercial_use_allowed", sa.Boolean(), nullable=False),
        sa.Column("reuse_allowed", sa.Boolean(), nullable=False),
        sa.Column("modification_allowed", sa.Boolean(), nullable=False),
        sa.Column("attribution_required", sa.Boolean(), nullable=False),
        sa.Column("exact_attribution_text", sa.Text(), nullable=True),
        sa.Column("valid_until", _tz(), nullable=True),
        sa.Column("mood_tags", _json(), nullable=False),
        sa.Column("genre_tags", _json(), nullable=False),
        sa.Column("energy", sa.String(length=16), nullable=False),
        sa.Column("bpm", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("loudness_lufs", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("license_approver_kind", sa.String(length=16), nullable=True),
        sa.Column("license_policy_version", sa.String(length=64), nullable=True),
        sa.Column(
            "content_id_risk", sa.String(length=16), nullable=False, server_default="unknown"
        ),
        sa.Column("mix_gain_offset_db", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allowed_brands", _json(), nullable=False),
        sa.Column("acquired_from", sa.String(length=64), nullable=False),
        sa.Column("acquired_at", _tz(), nullable=False),
        sa.Column("evidence_object_key", sa.Text(), nullable=True),
        sa.Column("analysis", _json(), nullable=False),
        sa.Column("inferred_tags", _json(), nullable=False),
        sa.Column("last_verified_at", _tz(), nullable=True),
        _created_at(),
        sa.UniqueConstraint("file_sha256", name="uq_bgm_tracks_file_sha256"),
        sa.CheckConstraint("length(file_sha256) = 64", name="ck_bgm_tracks_sha_length"),
        sa.CheckConstraint("duration_ms > 0", name="ck_bgm_tracks_duration"),
        sa.CheckConstraint(
            "mix_gain_offset_db >= -24 AND mix_gain_offset_db <= 12", name="ck_bgm_tracks_offset"
        ),
    )
    op.create_index("ix_bgm_tracks_status", TRACKS, ["status"])

    op.create_table(
        SELECTIONS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("track_id", sa.String(length=128), nullable=True),
        sa.Column("cue_sheet_sha256", sa.String(length=64), nullable=True),
        sa.Column("requirements_sha256", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("finalized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("decision", _json(), nullable=False),
        sa.Column("decided_at", _tz(), nullable=False),
        sa.Column("finalized_at", _tz(), nullable=True),
        _created_at(),
        sa.UniqueConstraint("episode_id", name="uq_bgm_selections_episode_id"),
        sa.CheckConstraint(
            "(finalized = true AND finalized_at IS NOT NULL) "
            "OR (finalized = false AND finalized_at IS NULL)",
            name="ck_bgm_selections_finalized",
        ),
    )
    op.create_index("ix_bgm_selections_finalized_at", SELECTIONS, ["finalized", "finalized_at"])

    op.create_table(
        RUNS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("demand_sha256", sa.String(length=64), nullable=False),
        sa.Column("started_at", _tz(), nullable=False),
        sa.Column("finished_at", _tz(), nullable=False),
        sa.Column("searches", sa.Integer(), nullable=False),
        sa.Column("candidates", _json(), nullable=False),
        _created_at(),
        sa.CheckConstraint("length(demand_sha256) = 64", name="ck_bgm_acquisition_runs_sha_length"),
    )
    op.create_index("ix_bgm_acquisition_runs_demand", RUNS, ["demand_sha256", "finished_at"])

    op.create_table(
        DEMANDS,
        sa.Column("demand_sha256", sa.String(length=64), primary_key=True),
        sa.Column("requirements", _json(), nullable=False),
        sa.Column("requested_at", _tz(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "length(demand_sha256) = 64", name="ck_bgm_acquisition_demands_sha_length"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        remaining = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if remaining:
            raise RuntimeError(
                f"refusing to drop {table}: {remaining} row(s) would be lost (ADR-0087)"
            )
    op.drop_index("ix_bgm_acquisition_runs_demand", table_name=RUNS)
    op.drop_index("ix_bgm_selections_finalized_at", table_name=SELECTIONS)
    op.drop_index("ix_bgm_tracks_status", table_name=TRACKS)
    for table in TABLES:
        op.drop_table(table)
