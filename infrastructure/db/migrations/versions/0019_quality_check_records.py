"""品質検査の記録・使用宣言・レポート・手動承認: 新しい表を 4 つ足すだけ（ADR-0081 / expand-only）

- ``quality_check_records``: 検査セット（対象 hash・入力 hash・規則版・世代に結び付く。追記専用）
- ``quality_usage_declarations``: render が書く BGM / 地図の使用・不使用の宣言（追記専用）
- ``quality_reports``: gate が組み立てた QualityReport（監査用。追記専用）
- ``quality_manual_approvals``: REVIEW を人が承認した記録（追記専用）

既存の表・制約・索引・列には触れない（旧コード e3c9555 はこれらの表を知らないまま動く）。
語彙は literal で凍結する（履歴の値。編集しない）。PostgreSQL では追記専用を UPDATE 拒否の
トリガ、DELETE 拒否のトリガで強制し、有効期間（24h 以内）の CHECK を足す
（SQLite は区間演算とトリガを使わない。テスト用の DB だけ）。

episode への外部キーは ``ON DELETE RESTRICT``（追記専用の監査記録を episode の削除で巻き込まない。
episode を削除するコードは無い。CASCADE のままでも DELETE 拒否のトリガに当たって失敗するだけなので、
意図を FK に書く）。承認の報告への外部キーは episode_id も含める
（別 episode の報告を承認できない）。

downgrade は行が残っていれば何も変えずに拒否する（監査記録を黙って消さない / ADR-0057 と同じ）。

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None

RECORDS = "quality_check_records"
USAGE = "quality_usage_declarations"
REPORTS = "quality_reports"
APPROVALS = "quality_manual_approvals"
TABLES = (APPROVALS, REPORTS, USAGE, RECORDS)
#: ADR-0081 の語彙。**編集しない**（履歴の凍結値）。
CHECK_KEYS: tuple[str, ...] = ("technical_qa", "description_lint", "bgm", "map")
USAGE_FEATURES: tuple[str, ...] = ("bgm", "map")
#: 判定語彙は ``contracts/verdict.py`` が唯一の定義。ここは凍結した写しで、literal の集合を
#: 書かない（INV-105 の検査器に引っかからない形）。一致は contract テストが固定する。
VERDICTS: tuple[str, ...] = ("pass", "warn") + ("review", "block")
APPROVER_KINDS: tuple[str, ...] = ("owner", "agent_delegated")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _episode_fk() -> sa.ForeignKey:
    return sa.ForeignKey("episodes.id", ondelete="RESTRICT")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        RECORDS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), _episode_fk(), nullable=False),
        sa.Column("final_video_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("check_key", sa.String(length=32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("producer", sa.String(length=64), nullable=False),
        sa.Column("producer_version", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=128), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            f"check_key IN ({_in(CHECK_KEYS)})", name="ck_quality_check_records_key"
        ),
        sa.CheckConstraint(
            f"verdict IN ({_in(VERDICTS)})", name="ck_quality_check_records_verdict"
        ),
        sa.CheckConstraint(
            "length(final_video_sha256) = 64 AND length(input_hash) = 64",
            name="ck_quality_check_records_hash_length",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_quality_check_records_generation"),
        sa.CheckConstraint("expires_at > checked_at", name="ck_quality_check_records_expiry"),
        sa.UniqueConstraint(
            "episode_id",
            "final_video_sha256",
            "rule_version",
            "check_key",
            "generation",
            name="uq_quality_check_records_subject_key",
        ),
    )
    op.create_index(
        "ix_quality_check_records_subject", RECORDS, ["episode_id", "final_video_sha256"]
    )

    op.create_table(
        USAGE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), _episode_fk(), nullable=False),
        sa.Column("final_video_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("feature", sa.String(length=16), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.Column("declared_by", sa.String(length=64), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ref", sa.String(length=128), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            f"feature IN ({_in(USAGE_FEATURES)})", name="ck_quality_usage_declarations_feature"
        ),
        sa.CheckConstraint(
            "length(final_video_sha256) = 64 AND length(input_hash) = 64",
            name="ck_quality_usage_declarations_hash_length",
        ),
        sa.CheckConstraint(
            "(used = true AND evidence_ref IS NOT NULL) OR (used = false AND evidence_ref IS NULL)",
            name="ck_quality_usage_declarations_evidence",
        ),
        sa.UniqueConstraint(
            "episode_id",
            "final_video_sha256",
            "feature",
            name="uq_quality_usage_declarations_subject_feature",
        ),
    )

    op.create_table(
        REPORTS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), _episode_fk(), nullable=False),
        sa.Column("final_video_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("generator_version", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint(f"verdict IN ({_in(VERDICTS)})", name="ck_quality_reports_verdict"),
        sa.CheckConstraint(
            "length(final_video_sha256) = 64 AND length(input_hash) = 64 "
            "AND length(report_sha256) = 64",
            name="ck_quality_reports_hash_length",
        ),
        sa.CheckConstraint("expires_at > generated_at", name="ck_quality_reports_expiry"),
        sa.UniqueConstraint(
            "id", "episode_id", "final_video_sha256", name="uq_quality_reports_id_episode_sha"
        ),
    )
    op.create_index("ix_quality_reports_subject", REPORTS, ["episode_id", "final_video_sha256"])

    op.create_table(
        APPROVALS,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), _episode_fk(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("final_video_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_verdict", sa.String(length=16), nullable=False),
        sa.Column("approver_kind", sa.String(length=32), nullable=False),
        sa.Column("approver_id", sa.String(length=128), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            f"approver_kind IN ({_in(APPROVER_KINDS)})", name="ck_quality_manual_approvals_kind"
        ),
        sa.CheckConstraint("length(approver_id) >= 1", name="ck_quality_manual_approvals_approver"),
        sa.CheckConstraint(
            "approved_verdict = 'review'", name="ck_quality_manual_approvals_verdict"
        ),
        sa.ForeignKeyConstraint(
            ["report_id", "episode_id", "final_video_sha256"],
            [
                "quality_reports.id",
                "quality_reports.episode_id",
                "quality_reports.final_video_sha256",
            ],
            name="fk_quality_manual_approvals_report",
        ),
    )
    op.create_index(
        "ix_quality_manual_approvals_subject", APPROVALS, ["episode_id", "final_video_sha256"]
    )

    if op.get_bind().dialect.name == "postgresql":
        _postgres_guards()


def _postgres_guards() -> None:
    """追記専用（UPDATE 拒否）と、有効期間 24h 以内の CHECK。PostgreSQL だけ。

    release 走査が読めるよう、文は全て literal にする（f-string にしない）。
    """
    op.execute(
        "CREATE FUNCTION quality_records_reject_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'quality records are append-only (ADR-0081)'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER quality_check_records_append_only BEFORE UPDATE ON quality_check_records "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    op.execute(
        "CREATE TRIGGER quality_usage_declarations_append_only "
        "BEFORE UPDATE ON quality_usage_declarations "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    op.execute(
        "CREATE TRIGGER quality_reports_append_only BEFORE UPDATE ON quality_reports "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    op.execute(
        "CREATE TRIGGER quality_manual_approvals_append_only "
        "BEFORE UPDATE ON quality_manual_approvals "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    # DELETE も拒否する（行ごと）。DROP TABLE は downgrade のため止めない。
    # TRUNCATE の拒否は 0020 に分ける（ADR-0081 追補: 走査器は TRUNCATE の語を破壊的と数え、
    # 承認する ADR が本番の revision に載るまで marker が効かない）
    op.execute(
        "CREATE TRIGGER quality_check_records_no_delete "
        "BEFORE DELETE ON quality_check_records "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    op.execute(
        "CREATE TRIGGER quality_usage_declarations_no_delete "
        "BEFORE DELETE ON quality_usage_declarations "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    op.execute(
        "CREATE TRIGGER quality_reports_no_delete "
        "BEFORE DELETE ON quality_reports "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    op.execute(
        "CREATE TRIGGER quality_manual_approvals_no_delete "
        "BEFORE DELETE ON quality_manual_approvals "
        "FOR EACH ROW EXECUTE FUNCTION quality_records_reject_update()"
    )
    # contracts.quality.MAX_REPORT_VALIDITY と同値のリテラル（一致は契約テストが固定する）
    op.execute(
        "ALTER TABLE quality_check_records ADD CONSTRAINT ck_quality_check_records_validity "
        "CHECK (expires_at - checked_at <= interval '24 hours')"
    )
    op.execute(
        "ALTER TABLE quality_reports ADD CONSTRAINT ck_quality_reports_validity "
        "CHECK (expires_at - generated_at <= interval '24 hours')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        remaining = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        if remaining:
            raise RuntimeError(
                f"refusing to drop {table}: {remaining} audit row(s) would be lost (ADR-0081)"
            )
    if bind.dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
            op.execute(f"DROP TRIGGER {table}_no_delete ON {table}")
        op.execute("DROP FUNCTION quality_records_reject_update()")
    for index, table in (
        ("ix_quality_manual_approvals_subject", APPROVALS),
        ("ix_quality_reports_subject", REPORTS),
        ("ix_quality_check_records_subject", RECORDS),
    ):
        op.drop_index(index, table_name=table)
    for table in TABLES:
        op.drop_table(table)
