"""migration の静的走査（INV-65）。best effort: 通過は「安全の証明」ではなく、人のレビューは必要。

各サンプルは、以前の走査をすり抜けた（または抜けうる）書き方。
"""

from __future__ import annotations

import textwrap

import pytest

from domain import release_preflight as rp
from tests.unit.test_release_preflight import (
    ACCEPTED_ADR,
    BASE_MIGRATION,
    OLD,
    ROOT_MIGRATION,
    SHA,
    Fake,
    _run,
    _status,
)


def _module(body: str, helpers: str = "", tail: str = "") -> str:
    return (
        'revision = "0016"\ndown_revision = "0015"\n'
        "import sqlalchemy as sa\nfrom alembic import op\n\n"
        + textwrap.dedent(helpers)
        + "\n\ndef upgrade() -> None:\n"
        + textwrap.indent(textwrap.dedent(body).strip("\n"), "    ")
        + "\n\n\ndef downgrade() -> None:\n    pass\n"
        + tail
    )


def _fake(text: str, **kw: object) -> Fake:
    return Fake(
        files={
            OLD: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION},
            SHA: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION, "0016.py": text},
        },
        **kw,
    )


def _migration_status(text: str, **kw: object) -> rp.Status:
    return _status(_run(_fake(text, **kw)), "migration_compat")


BLOCKED_BODIES = [
    'op.execute("DROP INDEX ix_t")',
    'op.execute("ALTER TABLE t DROP CONSTRAINT c")',
    'op.execute("DROP TYPE mood")',
    'op.execute("DELETE FROM t")',
    'op.execute("ALTER TABLE t DROP c")',
    'op.execute(sa.text("drop table t"))',
    'op.execute("ALTER TABLE t ALTER COLUMN x TYPE text")',
    'op.execute("ALTER TABLE t ADD COLUMN x int NOT NULL")',
    'op.drop_constraint("c", "t")',
    'op.drop_index("ix_t")',
    'sa.Enum("a", name="mood").drop(op.get_bind())',
    'op.alter_column("t", "x", type_=sa.Text())',
    'op.alter_column("t", "x", nullable=False, server_default=None)',
    'op.add_column("t", sa.Column("x", sa.Text(), nullable=False, server_default=None))',
    'op.add_column("t", sa.Column("x", sa.Integer(), primary_key=True))',
    'with op.batch_alter_table("t") as b:\n    b.drop_column("x")',
    'op.get_bind().execute(sa.text("TRUNCATE t"))',
]


@pytest.mark.parametrize("body", BLOCKED_BODIES)
def test_bypass_samples_are_blocked(body: str) -> None:
    assert _migration_status(_module(body)) is rp.Status.BLOCKED


def test_destructive_call_hidden_in_a_helper_called_from_upgrade_is_blocked() -> None:
    helpers = """
    def _go():
        op.drop_column("t", "x")
    """
    assert _migration_status(_module("_go()", helpers)) is rp.Status.BLOCKED


def test_helper_not_called_from_upgrade_is_not_scanned() -> None:
    helpers = """
    def _unused():
        op.drop_column("t", "x")
    """
    body = 'op.add_column("t", sa.Column("x", sa.Text(), nullable=True))'
    assert _migration_status(_module(body, helpers)) is rp.Status.PASS


UNKNOWN_BODIES = [
    "op.execute(sql)",
    'op.alter_column("t", "x", nullable=flag)',
    'op.add_column("t", sa.Column("x", sa.Text(), nullable=flag))',
    'op.add_column("t", col)',
    'op.add_column("t", sa.Column("x", sa.Text(), **opts))',
]


@pytest.mark.parametrize(
    "body",
    [
        'op.execute(f"DROP TABLE {name}")',
        'op.execute("ALTER TABLE " + name + " DROP COLUMN x")',
    ],
)
def test_dynamic_sql_with_a_destructive_fragment_is_never_pass(body: str) -> None:
    assert _migration_status(_module(body)) in (rp.Status.BLOCKED, rp.Status.UNKNOWN)


@pytest.mark.parametrize("body", UNKNOWN_BODIES)
def test_unscannable_constructs_are_unknown_not_pass(body: str) -> None:
    assert _migration_status(_module(body)) is rp.Status.UNKNOWN


@pytest.mark.parametrize(
    "body",
    [
        'op.add_column("t", sa.Column("x", sa.Text(), nullable=True))',
        'op.add_column("t", sa.Column("x", sa.Integer(), nullable=False, server_default="0"))',
        'op.create_index("ix_t_x", "t", ["x"])',
        'op.execute(sa.text("UPDATE t SET x = 1"))',
        'op.create_table("n", sa.Column("id", sa.Integer(), primary_key=True))',
        'op.add_column("t", sa.Column("p", sa.Integer(), sa.ForeignKey("u", ondelete="CASCADE")))',
    ],
)
def test_expand_samples_still_pass(body: str) -> None:
    assert _migration_status(_module(body)) is rp.Status.PASS


def test_migration_edited_in_place_is_blocked() -> None:
    """本番に入った後のファイルの書き換え（過去の revision に破壊的変更を忍ばせる）。"""
    edited = BASE_MIGRATION + "\n# edited\n"
    fake = Fake(
        files={
            OLD: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION},
            SHA: {"0014.py": ROOT_MIGRATION, "0015.py": edited},
        }
    )
    report = _run(fake)
    assert _status(report, "migration_compat") is rp.Status.BLOCKED
    detail = next(r.detail for r in report.results if r.name == "migration_compat")
    assert "0015.py" in detail


def test_migration_removed_is_blocked() -> None:
    fake = Fake(
        files={
            OLD: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION},
            SHA: {"0014.py": ROOT_MIGRATION},
        }
    )
    assert _status(_run(fake), "migration_compat") is rp.Status.BLOCKED


# ---- marker（なりすまし対策）

DROP = 'op.drop_column("t", "x")'
MARK = "# contract-migration: approved ADR-0050"


def test_marker_comment_with_accepted_deployed_adr_that_names_the_revision_passes() -> None:
    assert _migration_status(_module(DROP, tail=MARK + "\n")) is rp.Status.PASS


def test_marker_inside_a_string_or_docstring_does_not_count() -> None:
    in_string = _module(DROP, tail=f'NOTE = "{MARK}"\n')
    assert _migration_status(in_string) is rp.Status.BLOCKED
    in_doc = _module(DROP, tail=f'"""\n{MARK}\n"""\n')
    assert _migration_status(in_doc) is rp.Status.BLOCKED


def test_marker_adr_must_exist_in_the_deployed_base_not_only_the_target() -> None:
    only_target = {(SHA, "0050"): ACCEPTED_ADR}
    assert _migration_status(_module(DROP, tail=MARK), adr_texts=only_target) is rp.Status.BLOCKED


def test_marker_adr_must_be_accepted() -> None:
    proposed = ACCEPTED_ADR.replace("Accepted", "Proposed")
    texts = {(OLD, "0050"): proposed}
    assert _migration_status(_module(DROP, tail=MARK), adr_texts=texts) is rp.Status.BLOCKED


def test_marker_adr_must_mention_the_migration_revision() -> None:
    other = ACCEPTED_ADR.replace("0016", "0099")
    texts = {(OLD, "0050"): other}
    assert _migration_status(_module(DROP, tail=MARK), adr_texts=texts) is rp.Status.BLOCKED


def test_valid_marker_also_clears_an_unscannable_migration() -> None:
    assert _migration_status(_module("op.execute(sql)", tail=MARK)) is rp.Status.PASS
