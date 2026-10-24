"""CHECK 語彙の拡張だけを通す、狭い走査規則（ADR-0060 / INV-82）。

走査は無効化も緩和もしていない。通るのは「同じ名前の CHECK を旧集合の真の上位集合で
作り直す」宣言と、その宣言どおりの 2 文（drop → create）だけで、
それ以外の drop は従来どおり BLOCKED。
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from domain import release_preflight as rp
from tests.unit.test_release_migration_scan import _migration_status, _module

REPO = pathlib.Path(__file__).resolve().parents[2]
REAL_0017 = REPO / "infrastructure/db/migrations/versions/0017_daily_workflow_failure_kinds.py"

DECL = """
CHECK_WIDENING = {
    "table": "t",
    "constraint": "ck_t_kind",
    "column": "kind",
    "old": ("A", "B"),
    "new": ("A", "B", "C"),
}
"""

BODY = """
with op.batch_alter_table("t") as batch:
    batch.drop_constraint("ck_t_kind", type_="check")
    batch.create_check_constraint("ck_t_kind", "kind IN ('A', 'B', 'C')")
"""


def _status(body: str = BODY, decl: str = DECL, extra: str = "") -> rp.Status:
    return _migration_status(_module(textwrap.dedent(body), decl + extra))


def test_a_declared_strict_superset_widening_passes() -> None:
    assert _status() is rp.Status.PASS


def test_the_widening_is_reported_not_silently_ignored() -> None:
    src = _module(textwrap.dedent(BODY), DECL)
    assert rp.scan_migration(src) == []
    assert rp.check_widenings(src) == ["ck_t_kind on t: 2 -> 3 allowed values"]


def test_the_real_0017_is_recognised_as_a_widening() -> None:
    src = REAL_0017.read_text()
    assert rp.scan_migration(src) == []
    assert len(rp.check_widenings(src)) == 1


def test_without_a_declaration_the_same_drop_is_still_blocked() -> None:
    assert _status(decl="") is rp.Status.BLOCKED


@pytest.mark.parametrize(
    "old,new",
    [
        ('("A", "B")', '("A", "B")'),  # 同じ集合は拡張ではない
        ('("A", "B", "C")', '("A", "B")'),  # 縮小
        ('("A", "B")', '("A", "C", "D")'),  # B を落とす（旧コードの値が通らなくなる）
        ('("A", "B")', '("A", "A", "B", "C")'),  # 重複
        ("()", '("A",)'),  # 旧集合が空
    ],
)
def test_a_declaration_that_is_not_a_strict_superset_is_blocked(old: str, new: str) -> None:
    decl = DECL.replace('("A", "B"),', old + ",").replace('("A", "B", "C"),', new + ",")
    assert _status(decl=decl) is rp.Status.BLOCKED


def test_the_created_values_must_equal_the_declared_new_set() -> None:
    body = BODY.replace("'A', 'B', 'C'", "'A', 'B', 'C', 'EXTRA'")
    assert _status(body) is rp.Status.BLOCKED
    body = BODY.replace("'A', 'B', 'C'", "'A', 'B'")
    assert _status(body) is rp.Status.BLOCKED


@pytest.mark.parametrize(
    "cond",
    [
        "kind IS NOT NULL",
        "other IN ('A', 'B', 'C')",
        "kind IN ('A', 'B', 'C') OR 1=1",
        "kind IN ('A', 'B', 'C'); DROP TABLE t",
        "kind IN ('A', 'B', 'C\\'); DELETE FROM t; --')",
        "kind NOT IN ('A')",
    ],
)
def test_only_a_plain_column_in_list_condition_is_accepted(cond: str) -> None:
    body = BODY.replace("kind IN ('A', 'B', 'C')", cond)
    assert _status(body) is rp.Status.BLOCKED


def test_a_non_literal_condition_is_not_accepted() -> None:
    body = BODY.replace("\"kind IN ('A', 'B', 'C')\"", "cond")
    assert _status(body) in (rp.Status.BLOCKED, rp.Status.UNKNOWN)


@pytest.mark.parametrize(
    "body",
    [
        # 別名の制約を落とす
        BODY.replace('batch.drop_constraint("ck_t_kind"', 'batch.drop_constraint("ck_other"'),
        # CHECK ではない制約を落とす
        BODY.replace('type_="check"', 'type_="unique"'),
        BODY.replace('type_="check"', 'type_="foreignkey"'),
        BODY.replace('type_="check"', ""),
        # 別名で作り直す（旧制約は消えるのに新制約は別物）
        BODY.replace(
            'batch.create_check_constraint("ck_t_kind"', 'batch.create_check_constraint("ck_x"'
        ),
        # 宣言と別の表
        BODY.replace('batch_alter_table("t")', 'batch_alter_table("u")'),
        # create が無い（制約を落とすだけ）
        BODY.split("    batch.create_check_constraint")[0],
        # create が先
        """
        with op.batch_alter_table("t") as batch:
            batch.create_check_constraint("ck_t_kind", "kind IN ('A', 'B', 'C')")
            batch.drop_constraint("ck_t_kind", type_="check")
        """,
        # with の外
        """
        op.drop_constraint("ck_t_kind", "t", type_="check")
        op.create_check_constraint("ck_t_kind", "t", "kind IN ('A', 'B', 'C')")
        """,
        # 同じ with に別の操作が混ざる
        BODY + '    batch.drop_column("x")\n',
        # 2 回落とす
        BODY
        + '\nwith op.batch_alter_table("t") as b2:\n'
        + '    b2.drop_constraint("ck_t_kind", type_="check")\n',
    ],
)
def test_anything_that_is_not_exactly_the_declared_widening_is_blocked(body: str) -> None:
    assert _status(body) is rp.Status.BLOCKED


def test_other_destructive_calls_in_the_same_file_are_still_blocked() -> None:
    body = BODY + '\nop.drop_column("t", "x")\n'
    assert _status(body) is rp.Status.BLOCKED
    body = BODY + '\nop.execute("DELETE FROM t")\n'
    assert _status(body) is rp.Status.BLOCKED


def test_a_widening_hidden_in_a_helper_is_judged_the_same_way() -> None:
    helpers = DECL + "\ndef _go():\n" + textwrap.indent(textwrap.dedent(BODY).strip("\n"), "    ")
    assert _migration_status(_module("_go()", helpers)) is rp.Status.PASS
    bad = helpers.replace("'C')\")", "'C', 'X')\")")
    assert _migration_status(_module("_go()", bad)) is rp.Status.BLOCKED


@pytest.mark.parametrize(
    "decl",
    [
        # 文字列・docstring の中の宣言は数えない
        'DOC = """' + DECL + '"""',
        "# " + DECL.replace("\n", "\n# "),
        # 関数内の代入
        "def _x():\n    CHECK_WIDENING = {}\n",
        # 変数を使う宣言は静的に読めない
        'A = ("A", "B")\nCHECK_WIDENING = {"table": "t", "constraint": "ck_t_kind",'
        ' "column": "kind", "old": A, "new": A + ("C",)}',
        # キー欠落・余計なキー
        DECL.replace('    "column": "kind",\n', ""),
        DECL.replace('"new": ("A", "B", "C"),', '"new": ("A", "B", "C"), "skip": True,'),
    ],
)
def test_only_a_module_level_literal_declaration_counts(decl: str) -> None:
    assert _status(decl=decl) is rp.Status.BLOCKED


def test_downgrade_is_not_scanned_and_cannot_smuggle_an_upgrade_drop() -> None:
    src = _module(textwrap.dedent(BODY), DECL, tail='\n\ndef _x():\n    op.drop_column("t", "z")\n')
    assert rp.scan_migration(src) == []


def test_the_marker_route_is_unchanged_for_other_contract_migrations() -> None:
    assert _migration_status(_module('op.drop_column("t", "x")')) is rp.Status.BLOCKED
